"""Fusion-side operations: insert / swap fixtures and jaws, mount the workpiece, update CAM.

Everything here is deliberately built from plain, long-stable API pieces:
referenced-component inserts, a local container component, named joint
origins, rigid joints, user parameters and attributes for bookkeeping.
No configurations.

The fixture (and any jaws) live inside a local "FM Fixture" container. CAM
setups select the container, not the fixture, so swapping what is inside it
never breaks a setup's fixture selection.
"""

import json
import math

import adsk.cam
import adsk.core
import adsk.fusion

from . import jawmath as jm
from . import library

ATTR_GROUP = 'ForefrontFixtureManager'
ROLE = 'role'
ROLE_FIXTURE = 'fixture'          # v0.1 top-level fixture occurrence (migrated on apply)
ROLE_CONTAINER = 'fixtureContainer'
CONTAINER_NAME = 'FM Fixture'
ROLE_JOINT = 'mountJoint'
ROLE_JAW_JOINT = 'jawJoint'
ROLE_PLATE_JOINT = 'plateJoint'
ROLE_WCS = 'wcs'
ROLE_WCS_JOINT = 'wcsJoint'
ROLE_SEATS = 'chuckSeats'
ROLE_SEATS_JOINT = 'chuckSeatsJoint'
SEATS_NAME = 'FM Chuck Seats'
SEATS_CENTER = 'FM_SeatsCenter'
WCS_CUBE_CM = 1.0
PLATE_JOINT_NAME = 'FM Plate'
PLATE_MOUNT_JOINT_NAME = 'FM Plate Mount'
MACHINE_POS = 'FM_MachinePos'
STATE = 'state'
SKIP_SETUP_TAG = '[noFM]'
JOINT_NAME = 'FM Mount'
JAW_JOINT_NAME = 'FM Jaw {}'
SOFT_JAW_NAME = 'FM Soft Jaw {}'
JAW_STOCK_NAME = 'FM Soft Jaw Stock {}'
ROLE_JAW_STOCK = 'jawStock'
ROLE_JAW_STOCK_JOINT = 'jawStockJoint'


class FixtureError(Exception):
    pass


# --- stations -----------------------------------------------------------------
# A job file can hold up to three fixture stacks. OP1: the part goes to the fixture; the stack
# is grounded. OP2 and OP3: the fixture goes to the part. Their stack hangs off a point on the
# stock (FM_OP2 near the stock top, FM_OP3 near the bottom, so the part can flip back and
# forth), so the part never moves and each op can cut the previous op's rest stock. Public
# entry points take a `station`; internals read the active one. OP1 keeps the original names
# so older files work.
STATIONS = ('OP1', 'OP2', 'OP3')
_station = ['OP1']


class use_station(object):
    def __init__(self, station):
        if station not in STATIONS:
            raise FixtureError('Unknown station "{}".'.format(station))
        self.station = station

    def __enter__(self):
        self.prev = _station[0]
        _station[0] = self.station

    def __exit__(self, *exc):
        _station[0] = self.prev


def current_station():
    return _station[0]


def is_hanging():
    """OP2/OP3: the stack hangs off the part instead of being grounded."""
    return _station[0] != 'OP1'


def hang_target():
    """The stock point a hanging station's stack is joined to: FM_OP2, FM_OP3."""
    return 'FM_' + _station[0]


def station_name(base):
    """Names that must not collide between stations (parameters, joints)."""
    return base if _station[0] == 'OP1' else '{}_{}'.format(base, _station[0])


def app():
    return adsk.core.Application.get()


def active_design():
    doc = app().activeDocument
    if not doc:
        raise FixtureError('No active document.')
    design = adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
    if not design:
        raise FixtureError('The active document has no design.')
    return doc, design


# ------------------------------------------------------------------ state

def _state_key():
    return STATE if _station[0] == 'OP1' else '{}_{}'.format(STATE, _station[0])


def load_state(design):
    attr = design.attributes.itemByName(ATTR_GROUP, _state_key())
    if not attr:
        return {}
    try:
        return json.loads(attr.value)
    except ValueError:
        return {}


def save_state(design, state):
    design.attributes.add(ATTR_GROUP, _state_key(), json.dumps(state))


def _role(entity):
    # Some entities refuse attributes (e.g. joints on a broken external reference): not ours.
    try:
        attr = entity.attributes.itemByName(ATTR_GROUP, ROLE)
    except Exception:
        return None
    return attr.value if attr else None


# ------------------------------------------------------------------ geometry helpers

def _point_in_frame(frame_jo, world_point):
    """`world_point` expressed in `frame_jo`'s coordinate frame, as an (x, y, z) tuple."""
    inv = frame_jo.transform.copy()
    inv.invert()
    p = world_point.copy()
    p.transformBy(inv)
    return (p.x, p.y, p.z)


def _origin_of(jo):
    t = jo.transform.translation
    return adsk.core.Point3D.create(t.x, t.y, t.z)


def _rel(frame_jo, other_jo):
    """Position of `other_jo` in `frame_jo`'s frame."""
    return _point_in_frame(frame_jo, _origin_of(other_jo))


# ------------------------------------------------------------------ lookups

def _raw_joint_origins(comp, context_occ, path):
    """Yield (path, name, joint_origin) for a component and everything below it.
    `context_occ` is None for the fixture's own design (native objects), otherwise the
    occurrence the results are proxied through."""
    for jo in comp.jointOrigins:
        yield path, jo.name, (jo.createForAssemblyContext(context_occ) if context_occ else jo)
    children = context_occ.childOccurrences if context_occ else comp.occurrences
    for child in children:
        try:
            child_comp = child.component
        except Exception:
            continue  # broken external reference: nothing readable inside it
        yield from _raw_joint_origins(child_comp, child, path + [child.name])


def collect_joint_origins(occ=None, component=None):
    """[(key, joint_origin)] inside a fixture occurrence (root-context proxies) or inside a
    component (native; used while registering the fixture's own design).

    A key is the joint origin's name when that name is unique in the fixture, otherwise
    "<occurrence path>/<name>" - e.g. the framework's several "Zero Point Attachment"
    containers. Keys depend only on the fixture file, so they match between registering
    the fixture and using it inside a CAM design.
    """
    raw = list(_raw_joint_origins(occ.component if occ else component, occ, []))
    keys = library.joint_origin_keys([(path, name) for path, name, _ in raw])
    return list(zip(keys, [jo for _, _, jo in raw]))


def find_mount(occ, mount_key):
    for key, jo in collect_joint_origins(occ=occ):
        if key == mount_key:
            return jo
    return None


def _find_named(occ, name):
    """A joint origin inside `occ` by its own name (FM_Bolt, FM_Grip, FM_Center, ...)."""
    pairs = collect_joint_origins(occ=occ)
    key = jm.find_key([k for k, _ in pairs], name)
    return dict(pairs).get(key) if key else None


def _attr(entity, name):
    try:
        a = entity.attributes.itemByName(ATTR_GROUP, name)
    except Exception:
        return None
    return a.value if a else None


def _station_of(entity):
    return _attr(entity, 'station') or 'OP1'


def find_container(design):
    for occ in design.rootComponent.occurrences:
        if _role(occ) == ROLE_CONTAINER and _station_of(occ) == _station[0]:
            return occ
    return None


def _legacy_fixture(design):
    """v0.1 inserted the fixture directly under root."""
    if _station[0] != 'OP1':
        return None
    for occ in design.rootComponent.occurrences:
        if _role(occ) == ROLE_FIXTURE:
            return occ
    return None


def _child(container, name):
    if container is None or not name:
        return None
    for occ in container.childOccurrences:
        if occ.name == name:
            return occ
    return None


def find_jaw_occurrences(design, state=None):
    state = state if state is not None else load_state(design)
    container = find_container(design)
    names = (state.get('jaws') or {}).get('occs') or []
    occs = [_child(container, n) for n in names]
    return occs if len(occs) >= 2 and all(occs) else []


def find_fixture_occurrence(design, state=None):
    state = state if state is not None else load_state(design)
    container = find_container(design)
    if container:
        occ = _child(container, state.get('fixture_occ'))
        if occ:
            return occ
        others = set((state.get('jaws') or {}).get('occs') or [])
        others.add((state.get('plate') or {}).get('occ'))
        for occ in container.childOccurrences:
            if occ.name not in others and _role(occ.nativeObject or occ) not in (ROLE_WCS, ROLE_SEATS, ROLE_JAW_STOCK):
                return occ
    return _legacy_fixture(design)


def _find_seats(container):
    for occ in (container.childOccurrences if container else []):
        if _role(occ.nativeObject or occ) == ROLE_SEATS:
            return occ
    return None


def find_managed_joints(design, roles=(ROLE_JOINT, ROLE_JAW_JOINT, ROLE_PLATE_JOINT, ROLE_WCS_JOINT,
                                       ROLE_SEATS_JOINT, ROLE_JAW_STOCK_JOINT)):
    # The add-in's joints live in root (the workpiece mount) or in the FM Fixture container
    # (Fusion files a joint under the lowest component holding both parts). Look only there:
    # rootComponent.allJoints raises in any design with a broken external reference.
    out = [j for j in design.rootComponent.joints
           if _role(j) in roles and _station_of(j) == _station[0]]
    container = find_container(design)
    if container:
        out += [j.createForAssemblyContext(container) for j in container.component.joints
                if _role(j) in roles]
    return out


def find_mount_joint(design):
    joints = find_managed_joints(design, (ROLE_JOINT,))
    return joints[0] if joints else None


def find_workpiece_origin(design, state=None):
    """Stored joint origin first (by entity token), then any joint origin named FM_Stock
    outside the fixture."""
    state = state if state is not None else load_state(design)
    token = state.get('workpiece_token')
    if token:
        for ent in design.findEntityByToken(token):
            jo = adsk.fusion.JointOrigin.cast(ent)
            if jo and jo.isValid:
                return jo
    for occ in design.rootComponent.occurrences:
        try:
            if _role(occ) in (ROLE_FIXTURE, ROLE_CONTAINER):
                continue
            found = collect_joint_origins(occ=occ)
        except Exception:
            continue  # broken external reference
        want = hang_target() if is_hanging() else library.WORKPIECE_ORIGIN_NAME
        for key, jo in found:
            if key.rsplit('/', 1)[-1] == want:
                return jo
    return None


def find_data_file(urn):
    try:
        df = app().data.findFileById(urn)
    except Exception:
        df = None
    if not df:
        raise FixtureError('Could not find a Fusion file for URN:\n{}\n\n'
                           'It may have been deleted, moved to a project you cannot access, '
                           'or you may be offline.'.format(urn))
    return df


def _eval_cm(design, expression):
    try:
        return design.unitsManager.evaluateExpression(expression, 'cm')
    except Exception:
        return 0.0


def _current_offsets(design, state=None):
    """The user's mount offsets, read from the FM Mount joint itself so hand edits survive a
    re-apply. The add-in's own automatic part of the offsets (jaw grip centering, base-top
    seat) is subtracted. Falls back to the saved settings when there is no joint."""
    state = state if state is not None else load_state(design)
    out = {'z': state.get('z_offset_cm', 0.0), 'x': state.get('x_offset_cm', 0.0),
           'y': state.get('y_offset_cm', 0.0), 'angle': state.get('angle_rad', 0.0),
           'flip': bool(state.get('flip', False))}
    joint = find_mount_joint(design)
    if joint is None:
        return out
    try:
        auto_x = _eval_cm(design, state['auto_x_expr']) if state.get('auto_x_expr') else 0.0
        auto_y = _eval_cm(design, state['auto_y_expr']) if state.get('auto_y_expr') else 0.0
        # The StockOffsetX share is the parameter's, not a typed offset: leave it out.
        shift = state.get('shift') or {}
        param = design.userParameters.itemByName(shift.get('param', '')) if shift else None
        if param is not None:
            if shift['axis'] == 'x':
                auto_x += shift['sign'] * param.value
            else:
                auto_y += shift['sign'] * param.value
        out.update({
            'z': joint.offset.value - state.get('auto_z_cm', 0.0),
            'x': joint.offsetX.value - auto_x,
            'y': joint.offsetY.value - auto_y,
            'angle': joint.angle.value - state.get('auto_angle_rad', 0.0),
            'flip': joint.isFlipped,
        })
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ register

def inspect_fixture_document():
    """Read URN, name, joint origins and jaw geometry from the active (fixture) document."""
    doc, design = active_design()
    df = doc.dataFile
    if df is None:
        raise FixtureError('Save the fixture design to a Fusion project first. '
                           'The library tracks fixtures by the saved file URN.')
    origins = collect_joint_origins(component=design.rootComponent)
    by_key = dict(origins)
    heights = {}
    for key, jo in origins:
        try:
            # transform includes the joint origin's offsets; geometry.origin is only the
            # snapped base point (a coordinate-placed origin reads 0 there).
            heights[key] = jo.transform.translation.z
        except Exception:
            pass
    project = ''
    try:
        project = df.parentProject.name
    except Exception:
        pass
    keys = library.order_mounts([k for k, _ in origins])
    info = {
        'urn': df.id,
        'name': df.name,
        'project': project,
        'version': df.versionNumber,
        'mount_points': keys,
        'roles': library.classify(keys),
        'heights_cm': heights,
        'kind_suggestion': jm.suggest_kind(keys),
        'positions': sorted(jm.jaw_seats(keys)),
        'chuck_positions': sorted(jm.main_seats(keys)),
        'reversible': jm.is_reversible(keys),
        'plate_positions': {pos: sorted(p, key=int) for pos, p in jm.plate_positions(keys).items()},
        'stud_patterns': list(jm.stud_patterns(keys)),
        'is_configured': bool(getattr(design, 'isConfiguredDesign', False)),
        'loose': loose_components(design),
    }
    # Jaw files: where the grip edges sit relative to the bolt interface.
    bolt_key = jm.find_key(keys, jm.BOLT)
    if bolt_key:
        info['grip_local_cm'] = {}
        for name in jm.jaw_grips(keys):
            info['grip_local_cm'][name] = list(_rel(by_key[bolt_key], by_key[jm.find_key(keys, name)]))
    return info


def loose_components(design):
    """Top-level components of a fixture file that won't move with its FM_ joint origins when the
    file is placed by a joint. A component is anchored when it is locked to the file (ground to
    parent), carries FM_ joint origins, or is joined (directly or through other components) to
    something anchored; joints between free components don't count, so a base with its clamps
    jointed to it is still loose as a group. Fusion keeps loose components where the file put
    them, so the model shows in the wrong place on a plate or at OP2/OP3.
    Returns one component per loose group: locking it brings the rest of its group along through
    their joints. A joint moves its first component onto its second, so the one to lock is a
    component joints have not moved (it is still where it was inserted, which is where Ground to
    Parent puts it). Locking a moved one snaps it back to where it was first placed."""
    root = design.rootComponent
    occs = {o.name: o for o in root.occurrences}

    def top(occ):
        return occ.fullPathName.split('+')[0] if occ else None

    anchored, links, has_body = set(), {name: set() for name in occs}, set()
    moved, targets = {name: 0 for name in occs}, {name: 0 for name in occs}
    for name, occ in occs.items():
        try:
            comp = occ.component
            if comp.bRepBodies.count or any(o.bRepBodies.count for o in comp.allOccurrences):
                has_body.add(name)
            if occ.isGroundToParent or occ.isGrounded or any(
                    jo.name.upper().startswith('FM_') for jo in comp.allJointOrigins):
                anchored.add(name)
        except Exception:
            anchored.add(name)  # unreadable: leave it alone
    for joint in list(root.joints) + list(root.asBuiltJoints):
        try:
            a, b = top(joint.occurrenceOne), top(joint.occurrenceTwo)
        except Exception:
            continue
        if a in links and b in links and a != b:
            links[a].add(b)
            links[b].add(a)
            if joint.objectType == adsk.fusion.Joint.classType():  # as-built joints move nothing
                moved[a] += 1
                targets[b] += 1
        elif (a in links) != (b in links):
            anchored.add(a if a in links else b)  # jointed to the file's own geometry
    reached, todo = set(anchored), list(anchored)
    while todo:
        for nxt in links[todo.pop()] - reached:
            reached.add(nxt)
            todo.append(nxt)
    out, seen = [], set()
    for name in occs:
        if name in reached or name in seen:
            continue
        group, todo = {name}, [name]
        while todo:
            for nxt in links[todo.pop()] - group:
                group.add(nxt)
                todo.append(nxt)
        seen |= group
        if group & has_body:
            out.append(min(sorted(group), key=lambda n: (moved[n] > 0, -targets[n], -len(links[n]))))
    return out


def lock_loose_components(names):
    """Ground the named top-level components to their parent in the active (fixture) document
    and save a new version, so Register never stores a fixture that falls apart when placed."""
    doc, design = active_design()
    occs = design.rootComponent.occurrences
    before = {o.name: o.transform2.asArray() for o in occs}
    locked = [occs.itemByName(name) for name in names]
    locked = [o for o in locked if o is not None]
    for occ in locked:
        occ.isGroundToParent = True
    adsk.fusion.Design.cast(design).computeAll()
    shifted = [o.name for o in occs
               if o.name in before and any(abs(x - y) > 1e-6 for x, y in zip(before[o.name], o.transform2.asArray()))]
    if shifted:
        for occ in locked:
            occ.isGroundToParent = False
        raise FixtureError('Locking {} would move {} from where the joints put it, so nothing was locked or '
                           'saved. Ground the component the others are jointed to (Ground to Parent in the '
                           'browser), save, and register again.'.format(', '.join(names), ', '.join(shifted)))
    if not doc.save('Fixture Manager: locked {} to the parent so it follows the FM_ joint origins'.format(
            ', '.join(names))):
        raise FixtureError('Could not save "{}" after locking its components.'.format(doc.name))
    return ['Locked {} to its parent and saved "{}" as a new version.'.format(', '.join(names), doc.name)]


# ------------------------------------------------------------------ apply / swap

def _remove_managed_joints(design):
    for j in find_managed_joints(design):
        j.deleteMe()


def _ensure_container(design, msgs):
    container = find_container(design)
    if container:
        return container
    name = CONTAINER_NAME if _station[0] == 'OP1' else 'FM ' + _station[0]
    container = design.rootComponent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    container.component.name = name
    container.attributes.add(ATTR_GROUP, ROLE, ROLE_CONTAINER)
    container.attributes.add(ATTR_GROUP, 'station', _station[0])
    # An OP2 stack hangs off the part, so its container must be free to move.
    container.isGrounded = not is_hanging()
    if is_hanging():
        container.isGroundToParent = False
    msgs.append('Created container component "{}" (select it as the setup fixture).'.format(name))
    return container


def _insert(container, urn, ground):
    df = find_data_file(urn)
    try:
        native = container.component.occurrences.addByInsert(df, adsk.core.Matrix3D.create(), True)
    except Exception as ex:
        raise FixtureError('Fusion refused to insert "{}" as a referenced component.\n'
                           'Make sure this CAM design is saved and is not the fixture file itself.\n\n'
                           'Fusion said: {}'.format(df.name, ex))
    if getattr(native, 'isConfiguration', False):
        raise FixtureError('"{}" is a configuration of a configured design. Save it as an ordinary '
                           'design and register that file instead.'.format(df.name))
    occ = native.createForAssemblyContext(container)
    if ground:
        try:
            native.isGroundToParent = True
        except Exception:
            occ.isGrounded = True
    elif native.isGroundToParent:
        # Fusion locks the first component added to an empty component to its parent. A joint
        # that has to move or turn it (a Y-up plate, say) then conflicts with that lock.
        native.isGroundToParent = False
    return native, occ


def _machine_position(container):
    """The fixed point a plate sits on: FM_MachinePos at the container's origin, Z up (created
    once). Plates are placed by joint, not by their file origin, so a plate modelled Y-up or
    off-origin still lands right."""
    comp = container.component
    jo = comp.jointOrigins.itemByName(MACHINE_POS)
    if jo is None:
        ji = comp.jointOrigins.createInput(adsk.fusion.JointGeometry.createByPoint(comp.originConstructionPoint))
        jo = comp.jointOrigins.add(ji)
        jo.name = MACHINE_POS
    return jo.createForAssemblyContext(container)


def wcs_point_name():
    return 'FM_WCS_' + _station[0]


def _find_wcs(container):
    for occ in container.childOccurrences:
        if _role(occ.nativeObject or occ) == ROLE_WCS:
            return occ
    return None


def _attach_wcs(design, container, plate_name, msgs):
    """A small LOCAL WCS component per station (a 10 mm cube with its top-center point named
    FM_WCS_<station>), joined to the plate's FM_WCS. Setups select its point and faces once;
    swapping the plate only re-joins it, so the setup's WCS selection is never lost."""
    wcs = _find_wcs(container)
    if wcs is None:
        native = container.component.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        comp = native.component
        comp.name = 'FM {} WCS'.format(_station[0])
        native.attributes.add(ATTR_GROUP, ROLE, ROLE_WCS)
        native.isGroundToParent = False
        h = WCS_CUBE_CM / 2
        sk = comp.sketches.add(comp.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addCenterPointRectangle(adsk.core.Point3D.create(0, 0, 0),
                                                            adsk.core.Point3D.create(h, h, 0))
        ext = comp.features.extrudeFeatures.createInput(sk.profiles.item(0),
                                                        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        ext.setDistanceExtent(False, adsk.core.ValueInput.createByReal(-WCS_CUBE_CM))
        comp.features.extrudeFeatures.add(ext).bodies.item(0).name = 'WCS Cube'
        ji = comp.jointOrigins.createInput(adsk.fusion.JointGeometry.createByPoint(comp.originConstructionPoint))
        comp.jointOrigins.add(ji).name = wcs_point_name()
        wcs = native.createForAssemblyContext(container)
        msgs.append('Created {} (pick its point and cube faces for the setup WCS).'.format(comp.name))
    point = _find_named(wcs, wcs_point_name())
    target = _find_named(_child(container, plate_name), jm.PLATE_WCS)
    if target is None:
        msgs.append('Plate has no FM_WCS; the station WCS was not attached.')
        return
    _add_rigid_joint(design.rootComponent, point, target, station_name('FM WCS'), ROLE_WCS_JOINT)


def _build_chuck_seats(design, container, chuck_name, chuck_entry, pos, count, msgs):
    """A chuck's jaw seats for `count` jaws: a small LOCAL component joined to the chuck's
    FM_Center, holding FM_JawSeat_<pos>1..N. Seat 1 copies the chuck's main seat and the rest
    are it turned about the chuck axis (2 jaws 180 deg apart, 3 jaws 120 deg, ...). The jaws
    then go on these seats exactly as on a vise. Rebuilt on every apply, so changing the jaw
    count just respaces them. Returns the component's occurrence name."""
    old = _find_seats(container)
    if old:
        old.deleteMe()
    chuck = _child(container, chuck_name)
    main_key = jm.main_seats([k for k, _ in collect_joint_origins(occ=chuck)]).get(pos)
    center = _find_named(chuck, jm.CENTER)
    if not main_key or center is None:
        raise FixtureError('Chuck "{}" needs FM_Center and a main jaw seat FM_JawSeat_{}1.'.format(
            chuck_entry.get('name'), pos))
    # The main seat in the chuck center's own frame. Only its position is used: on a centering
    # chuck every jaw faces the axis, so the seat's X is pointed at the axis here, whichever way
    # it was drawn in the chuck file.
    to_center = center.transform.copy()
    to_center.invert()
    drawn = find_mount(chuck, main_key).transform.copy()
    drawn.transformBy(to_center)
    origin, _, _, z_axis = drawn.getAsCoordinateSystem()
    radius = math.hypot(origin.x, origin.y)
    if abs(z_axis.z - 1.0) > 1e-6 or radius < 1e-4:
        raise FixtureError('The main jaw seat FM_JawSeat_{}1 on "{}" must be off the chuck axis, with Z '
                           'pointing the same way as FM_Center\'s Z.'.format(pos, chuck_entry.get('name')))
    inward = adsk.core.Vector3D.create(-origin.x / radius, -origin.y / radius, 0)
    local = adsk.core.Matrix3D.create()
    local.setWithCoordinateSystem(origin, inward, adsk.core.Vector3D.create(-inward.y, inward.x, 0),
                                  adsk.core.Vector3D.create(0, 0, 1))

    native = container.component.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = native.component
    comp.name = SEATS_NAME
    native.attributes.add(ATTR_GROUP, ROLE, ROLE_SEATS)
    native.isGroundToParent = False
    _add_joint_origin(comp, SEATS_CENTER, adsk.core.Matrix3D.create())
    for n, angle in enumerate(jm.jaw_angles(count), 1):
        turn = adsk.core.Matrix3D.create()
        turn.setToRotation(angle, adsk.core.Vector3D.create(0, 0, 1), adsk.core.Point3D.create(0, 0, 0))
        seat = local.copy()
        seat.transformBy(turn)
        _add_joint_origin(comp, 'FM_JawSeat_{}{}'.format(pos, n), seat)
    seats = native.createForAssemblyContext(container)
    _add_rigid_joint(design.rootComponent, _find_named(seats, SEATS_CENTER), center,
                     station_name('FM Chuck Seats'), ROLE_SEATS_JOINT)
    # Check: seat 1 must sit on the chuck's own main seat, with its X pointing at the axis.
    seats, chuck = _find_seats(container), _child(container, chuck_name)
    first = _find_named(seats, 'FM_JawSeat_{}1'.format(pos))
    to_axis = _rel(first, _find_named(chuck, jm.CENTER))
    if (_origin_of(first).distanceTo(_origin_of(find_mount(chuck, main_key))) > 0.001
            or abs(to_axis[1]) > 0.001 or to_axis[0] <= 0):
        raise FixtureError('The jaw seats for "{}" did not line up with its main seat FM_JawSeat_{}1. '
                           'Check that FM_Center and the main seat both have Z pointing up.'.format(
                               chuck_entry.get('name'), pos))
    msgs.append('Spaced {} jaw seats evenly around the chuck axis ({:g} deg apart).'.format(count, 360.0 / count))
    return seats.name


def _seat_plate(design, container, plate_name, plate_entry):
    plate_occ = _child(container, plate_name)
    mount = _find_named(plate_occ, jm.PLATE_MOUNT)
    if mount is None:
        raise FixtureError('Plate "{}" has no {} joint origin.'.format(plate_entry.get('name'), jm.PLATE_MOUNT))
    _add_rigid_joint(design.rootComponent, mount, _machine_position(container),
                     PLATE_MOUNT_JOINT_NAME, ROLE_PLATE_JOINT)


def _mount_on_plate(design, container, fixture_name, plate_name, entry, plate, msgs):
    """Seat the vise or fixture on the plate: its stud for the chosen pattern joins the plate's
    receiver for the chosen position, turned by the chosen multiple of 90 degrees."""
    root = design.rootComponent
    fixture_occ = _child(container, fixture_name)
    plate_occ = _child(container, plate_name)
    pattern, position = str(plate['pattern']), plate['position']
    stud_key = jm.stud_patterns([k for k, _ in collect_joint_origins(occ=fixture_occ)]).get(pattern)
    if not stud_key:
        raise FixtureError('"{}" has no FM_ZP_{} stud joint origin for the {} mm pattern.'.format(
            entry.get('name'), pattern, pattern))
    receiver_key = jm.plate_positions([k for k, _ in collect_joint_origins(occ=plate_occ)]).get(
        position, {}).get(pattern)
    if not receiver_key:
        raise FixtureError('Plate "{}" has no FM_ZP_{}_{} receiver.'.format(
            plate['entry'].get('name'), position, pattern))
    rotation = math.radians(plate.get('rotation_deg', 0))
    stud, receiver = find_mount(fixture_occ, stud_key), find_mount(plate_occ, receiver_key)
    joint = _add_rigid_joint(root, stud, receiver, station_name(PLATE_JOINT_NAME), ROLE_PLATE_JOINT,
                             angle_rad=rotation)
    # Fusion's joint angle direction isn't documented: measure it, then set the turn.
    sense = _joint_angle_sense(joint, stud, receiver, rotation)
    joint.angle.value = sense * rotation
    # Check: stud on the receiver, turned by the chosen angle.
    stud = find_mount(_child(container, fixture_name), stud_key)
    receiver = find_mount(_child(container, plate_name), receiver_key)
    gap = _origin_of(stud).distanceTo(_origin_of(receiver))
    turned = jm.wrap(_axis_angle_in(receiver, stud) - rotation)
    if gap > 0.001 or abs(turned) > 0.001:
        raise FixtureError('"{}" did not seat on plate position {} ({} mm): check that the FM_ZP '
                           'joint origins have Z pointing up.'.format(entry.get('name'), position, pattern))
    msgs.append('Seated {} on {} at {} ({} mm pattern), {} deg.'.format(
        fixture_occ.name, plate_occ.name, position, pattern, plate.get('rotation_deg', 0)))


def _insert_jaws(container, jaw_entry, msgs, count=2):
    """One jaw per seat (2 for a vise, 6 for a 6-jaw chuck) from one jaw file. Hard jaws stay
    referenced. Soft jaws become independent local components, so each can be cut on its own."""
    if jaw_entry.get('kind') == jm.KIND_SOFT_JAW:
        occs = _copy_soft_jaws(container, jaw_entry, count)
        msgs.append('Inserted {} fresh soft jaw blanks (local, editable in this file).'.format(count))
    else:
        occs = [_insert(container, jaw_entry['urn'], ground=False)[1] for _ in range(count)]
        msgs.append('Inserted {} hard jaws: {}'.format(count, jaw_entry.get('name', '')))
    return occs


def _copy_soft_jaws(container, jaw_entry, count=2, name_fmt=SOFT_JAW_NAME, role=None):
    """Make local soft jaws from the blank without Break Link (which leaves a copy of the
    blank's features at the top of the timeline for good). The blank is inserted as a normal
    reference just long enough to copy its solids and joint origins, then removed. Also used
    for the uncut soft jaw stock (`name_fmt` / `role`)."""
    _, ref = _insert(container, jaw_entry['urn'], ground=False)
    temp = adsk.fusion.TemporaryBRepManager.get()
    bodies = []
    for body in _all_bodies(ref):
        bodies.append((body.name, temp.copy(body)))
    origins = [(key.rsplit('/', 1)[-1], jo.transform.copy()) for key, jo in collect_joint_origins(occ=ref)]
    ref.deleteMe()
    if not bodies:
        raise FixtureError('Soft jaw blank "{}" has no solid bodies.'.format(jaw_entry.get('name')))

    comp_occs = container.component.occurrences
    occs = []
    for n in range(1, count + 1):
        native = comp_occs.addNewComponent(adsk.core.Matrix3D.create())
        comp = native.component
        comp.name = name_fmt.format(n)
        if role:
            native.attributes.add(ATTR_GROUP, ROLE, role)
        base = comp.features.baseFeatures.add()
        base.startEdit()
        for name, tbody in bodies:
            comp.bRepBodies.add(temp.copy(tbody), base).name = name
        base.finishEdit()
        for name, transform in origins:
            _add_joint_origin(comp, name, transform)
        occs.append(native.createForAssemblyContext(container))
    return occs


def _jaw_stock_occurrences(container):
    return [o for o in (container.childOccurrences if container else [])
            if _role(o.nativeObject or o) == ROLE_JAW_STOCK]


def _sync_jaw_stock(design, container, jaw_entry, jaw_names, rebuild, msgs):
    """Soft jaws get matching stock: an uncut copy of the blank per jaw (FM Soft Jaw Stock N),
    joined bolt to bolt to its jaw so it follows the jaw, and hidden so the cut jaws stay in
    view. CAM uses it as the soft jaw cutting setup's stock. Rebuilt with fresh jaws or when the
    count no longer matches; removed for hard jaws. Returns the stock occurrence names, and
    whether they were (re)built."""
    old = _jaw_stock_occurrences(container)
    soft = jaw_entry.get('kind') == jm.KIND_SOFT_JAW
    if old and (not soft or rebuild or len(old) != len(jaw_names)):
        for occ in old:
            occ.deleteMe()
        old = []
    if not soft:
        return [], False
    built = not old
    if built:
        old = _copy_soft_jaws(container, jaw_entry, len(jaw_names), JAW_STOCK_NAME, ROLE_JAW_STOCK)
    names = [o.name for o in old]
    container = find_container(design)
    for n, (stock_name, jaw_name) in enumerate(zip(names, jaw_names), 1):
        stock = _child(container, stock_name)
        _add_rigid_joint(design.rootComponent, _find_named(stock, jm.BOLT),
                         _find_named(_child(container, jaw_name), jm.BOLT),
                         station_name('FM Jaw Stock {}'.format(n)), ROLE_JAW_STOCK_JOINT)
        stock.isLightBulbOn = False
    if built:
        msgs.append('Made soft jaw stock (uncut blanks, hidden): {}.'.format(', '.join(names)))
    return names, built


def _all_bodies(occ):
    """Solid bodies of an occurrence and everything below it, as root-context proxies."""
    out = [b for b in occ.bRepBodies if b.isSolid]
    for child in occ.childOccurrences:
        out.extend(_all_bodies(child))
    return out


def _add_joint_origin(comp, name, transform):
    """Recreate a Z-up joint origin from a transform. Joint origin offsets are measured in the
    origin's own rotated frame, so the position is rotated back by its angle first."""
    origin, x_axis, _, z_axis = transform.getAsCoordinateSystem()
    if abs(z_axis.z - 1.0) > 1e-6:
        raise FixtureError('Joint origin "{}" on the soft jaw blank must have Z pointing up.'.format(name))
    angle = math.atan2(x_axis.y, x_axis.x)
    c, s_ = math.cos(-angle), math.sin(-angle)
    vi = adsk.core.ValueInput
    ji = comp.jointOrigins.createInput(adsk.fusion.JointGeometry.createByPoint(comp.originConstructionPoint))
    ji.offsetX = vi.createByReal(c * origin.x - s_ * origin.y)
    ji.offsetY = vi.createByReal(s_ * origin.x + c * origin.y)
    ji.offsetZ = vi.createByReal(origin.z)
    ji.angle = vi.createByReal(angle)
    comp.jointOrigins.add(ji).name = name


def _release_workpiece(wp_occ, msgs):
    # Either lock fights the joint ("conflicts with assembly relationships"). Fusion locks the
    # first component made in an empty design to its parent, so most part files hit this.
    if wp_occ.isGrounded:
        wp_occ.isGrounded = False
        msgs.append('Un-grounded workpiece: ' + wp_occ.name)
    if wp_occ.isGroundToParent:
        wp_occ.isGroundToParent = False
        msgs.append('Released ground-to-parent on workpiece: ' + wp_occ.name)


def _add_rigid_joint(root, geo_one, geo_two, name, role, angle_rad=0.0, flip=False,
                     z_cm=0.0, x_expr=None, y_expr=None):
    ji = root.joints.createInput(geo_one, geo_two)
    ji.setAsRigidJointMotion()
    ji.offset = adsk.core.ValueInput.createByReal(z_cm)
    ji.angle = adsk.core.ValueInput.createByReal(angle_rad)
    ji.isFlipped = flip
    try:
        joint = root.joints.add(ji)
    except Exception as ex:
        raise FixtureError('Could not create joint "{}": {}'.format(name, ex))
    joint.name = name
    joint.attributes.add(ATTR_GROUP, ROLE, role)
    joint.attributes.add(ATTR_GROUP, 'station', _station[0])
    if x_expr:
        joint.offsetX.expression = x_expr
    if y_expr:
        joint.offsetY.expression = y_expr
    # joints.add() succeeds even when the joint can't be solved; check before reporting success.
    if joint.healthState != adsk.fusion.FeatureHealthStates.HealthyFeatureHealthState:
        raise FixtureError('Joint "{}" was created but does not solve:\n{}'.format(
            name, joint.errorOrWarningMessage or '(no message from Fusion)'))
    return joint


STOCK_SHIFT_PARAM = 'StockOffsetX'


def _link_stock_shift(design, joint, workpiece_token, fixture_name, msgs):
    """If the design has a StockOffsetX parameter (StockOffsetX_OP2 / _OP3 for OP2 / OP3), add it to the mount
    joint so the stock and part slide along the vise's X axis by that amount (+ = the vise's +X,
    which is the file's +X when the vise isn't turned on its plate). Which joint offset runs
    along the jaws, and which way, depends on how the jaws and stock are turned, so measure it:
    nudge each offset and watch the part move in the vise's frame. Returns {'axis', 'sign'} for
    the saved state (so a re-apply can tell it apart from a typed offset), or None."""
    param = design.userParameters.itemByName(station_name(STOCK_SHIFT_PARAM))
    if param is None:
        return None
    fixture = _child(find_container(design), fixture_name)
    ref = _find_named(fixture, jm.CENTER) or _find_named(fixture, library.MOUNT_PREFIX)
    if ref is None:
        msgs.append('{} not linked: "{}" has no FM_Center or FM_Mount to measure along.'.format(
            param.name, fixture_name))
        return None

    def along():
        wp = find_workpiece_origin(design, {'workpiece_token': workpiece_token})
        fx = _child(find_container(design), fixture_name)
        frame = _find_named(fx, jm.CENTER) or _find_named(fx, library.MOUNT_PREFIX)
        return _rel(frame, wp)[0]

    base = along()
    best = None
    for axis, offset in (('x', joint.offsetX), ('y', joint.offsetY)):
        old = offset.expression or '0 mm'
        offset.expression = '({}) + 10 mm'.format(old)
        moved = along() - base
        offset.expression = old
        if abs(abs(moved) - 1.0) < 0.001:
            best = (axis, 1 if moved > 0 else -1)
    if best is None:
        msgs.append('{} not linked: no joint offset runs along the jaws for this setup.'.format(param.name))
        return None
    axis, sign = best
    offset = joint.offsetX if axis == 'x' else joint.offsetY
    offset.expression = '({}) {} {}'.format(offset.expression or '0 mm', '+' if sign > 0 else '-', param.name)
    if abs(along() - base - param.value) > 0.001:
        raise FixtureError('Could not link {} to the stock position.'.format(param.name))
    msgs.append('Stock shift along the jaws follows {} (now {}).'.format(param.name, param.expression))
    return {'axis': axis, 'sign': sign, 'param': param.name}


def _offset_expr(auto_expr, user_cm):
    if auto_expr:
        return '{} + {:.4f} mm'.format(auto_expr, user_cm * 10.0)
    return '{:.4f} mm'.format(user_cm * 10.0) if user_cm else None


def _axis_angle_in(frame_jo, other_jo):
    """Plan angle of `other_jo`'s X axis, measured in `frame_jo`'s frame."""
    p0 = _rel(frame_jo, other_jo)
    p1 = _point_in_frame(frame_jo, _seat_point(other_jo, (1.0, 0.0, 0.0)))
    return math.atan2(p1[1] - p0[1], p1[0] - p0[0])


def _joint_angle_sense(joint, one, two, base):
    """+1 if Fusion turns geometry one by +angle for a positive joint angle, else -1. Found by
    nudging the joint a quarter turn and looking, then putting it back."""
    joint.angle.value = base + math.pi / 2
    seen = _axis_angle_in(two, one)
    joint.angle.value = base
    return 1 if abs(jm.wrap(seen - (base + math.pi / 2))) < 0.01 else -1


def _set_param(design, name, value_cm, comment, expression=None):
    """Set a user parameter to a value, or to an expression (e.g. 'StockY') when given."""
    p = design.userParameters.itemByName(name)
    if p and expression:
        p.expression = expression
    elif p:
        p.value = value_cm
    else:
        value = (adsk.core.ValueInput.createByString(expression) if expression
                 else adsk.core.ValueInput.createByReal(value_cm))
        design.userParameters.add(name, value, 'mm', comment)


def _hard_jaw_width(design, workpiece_jo, angle_rad):
    """Hard jaws always close flush on the stock. With the template's StockX/StockY parameters
    the width is linked to them, so the jaws follow the stock with no re-apply; otherwise it is
    measured from the stock body now. Returns (expression, cm)."""
    params = design.userParameters
    if params.itemByName(jm.STOCK_X_PARAM) and params.itemByName(jm.STOCK_Y_PARAM):
        expr = jm.stock_width_expr(angle_rad)
    else:
        ex, ey = workpiece_extents(workpiece_jo)
        expr = '{:.4f} mm'.format(jm.grip_width(ex, ey, angle_rad) * 10.0)
    return expr, _eval_cm(design, expr)


def workpiece_extents(workpiece_jo):
    """(x, y) extents of the workpiece component's bodies, in its own frame (cm)."""
    comp = workpiece_jo.assemblyContext.component
    box = None
    for body in comp.bRepBodies:
        b = body.boundingBox
        if box is None:
            box = b.copy()
        else:
            box.combine(b)
    if box is None:
        return 0.0, 0.0
    return box.maxPoint.x - box.minPoint.x, box.maxPoint.y - box.minPoint.y


def _apply_fixture(entry, mount_key, workpiece_jo, z_offset_cm=0.0, angle_rad=0.0,
                  flip=False, update_setups=True, regenerate=False, x_offset_cm=0.0,
                  y_offset_cm=0.0, jaws=None, plate=None):
    """Insert or swap to `entry` inside the fixture container and rigidly mount the workpiece.

    For a plain fixture the workpiece mounts on `mount_key`. For a vise, `jaws` describes the
    jaw setup: {'entry', 'position', 'reversed', 'seat', 'width_cm', 'new_blank'} and the
    workpiece mounts on jaw 1's grip edge, centered between the jaws. `plate`, when given, is
    {'entry', 'position', 'pattern', 'rotation_deg'}: the plate is grounded at the origin and
    the vise or fixture sits on it, stud to receiver.
    Returns a list of human-readable messages."""
    doc, design = active_design()
    if doc.dataFile is None:
        raise FixtureError('Save this CAM design before adding fixtures '
                           '(Fusion needs a saved document to hold external references).')
    msgs = []
    root = design.rootComponent
    state = load_state(design)
    is_vise = entry.get('kind') in jm.JAWED_KINDS  # vises and chucks both take jaws
    is_chuck = entry.get('kind') == jm.KIND_CHUCK
    if is_vise and not jaws:
        raise FixtureError('Choose jaws for the vise "{}".'.format(entry.get('name')))

    if workpiece_jo is None:
        raise FixtureError('Pick the workpiece joint origin (e.g. "{}" at the bottom center '
                           'of the stock).'.format(library.WORKPIECE_ORIGIN_NAME))
    wp_occ = workpiece_jo.assemblyContext
    if wp_occ is None:
        raise FixtureError('The workpiece joint origin is in the root component. Put the part '
                           'and stock in their own component so it can move onto the fixture.')
    if _role(_top_level(wp_occ)) in (ROLE_FIXTURE, ROLE_CONTAINER):
        raise FixtureError('The selected workpiece origin is inside the fixture.')
    workpiece_token = workpiece_jo.entityToken

    _remove_managed_joints(design)

    legacy = _legacy_fixture(design)
    if legacy:
        msgs.append('Moved fixture into the container (removed old top-level "{}").'.format(legacy.name))
        legacy.deleteMe()

    container = _ensure_container(design, msgs)

    # --- jaws: keep cut soft jaws when the new setup can still use them, otherwise remove
    old_jaws = state.get('jaws') or {}
    jaw_occs = find_jaw_occurrences(design, state)
    # Keep the jaws whenever the jaw file is the same: changing position, orientation, seat or
    # width then only rebuilds joints. Re-inserting the same file also confused Fusion's undo.
    keep_jaws = bool(jaw_occs and is_vise and not jaws.get('new_blank')
                     and old_jaws.get('urn') == jaws['entry']['urn'])
    jaw_setups = []
    if jaw_occs and not keep_jaws:
        jaw_setups = _jaw_setups(doc, container, [o.name for o in jaw_occs])
        for occ in jaw_occs:
            occ.deleteMe()
        msgs.append('Removed previous jaws.')
        jaw_occs = []

    # --- zero point plate: kept when it's the same file, otherwise replaced or removed
    old_plate = state.get('plate') or {}
    plate_occ = _child(container, old_plate.get('occ'))
    if plate_occ is not None and (plate is None or old_plate.get('urn') != plate['entry']['urn']):
        msgs.append('Removed plate: ' + plate_occ.name)
        plate_occ.deleteMe()
        plate_occ = None
    if plate is not None and plate_occ is None:
        plate_occ = _insert(container, plate['entry']['urn'], ground=False)[1]
        msgs.append('Inserted plate: ' + plate_occ.name)
    plate_occ_name = plate_occ.name if plate_occ is not None else None
    if plate_occ is not None and not is_hanging():
        _seat_plate(design, container, plate_occ_name, plate['entry'])

    # --- vise / fixture (never mistake the plate we just placed for an old fixture)
    lookup = dict(state, plate={'occ': plate_occ_name}) if plate_occ_name else state
    fixture_occ = find_fixture_occurrence(design, lookup)
    if fixture_occ is not None and state.get('fixture_urn') != entry['urn']:
        msgs.append('Removed fixture: ' + fixture_occ.name)
        fixture_occ.deleteMe()
        fixture_occ = None
    if fixture_occ is None:
        fixture_occ = _insert(container, entry['urn'], ground=plate is None and not is_hanging())[1]
        msgs.append('Inserted fixture: ' + fixture_occ.name)
    else:
        msgs.append('Kept fixture: ' + fixture_occ.name)
        # A kept fixture is locked to the container only when there is no plate to sit on.
        native = fixture_occ.nativeObject or fixture_occ
        lock = plate is None and not is_hanging()
        if native.isGroundToParent != lock:
            native.isGroundToParent = lock
    fixture_occ_name = fixture_occ.name
    if plate is not None:
        _mount_on_plate(design, container, fixture_occ_name, plate_occ_name, entry, plate, msgs)
        _attach_wcs(design, container, plate_occ_name, msgs)

    new_state = {
        'fixture_urn': entry['urn'],
        'fixture_name': entry.get('name', ''),
        'fixture_occ': fixture_occ_name,
        'plate': None if plate is None else {
            'urn': plate['entry']['urn'], 'name': plate['entry'].get('name', ''), 'occ': plate_occ_name,
            'position': plate['position'], 'pattern': plate['pattern'],
            'rotation_deg': plate.get('rotation_deg', 0)},
        'z_offset_cm': z_offset_cm, 'x_offset_cm': x_offset_cm, 'y_offset_cm': y_offset_cm,
        'angle_rad': angle_rad, 'flip': bool(flip),
    }

    if not is_chuck and _find_seats(container):
        _find_seats(container).deleteMe()  # left from a chuck this station used before
    if is_vise:
        if is_chuck:
            count = int(jaws.get('count') or 0)
            if count < 2:
                raise FixtureError('Choose how many jaws the chuck "{}" uses (2 or more).'.format(entry.get('name')))
        else:
            seats = jm.jaw_seats([k for k, _ in collect_joint_origins(occ=_child(container, fixture_occ_name))])
            count = len(seats.get(jaws['position']) or {})
            if not count:
                raise FixtureError('"{}" has no complete set of jaw seats for position {} (FM_JawSeat_{}1, '
                                   '{}2, ...).'.format(entry.get('name'), jaws['position'], jaws['position'],
                                                       jaws['position']))
        if jaw_occs and len(jaw_occs) != count:
            # Same jaw file, but a different number of jaws (another position or jaw count).
            jaw_setups = jaw_setups or _jaw_setups(doc, container, [o.name for o in jaw_occs])
            for occ in jaw_occs:
                occ.deleteMe()
            msgs.append('Removed the previous {} jaws ({} needed now).'.format(len(jaw_occs), count))
            jaw_occs, keep_jaws = [], False
        if jaws['entry'].get('kind') != jm.KIND_SOFT_JAW and not is_hanging():
            # OP1 hard jaws close flush on the stock; any width passed in is ignored. On OP2/OP3
            # the jaws grip what earlier ops left, so the width entered is used as is.
            wp_now = find_workpiece_origin(design, {'workpiece_token': workpiece_token}) or workpiece_jo
            expr, cm = _hard_jaw_width(design, wp_now, angle_rad)
            jaws = dict(jaws, width_expr=expr, width_cm=cm)
        seat_holder = fixture_occ_name
        if is_chuck:
            seat_holder = _build_chuck_seats(design, container, fixture_occ_name, entry, jaws['position'],
                                             count, msgs)
        if not jaw_occs:
            jaw_occs = _insert_jaws(container, jaws['entry'], msgs, count)
        else:
            msgs.append('Kept the jaws' + (' (cut soft jaws).' if old_jaws.get('kind') == jm.KIND_SOFT_JAW else '.'))
            if jm.blank_is_stale(old_jaws, jaws['entry']):
                msgs.append('WARNING: "{}" was re-registered (v{} -> v{}) since these soft jaws were made, '
                            'so they still have the old shape and joint origins. Tick "Start from new soft '
                            'jaw blanks" to replace them.'.format(jaws['entry'].get('name'), old_jaws.get('blank_version'),
                                                                  jm.blank_version(jaws['entry'])))
        # Re-find everything by name: proxies from before the timeline changed can be stale.
        fixture_occ = _child(container, fixture_occ_name)
        jaw_names = [o.name for o in jaw_occs]
        jaw_occs = [_child(container, n) for n in jaw_names]
        mount_jo, auto_x_expr, auto_y_expr, auto_z_cm, auto_angle, jaw_state = _mount_jaws(
            design, entry, fixture_occ, jaw_occs, jaws, keep_jaws, old_jaws, msgs,
            seat_occ=_child(container, seat_holder))
        mount_label = '{} grip (jaw 1)'.format(jaw_state['grip'])
        jaw_state['occs'] = jaw_names
        if is_chuck:
            jaw_state['count'] = count
        stock_names, stock_built = _sync_jaw_stock(design, find_container(design), jaws['entry'], jaw_names,
                                                   not keep_jaws, msgs)
        jaw_state['stock_occs'] = stock_names
        new_state['jaws'] = jaw_state
    else:
        for occ in _jaw_stock_occurrences(container):
            occ.deleteMe()  # soft jaw stock left from an earlier vise
        stock_names, stock_built = [], False
        mount_jo = find_mount(fixture_occ, mount_key)
        if mount_jo is None:
            keys = [k for k, _ in collect_joint_origins(occ=fixture_occ)]
            raise FixtureError('Fixture "{}" has no joint origin "{}".\nFound: {}\n\n'
                               'Re-register the fixture if its joint origins changed.'.format(
                                   entry.get('name'), mount_key, ', '.join(keys) or '(none)'))
        auto_x_expr, auto_y_expr, auto_z_cm, auto_angle = None, None, 0.0, 0.0
        mount_label = mount_key
    new_state.update({'mount': mount_key if not is_vise else jm.GRIP,
                      'auto_x_expr': auto_x_expr, 'auto_y_expr': auto_y_expr,
                      'auto_z_cm': auto_z_cm, 'auto_angle_rad': auto_angle})

    # --- workpiece
    workpiece_jo = find_workpiece_origin(design, {'workpiece_token': workpiece_token}) or workpiece_jo
    wp_occ = workpiece_jo.assemblyContext
    if not is_hanging():
        _release_workpiece(wp_occ, msgs)  # OP2: the part stays put; the stack comes to it
    x_expr = _offset_expr(auto_x_expr, x_offset_cm)
    y_expr = _offset_expr(auto_y_expr, y_offset_cm)
    try:
        joint = _add_rigid_joint(root, workpiece_jo, mount_jo, station_name(JOINT_NAME), ROLE_JOINT,
                                 angle_rad + auto_angle, flip, z_cm=auto_z_cm + z_offset_cm,
                                 x_expr=x_expr, y_expr=y_expr)
    except FixtureError as ex:
        others = [j.name for j in wp_occ.joints] + [j.name for j in wp_occ.asBuiltJoints]
        others = [n for n in others if not n.startswith(JOINT_NAME)]
        hint = ('\nOther joints already hold the workpiece: ' + ', '.join(others)) if others else ''
        raise FixtureError(str(ex) + hint)
    if is_vise:
        new_state['auto_x_expr'], new_state['auto_y_expr'] = _center_workpiece(
            design, joint, workpiece_token, new_state['jaws'], auto_x_expr, auto_y_expr,
            x_offset_cm, y_offset_cm)
    new_state['shift'] = _link_stock_shift(design, joint, workpiece_token, fixture_occ_name, msgs)
    msgs.append('Mounted {} on {} / {}'.format(wp_occ.name, fixture_occ.name, mount_label))

    new_state['workpiece_token'] = workpiece_jo.entityToken
    save_state(design, new_state)

    for name in [plate_occ_name, fixture_occ_name] + [o for o in (new_state.get('jaws') or {}).get('occs', [])]:
        loose = _loose_children(_child(container, name))
        if loose:
            msgs.append('WARNING: in "{}", {} did not move with the file\'s joint origins, so the model '
                        'shows in the wrong place. Open that file, right-click {} > Ground to Parent '
                        '(or joint it to the rest), save, and Apply again.'.format(
                            name, ', '.join(loose), 'it' if len(loose) == 1 else 'each'))

    # CAM model selections made in the same command that created the jaws come back as "missing
    # selections" when the setup generates, so the re-link runs after this command: the dialog
    # fires an event for it (scripts call run_pending_relinks() in a separate call).
    if stock_built and not jaw_setups and keep_jaws:
        # Kept jaws that just got their stock: the setups machining them get it as stock.
        jaw_setups = _jaw_setups(doc, find_container(design), new_state['jaws']['occs'])
    if jaw_setups and (not keep_jaws or stock_built) and is_vise and jaws['entry'].get('kind') == jm.KIND_SOFT_JAW:
        _pending_relinks.append((_station[0], jaw_setups, list(new_state['jaws']['occs']), stock_names))
        msgs.append('Setups {} will be pointed at the new jaws and their stock.'.format(
            ', '.join('"{}"'.format(n) for n in jaw_setups)))

    if update_setups or regenerate:
        msgs.extend(update_cam(doc, container, update_setups, regenerate))
    return msgs


def _loose_children(occ, tol_cm=0.001):
    """Child components of a referenced file that stayed behind when the joints moved it: free
    (not grounded to parent, not jointed) children with no FM_ joint origins of their own. A
    joint origin in the file's top level then moves without the body it describes. (Leftover
    unnamed joint origins in the child don't count: the add-in never joints to them.)"""
    if occ is None:
        return []
    out = []
    for child in occ.childOccurrences:
        try:
            if any(jo.name.upper().startswith('FM_') for jo in child.component.allJointOrigins):
                continue  # its own FM_ joint origins drive it (e.g. a jaw body inside its file)
            want = child.nativeObject.transform2.copy()
            want.transformBy(occ.transform2)
            a, b = want.translation, child.transform2.translation
            if ((a.x - b.x) ** 2 + (a.y - b.y) ** 2 + (a.z - b.z) ** 2) ** 0.5 > tol_cm:
                out.append(child.name)
        except Exception:
            continue
    return out


def _mount_jaws(design, vise_entry, vise_occ, jaw_occs, jaws, kept, old_jaws, msgs, seat_occ=None):
    """Joint each jaw to its seat at the chosen position (2 on a vise, N on a chuck) and return
    the workpiece mount: (mount joint origin, auto X expression, auto Z cm, jaw state).
    `seat_occ` holds the jaw seats when they aren't on the vise itself (a chuck's seat carrier)."""
    seat_occ = seat_occ or vise_occ
    root = design.rootComponent
    jaw_entry = jaws['entry']
    soft = jaw_entry.get('kind') == jm.KIND_SOFT_JAW
    reversed_ = bool(jaws.get('reversed')) and not soft
    seat_choice = jaws.get('seat', jm.SEAT_STEP)
    grip_name = jm.grip_for(reversed_, seat_choice)

    pos = jaws['position']
    seat_map = jm.jaw_seats([k for k, _ in collect_joint_origins(occ=seat_occ)]).get(pos)
    if not seat_map:
        raise FixtureError('Vise "{}" has no complete set of jaw seats for position {}.'.format(
            vise_entry.get('name'), pos))
    seat_keys = jm.seat_list(seat_map)
    if len(seat_keys) != len(jaw_occs):
        raise FixtureError('Position {} has {} jaw seats but {} jaws are in place.'.format(
            pos, len(seat_keys), len(jaw_occs)))
    seats = [find_mount(seat_occ, k) for k in seat_keys]
    seat_name = seat_occ.name
    center = _find_named(vise_occ, jm.CENTER)
    if center is None:
        raise FixtureError('Vise "{}" has no {} joint origin.'.format(vise_entry.get('name'), jm.CENTER))

    bolts = [_find_named(o, jm.BOLT) for o in jaw_occs]
    grips = [_find_named(o, grip_name) for o in jaw_occs]
    if not all(bolts) or not all(grips):
        raise FixtureError('Jaw "{}" needs {} and {} joint origins for this seat{}.'.format(
            jaw_entry.get('name'), jm.BOLT, grip_name, ' (reversed)' if reversed_ else ''))

    # Grip face position along the seat X axis once bolted on (before any slide). Soft jaws
    # use the blank's registered grip so cutting or moving FM_Grip never changes the opening.
    if soft:
        blank = (jaw_entry.get('metadata', {}).get('grip_local_cm') or {}).get(grip_name)
        grip_local = blank or list(_rel(bolts[0], grips[0]))
        normal_local = grip_local
    else:
        grip_local = list(_rel(bolts[0], grips[0]))
        normal_grip = _find_named(jaw_occs[0], jm.GRIP)
        normal_local = list(_rel(bolts[0], normal_grip)) if normal_grip else grip_local
    # The jaw faces from FM_Bolt toward FM_Grip, whatever way FM_Bolt itself is turned. The
    # jaw is turned so that direction points at the vise center (plus 180 deg when reversed).
    forward = jm.forward_angle(normal_local)
    grip_seated = jm.seated(grip_local, forward, reversed_)
    grip_x = grip_seated[0]

    width_cm = jaws['width_cm']
    width_expr = None if soft else jaws.get('width_expr')  # hard jaws: linked to the stock
    param = station_name(jm.JAW_OPENING_PARAM if soft else jm.GRIP_WIDTH_PARAM)
    _set_param(design, param, width_cm,
               'Fixture Manager: soft jaw opening' if soft else 'Fixture Manager: stock width across the jaws',
               expression=width_expr)

    clamping = vise_entry.get('clamping', jm.CLAMP_SELF_CENTERING)
    turn = -forward + (math.pi if reversed_ else 0.0)
    exprs = []
    if clamping == jm.CLAMP_FIXED_JAW:
        if len(seats) != 2:
            raise FixtureError('Fixed-jaw clamping needs exactly 2 jaw seats; position {} has {}. Register '
                               '"{}" as self-centering.'.format(pos, len(seats), vise_entry.get('name')))
        exprs.append(None)  # jaw 1 is the fixed jaw: no slide
        # Jaw 1's grip face (it sits exactly on seat 1), seen from seat 2. Jaw 2 closes to one
        # stock width short of it.
        q = _point_in_frame(seats[1], _seat_point(seats[0], grip_seated))
        exprs.append(jm.mm_expr(q[0] - grip_x, param))
    else:
        for seat in seats:
            cx = _rel(seat, center)[0]
            exprs.append(jm.mm_expr(cx - grip_x, param, 2))

    jaw_names = [o.name for o in jaw_occs]
    jaw_joints = []
    for n, (bolt, seat, expr) in enumerate(zip(bolts, seats, exprs), 1):
        jaw_joints.append(_add_rigid_joint(root, bolt, seat, station_name(JAW_JOINT_NAME.format(n)),
                                           ROLE_JAW_JOINT, angle_rad=turn, x_expr=expr))
    # Fusion's joint angle direction isn't documented: measure it once, then set the turn.
    container = find_container(design)
    sense = _joint_angle_sense(jaw_joints[0], _find_named(_child(container, jaw_names[0]), jm.BOLT),
                               find_mount(_child(container, seat_name), seat_keys[0]), turn)
    for joint in jaw_joints:
        joint.angle.value = sense * turn
    if not _jaw_turn_ok(design, seat_name, jaw_names, seat_keys, grip_local, grip_seated):
        raise FixtureError('Could not turn the jaws to face the vise center. Check that FM_Bolt and '
                           'FM_Grip both have Z pointing up.')
    check = (design, seat_name, jaw_names, seat_keys, exprs)
    if not _jaw_slides_ok(*check):
        # Fusion's joint X offset runs the other way for this geometry: negate and re-check.
        for joint, expr in zip(jaw_joints, exprs):
            if expr:
                joint.offsetX.expression = '-({})'.format(expr)
        if not _jaw_slides_ok(*check):
            raise FixtureError('The jaws did not close to {:.3f} mm. Check that each FM_JawSeat X '
                               'axis points toward the vise center and that the jaw\'s grip X axis '
                               'points out of the grip face.'.format(width_cm * 10.0))

    grip1 = _find_named(_child(find_container(design), jaw_occs[0].name), grip_name)
    auto_z = 0.0
    if seat_choice == jm.SEAT_BASE_TOP:
        base_top = _find_named(vise_occ, jm.BASE_TOP)
        if base_top is None:
            raise FixtureError('Vise "{}" has no {} joint origin for the base-top seat.'.format(
                vise_entry.get('name'), jm.BASE_TOP))
        auto_z = _origin_of(base_top).z - _origin_of(grip1).z

    # Workpiece center sits half the width in from jaw 1's grip face, measured in the
    # into-the-part direction. For soft jaws, undo any move of FM_Grip toward the part since
    # the blank, so the part stays centered between the jaw faces.
    half = '{} / 2'.format(param)
    if soft and not reversed_:
        bolt1 = _find_named(_child(find_container(design), jaw_names[0]), jm.BOLT)
        now = _rel(bolt1, grip1)
        # How far FM_Grip moved toward the part since the blank, along the jaw's facing.
        moved = ((now[0] - grip_local[0]) * math.cos(forward)
                 + (now[1] - grip_local[1]) * math.sin(forward))
        if abs(moved) > 1e-6:
            half = '{} / 2 {} {:.4f} mm'.format(param, '-' if moved > 0 else '+', abs(moved) * 10.0)
    # FM_Grip's own X axis may not point into the part (its rotation shouldn't matter, like
    # FM_Bolt's). delta is how far the into-the-part direction is turned from it.
    bolt1 = _find_named(_child(find_container(design), jaw_names[0]), jm.BOLT)
    into = forward + (math.pi if reversed_ else 0.0)
    delta = jm.wrap(into - _axis_angle_in(bolt1, grip1))
    auto_x, auto_y = jm.mount_offset_exprs(half, delta)
    msgs.append('Jaws: {} at position {}{}, {} {:.3f} mm{}, seat on {}.'.format(
        jaw_entry.get('name', ''), pos, ' (reversed)' if reversed_ else '',
        'opening' if soft else 'flush on the stock at' if width_expr else 'grip width', width_cm * 10.0,
        ' (follows {})'.format(width_expr) if width_expr and not width_expr.endswith('mm') else '',
        jm.BASE_TOP if seat_choice == jm.SEAT_BASE_TOP else grip_name))
    return grip1, auto_x, auto_y, auto_z, sense * (delta + jm.STOCK_ACROSS_TURN), {
        'urn': jaw_entry['urn'], 'name': jaw_entry.get('name', ''), 'kind': jaw_entry.get('kind'),
        'position': pos, 'reversed': reversed_, 'seat': seat_choice, 'grip': grip_name,
        'width_cm': width_cm, 'width_expr': width_expr, 'face_local_cm': list(grip_local),
        # Which registration the soft jaw copies came from; kept jaws keep their original one.
        'blank_version': old_jaws.get('blank_version') if kept else jm.blank_version(jaw_entry),
    }


def _center_workpiece(design, joint, workpiece_token, jaw_state, auto_x, auto_y, x_off, y_off):
    """With no user X/Y offset the workpiece origin must sit halfway between the jaw faces (in
    plan). If it doesn't, the joint's offsets run the other way: negate the automatic part and
    check again. Returns the automatic (X, Y) expressions actually in use."""
    def off_center():
        wp = find_workpiece_origin(design, {'workpiece_token': workpiece_token})
        # Jaw faces from each bolt and the registered face position (a cut soft jaw's FM_Grip
        # may have moved, so it can't be used here).
        container = find_container(design)
        g = []
        for name in jaw_state['occs']:
            bolt = _find_named(_child(container, name), jm.BOLT)
            g.append(_seat_point(bolt, tuple(jaw_state['face_local_cm'])))
        # The middle of all the jaw faces: halfway between two vise jaws, the axis of a chuck.
        mid = adsk.core.Point3D.create(sum(p.x for p in g) / len(g), sum(p.y for p in g) / len(g), 0)
        p = _origin_of(wp)
        return math.hypot(p.x - mid.x, p.y - mid.y)
    expected = math.hypot(x_off, y_off)
    if abs(off_center() - expected) < 0.001:
        return auto_x, auto_y
    flipped = tuple('-({})'.format(e) if e else None for e in (auto_x, auto_y))
    if flipped[0]:
        joint.offsetX.expression = _offset_expr(flipped[0], x_off)
    if flipped[1]:
        joint.offsetY.expression = _offset_expr(flipped[1], y_off)
    if abs(off_center() - expected) < 0.001:
        return flipped
    raise FixtureError('Could not center the workpiece between the jaws. Check that FM_Grip has '
                       'Z pointing up and sits on the grip face.')


def _seat_point(seat_jo, local):
    p = adsk.core.Point3D.create(*local)
    p.transformBy(seat_jo.transform)
    return p


def _grip_points(design, jaw_occs, grip_name):
    container = find_container(design)
    return [_origin_of(_find_named(_child(container, o.name), grip_name)) for o in jaw_occs]


def _jaw_turn_ok(design, vise_name, jaw_names, seat_keys, grip_local, grip_seated):
    """Each jaw must face the way we planned: seen from its seat, the bolt-to-grip vector has to
    match `grip_seated`. Slide doesn't affect this vector. `grip_local` is the grip in the bolt
    frame as planned (the blank's, for soft jaws), so a moved FM_Grip can't fool the check."""
    container = find_container(design)
    vise = _child(container, vise_name)
    for name, seat_key in zip(jaw_names, seat_keys):
        seat = find_mount(vise, seat_key)
        bolt = _find_named(_child(container, name), jm.BOLT)
        b = _rel(seat, bolt)
        g = _point_in_frame(seat, _seat_point(bolt, tuple(grip_local)))
        if abs(g[0] - b[0] - grip_seated[0]) > 0.001 or abs(g[1] - b[1] - grip_seated[1]) > 0.001:
            return False
    return True


def _jaw_slides_ok(design, vise_name, jaw_names, seat_keys, exprs):
    """Check each jaw's bolt sits its intended slide along its seat's X axis. Fusion's joint
    offset direction is the one thing the math can't know in advance, so verify rather than
    assume. (Bolts, not grips: a cut soft jaw's FM_Grip no longer marks the jaw face.)"""
    container = find_container(design)
    vise = _child(container, vise_name)
    for name, seat_key, expr in zip(jaw_names, seat_keys, exprs):
        bolt = _find_named(_child(container, name), jm.BOLT)
        seat = find_mount(vise, seat_key)
        want = _eval_cm(design, expr) if expr else 0.0
        x, y, z = _rel(seat, bolt)
        if abs(x - want) > 0.001 or abs(y) > 0.001 or abs(z) > 0.001:
            return False
    return True


def _top_level(occ):
    """The root-level occurrence that contains `occ`."""
    path = occ.fullPathName.split('+')
    return occ.component.parentDesign.rootComponent.occurrences.itemByName(path[0]) or occ


def _remove_fixture():
    """Remove the fixture and jaws but keep the (now empty) container, so setups stay
    pointed at it."""
    _, design = active_design()
    msgs = []
    state = load_state(design)
    _remove_managed_joints(design)
    for occ in find_jaw_occurrences(design, state):
        msgs.append('Removed jaw: ' + occ.name)
        occ.deleteMe()
    occ = find_fixture_occurrence(design, state)
    if occ:
        msgs.append('Removed fixture: ' + occ.name)
        occ.deleteMe()
    plate_occ = _child(find_container(design), (state.get('plate') or {}).get('occ'))
    if plate_occ:
        msgs.append('Removed plate: ' + plate_occ.name)
        plate_occ.deleteMe()
    seats = _find_seats(find_container(design))
    if seats:
        seats.deleteMe()
    for occ in _jaw_stock_occurrences(find_container(design)):
        occ.deleteMe()
    for key in ('fixture_urn', 'fixture_name', 'fixture_occ', 'mount', 'jaws', 'plate',
                'auto_x_expr', 'auto_z_cm'):
        state.pop(key, None)
    save_state(design, state)
    return msgs


# ------------------------------------------------------------------ CAM

def update_cam(doc, container, update_setups=True, regenerate=False):
    """Make every milling setup use the fixture container. Setups that already do are left
    alone, so after the first apply a swap does not touch CAM at all."""
    msgs = []
    cam = _cam_of(doc)
    if not cam:
        return ['No Manufacture data in this document yet; setups not touched.']
    if update_setups and container is not None:
        setups = [st for st in cam.setups if SKIP_SETUP_TAG not in st.name]
        tagged = [st for st in setups if jm.setup_station(st.name)]
        for setup in setups:
            # Setups are linked to a station by name ("Op 1", "OP2", ...). A file whose setups
            # carry no station tag at all keeps the old behaviour: every setup uses OP1.
            want = jm.setup_station(setup.name) or ('OP1' if not tagged else None)
            if want != _station[0]:
                continue
            try:
                if setup.operationType != adsk.cam.OperationTypes.MillingOperation:
                    continue
            except Exception:
                pass
            try:
                old = setup.fixtures
                items = [old.item(i) for i in range(old.count)] if old else []
                if setup.fixtureEnabled and any(_same(i, container) for i in items):
                    continue
                coll = adsk.core.ObjectCollection.create()
                # Keep anything else the programmer added (clamps, soft jaws, ...).
                for item in items:
                    try:
                        keep = (item.isValid and not _same(item, container)
                                and _role(item) not in (ROLE_FIXTURE, ROLE_CONTAINER))
                    except Exception:
                        keep = False
                    if keep:
                        coll.add(item)
                coll.add(container)
                setup.fixtureEnabled = True
                setup.fixtures = coll
                msgs.append('Setup "{}": now uses the "{}" container'.format(setup.name, container.component.name))
            except Exception as ex:
                msgs.append('Setup "{}": could not set fixture ({})'.format(setup.name, ex))
    if regenerate:
        try:
            cam.generateAllToolpaths(False)
            msgs.append('Toolpath regeneration started.')
        except Exception as ex:
            msgs.append('Could not start toolpath regeneration: {}'.format(ex))
    return msgs


def _cam_of(doc):
    """The document's CAM product, or None if it was never opened in Manufacture. Looked up by
    walking the products: itemByProductType raises for a missing product, and inside some
    commands that error aborts the whole command even when it's caught."""
    for i in range(doc.products.count):
        product = doc.products.item(i)
        if product.productType == 'CAMProductType':
            return adsk.cam.CAM.cast(product)
    return None


def _jaw_setups(doc, container, jaw_names):
    """Names of setups that machine these jaws (their models are the jaw bodies), found before
    the jaws are replaced. A setup named "...soft jaw..." with no models left counts too: an
    earlier Apply replaced its jaws."""
    cam = _cam_of(doc)
    if not cam:
        return []
    tails = ['{}+{}'.format(container.name, n) for n in jaw_names]
    out = []
    for setup in cam.setups:
        try:
            models = setup.models
            items = [models.item(i) for i in range(models.count)]
            paths = []
            for m in items:
                occ = m if isinstance(m, adsk.fusion.Occurrence) else m.assemblyContext
                paths.append(occ.fullPathName if occ else '')
            uses = any(p.endswith(t) for p in paths for t in tails)
            # Deleting the jaws empties the model list (or leaves invalid entries).
            broken = not items or any(not m.isValid for m in items)
        except Exception:
            uses, broken = False, True
        if uses or (broken and 'soft jaw' in setup.name.lower()):
            out.append(setup.name)
    return out


_pending_relinks = []  # (station, setup names, jaw occurrence names) left by the last apply


def has_pending_relinks():
    return bool(_pending_relinks)


def run_pending_relinks():
    """Point setups at the soft jaws the last apply created. Must run outside that apply's
    command. Returns messages."""
    msgs = []
    try:
        doc, design = active_design()
    except FixtureError:
        del _pending_relinks[:]
        return msgs
    while _pending_relinks:
        station, setup_names, jaw_names, stock_names = _pending_relinks.pop(0)
        with use_station(station):
            container = find_container(design)
        if container is not None:
            _relink_jaw_setups(doc, container, setup_names, jaw_names, msgs, stock_names)
    return msgs


def _relink_jaw_setups(doc, container, setup_names, jaw_names, msgs, stock_names=()):
    """Point those setups' models at the new jaw copies, and their stock at the uncut jaw stock
    (stock mode "From solid") when there is some."""
    cam = _cam_of(doc)
    if not cam:
        return
    try:
        cam.checkValidity()  # sync CAM with the design first, so it lets go of the deleted jaws
    except Exception:
        pass
    for name in setup_names:
        setup = cam.setups.itemByName(name)
        if setup is None:
            continue
        coll = adsk.core.ObjectCollection.create()
        for n in jaw_names:
            occ = _child(container, n)
            if occ is not None:
                coll.add(occ)
        try:
            # The new jaws have the old ones' names, so the API already resolves the stale models to
            # them and setting the same list is a no-op that leaves CAM with "missing selections".
            # Clearing first forces a real re-selection.
            setup.models = adsk.core.ObjectCollection.create()
            setup.models = coll
            msgs.append('Setup "{}": models now the new jaws ({}).'.format(name, ', '.join(jaw_names)))
        except Exception as ex:
            msgs.append('Setup "{}": could not re-point its models at the new jaws ({}); pick them '
                        'by hand.'.format(name, ex))
        if not stock_names:
            continue
        stock = adsk.core.ObjectCollection.create()
        for n in stock_names:
            occ = _child(container, n)
            if occ is not None:
                stock.add(occ)
        try:
            setup.parameters.itemByName('job_stockMode').expression = "'solid'"
            setup.stockSolids = adsk.core.ObjectCollection.create()
            setup.stockSolids = stock
            msgs.append('Setup "{}": stock is now the uncut jaw stock ({}).'.format(name, ', '.join(stock_names)))
        except Exception as ex:
            msgs.append('Setup "{}": could not set its stock to the jaw stock ({}); pick it by hand.'.format(
                name, ex))


def _same(a, b):
    # Occurrences read back from Setup.fixtures carry a different entityToken than the same
    # occurrence read from the design, and their path is wrapped by the CAM model
    # ("CAM Component:1+<doc>:1+FM Fixture:1"), so compare the path's tail.
    try:
        if a.entityToken == b.entityToken:
            return True
    except Exception:
        pass
    try:
        pa, pb = a.fullPathName, b.fullPathName
        return pa == pb or pa.endswith('+' + pb) or pb.endswith('+' + pa)
    except Exception:
        return False


# ------------------------------------------------------------------ misc

def _status_text(design):
    state = load_state(design)
    occ = find_fixture_occurrence(design, state)
    if not occ:
        return 'No managed fixture in this design.'
    um = design.unitsManager
    fmt = lambda v: um.formatInternalValue(v, um.defaultLengthUnits, True)
    off = _current_offsets(design, state)
    lines = ['Fixture: ' + (state.get('fixture_name') or occ.name)]
    plate = state.get('plate')
    if plate:
        lines.append('Plate: {} at {} ({} mm), {} deg'.format(
            plate.get('name', ''), plate.get('position'), plate.get('pattern'), plate.get('rotation_deg', 0)))
    jaws = state.get('jaws')
    if jaws:
        lines.append('Jaws: {} at {}{}, seat on {}'.format(
            jaws.get('name', ''), jaws.get('position', '?'), ' (reversed)' if jaws.get('reversed') else '',
            jm.BASE_TOP if jaws.get('seat') == jm.SEAT_BASE_TOP else jaws.get('grip') or jm.GRIP))
    else:
        lines.append('Mount: ' + state.get('mount', '?'))
    lines.append('Offset X/Y/Z: {} / {} / {}'.format(fmt(off['x']), fmt(off['y']), fmt(off['z'])))
    lines.append('Rotation: {:.2f} deg'.format(math.degrees(off['angle'])))
    if not find_mount_joint(design):
        lines.append('WARNING: mount joint is missing - re-apply to fix.')
    return '\n'.join(lines)


def open_fixture(urn):
    app().documents.open(find_data_file(urn), True)


# ------------------------------------------------------------------ public, per station

def apply_fixture(*args, station='OP1', **kwargs):
    with use_station(station):
        return _apply_fixture(*args, **kwargs)


def remove_fixture(station='OP1'):
    with use_station(station):
        return _remove_fixture()


def status_text(design, station='OP1'):
    with use_station(station):
        return _status_text(design)


def current_offsets(design, state=None, station='OP1'):
    with use_station(station):
        return _current_offsets(design, state)
