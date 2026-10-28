"""Toolbar commands: Apply / Swap Fixture, Register Fixture, Fixture Library."""

import os
import traceback

import adsk.core
import adsk.fusion

from . import fusion_ops as ops
from . import jawmath as jm
from . import library
from . import updater

TAB_ID = 'FixtureManagerTab'
TAB_NAME = 'FIXTURES'
PANEL_ID = 'FixtureManagerPanel'
PANEL_NAME = 'Fixture Manager'
WORKSPACES = ['FusionSolidEnvironment', 'CAMEnvironment']
ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'resources')

NONE_LABEL = '(none - remove fixture)'
NO_PLATE = '(no plate)'

_handlers = []  # Fusion only keeps weak references to handlers.


def _app():
    return adsk.core.Application.get()


def _ui():
    return _app().userInterface


def _report_exception():
    _ui().messageBox('Fixture Manager error:\n\n' + traceback.format_exc())


def _show(msgs, title='Fixture Manager'):
    _ui().messageBox('\n'.join(msgs) if msgs else 'Done.', title)


# ------------------------------------------------------------------ handler plumbing

class _Created(adsk.core.CommandCreatedEventHandler):
    def __init__(self, cmd_obj):
        super().__init__()
        self.cmd_obj = cmd_obj

    def notify(self, args):
        try:
            cmd = args.command
            self.cmd_obj.inputs = cmd.commandInputs
            for event, cls, fn in ((cmd.execute, _CmdEvent, self.cmd_obj.on_execute),
                                   (cmd.inputChanged, _InputChanged, self.cmd_obj.on_input_changed),
                                   (cmd.validateInputs, _Validate, self.cmd_obj.on_validate),
                                   (cmd.destroy, _CmdEvent, self.cmd_obj.on_destroy)):
                h = cls(fn)
                event.add(h)
                self.cmd_obj.handlers.append(h)
            self.cmd_obj.on_create(cmd, cmd.commandInputs)
        except ops.FixtureError as ex:
            _ui().messageBox(str(ex), 'Fixture Manager')
        except Exception:
            _report_exception()


class _CmdEvent(adsk.core.CommandEventHandler):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def notify(self, args):
        try:
            self.fn(args)
        except ops.FixtureError as ex:
            _ui().messageBox(str(ex), 'Fixture Manager')
            self._fail(args, str(ex))
        except Exception:
            _report_exception()
            self._fail(args, 'Unexpected error')

    @staticmethod
    def _fail(args, message):
        # On execute, this makes Fusion abort the command and roll back its changes, so a
        # failed apply never leaves a half-mounted fixture behind.
        try:
            args.executeFailed = True
            args.executeFailedMessage = message
        except Exception:
            pass


class _InputChanged(adsk.core.InputChangedEventHandler):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def notify(self, args):
        try:
            self.fn(args)
        except Exception:
            _report_exception()


class _Validate(adsk.core.ValidateInputsEventHandler):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def notify(self, args):
        try:
            args.areInputsValid = bool(self.fn(args))
        except Exception:
            args.areInputsValid = False


class BaseCommand(object):
    cmd_id = ''
    name = ''
    tooltip = ''
    icon = ''

    def __init__(self):
        self.inputs = None
        self.handlers = []

    def on_create(self, cmd, inputs):
        pass

    def on_execute(self, args):
        pass

    def on_input_changed(self, args):
        pass

    def on_validate(self, args):
        return True

    def on_destroy(self, args):
        self.handlers = []


def _dropdown(inputs, input_id, label, items, selected=None):
    dd = inputs.addDropDownCommandInput(input_id, label, adsk.core.DropDownStyles.TextListDropDownStyle)
    for i, text in enumerate(items):
        dd.listItems.add(text, text == selected if selected is not None else i == 0)
    if items and not any(li.isSelected for li in dd.listItems):
        dd.listItems.item(0).isSelected = True
    return dd


def _selected_text(dd):
    return dd.selectedItem.name if dd.selectedItem else None


def _load_library():
    try:
        return library.FixtureLibrary()
    except Exception as ex:
        raise ops.FixtureError('Could not read the fixture library at\n{}\n\n{}'.format(
            library.library_path(), ex))


# ------------------------------------------------------------------ Apply / Swap

ORIENTATIONS = ['Normal', 'Reversed']


class ApplyFixtureCommand(BaseCommand):
    cmd_id = 'FM_ApplyFixture'
    name = 'Apply / Swap Fixture'
    tooltip = ('Insert a fixture or vise from the library (or swap the current one), choose jaws, '
               'and rigidly mount the workpiece. CAM setups are pointed at the fixture container.')
    icon = 'apply'

    def on_create(self, cmd, inputs):
        _, design = ops.active_design()
        self.design = design
        self.lib = _load_library()
        self.by_label = {library.FixtureLibrary.label(e): e for e in self.lib.mountables()}
        self.plate_by_label = {library.FixtureLibrary.label(e): e for e in self.lib.plates()}
        um = design.unitsManager
        cmd.setDialogMinimumSize(400, 460)
        zero = adsk.core.ValueInput.createByReal(0.0)

        _dropdown(inputs, 'station', 'Station', list(ops.STATIONS))
        inputs.addTextBoxCommandInput('status', 'Current', '', 5, True)
        sel = inputs.addSelectionInput('workpiece', 'Workpiece origin',
                                       'OP1: FM_Stock on the stock (part goes to the fixture). '
                                       'OP2 / OP3: FM_OP2 / FM_OP3 on the stock (the fixture comes to the part).')
        sel.addSelectionFilter('JointOrigins')
        sel.setSelectionLimits(0, 1)
        _dropdown(inputs, 'fixture', 'Fixture / vise', [NONE_LABEL] + list(self.by_label.keys()))
        _dropdown(inputs, 'mount', 'Stock mount point', [])
        _dropdown(inputs, 'plate', 'Zero point plate', [NO_PLATE] + list(self.plate_by_label))
        _dropdown(inputs, 'plate_pos', 'Plate position', [])
        _dropdown(inputs, 'plate_rot', 'Rotation on plate', ['{} deg'.format(r) for r in jm.ROTATIONS])
        _dropdown(inputs, 'jaws', 'Jaws', [])
        _dropdown(inputs, 'position', 'Jaw position', [])
        _dropdown(inputs, 'jaw_count', 'Number of jaws', [])
        _dropdown(inputs, 'orientation', 'Orientation', ORIENTATIONS)
        _dropdown(inputs, 'seat', 'Seat part on', [])
        # Two inputs, shown one at a time: a command input's label can't be changed later.
        inputs.addValueInput('width', 'Grip width', um.defaultLengthUnits, zero)
        inputs.addValueInput('opening', 'Jaw opening', um.defaultLengthUnits, zero)
        inputs.addBoolValueInput('new_blank', 'Start from new soft jaw blanks', True, '', False)
        inputs.addValueInput('xoffset', 'X offset', um.defaultLengthUnits, zero)
        inputs.addValueInput('yoffset', 'Y offset', um.defaultLengthUnits, zero)
        inputs.addValueInput('zoffset', 'Z offset', um.defaultLengthUnits, zero)
        inputs.addValueInput('angle', 'Rotation about Z', 'deg', zero)
        inputs.addBoolValueInput('flip', 'Flip', True, '', False)
        inputs.addBoolValueInput('setups', 'Point setups at container', True, '', True)
        inputs.addBoolValueInput('regen', 'Regenerate toolpaths', True, '', False)
        if not self.by_label:
            inputs.addTextBoxCommandInput('hint', '', 'Library is empty. Open a fixture design and '
                                          'use Register Fixture first.', 2, True)
        self._load_station('OP1')

    def _select(self, input_id, text):
        for li in self.inputs.itemById(input_id).listItems:
            if li.name == text:
                li.isSelected = True
                return

    def _load_station(self, station):
        """Fill the dialog from one station's saved settings."""
        self.station = station
        design, inp = self.design, self.inputs
        with ops.use_station(station):
            self.state = state = ops.load_state(design)
            off = ops.current_offsets(design, state, station=station)
            inp.itemById('status').text = ops.status_text(design, station=station)
            sel = inp.itemById('workpiece')
            sel.clearSelection()
            jo = ops.find_workpiece_origin(design, state)
            if jo:
                sel.addSelection(jo)
        self.jaw_state = state.get('jaws') or {}
        self.plate_state = state.get('plate') or {}
        cur = self.lib.get(state.get('fixture_urn', ''))
        labels = [li.name for li in inp.itemById('fixture').listItems]
        self._select('fixture', library.FixtureLibrary.label(cur) if cur else (labels[1] if len(labels) > 1 else NONE_LABEL))
        self._select('plate', next((l for l, e in self.plate_by_label.items()
                                    if e['urn'] == self.plate_state.get('urn')), NO_PLATE))
        self._select('plate_rot', '{} deg'.format(self.plate_state.get('rotation_deg', 0)))
        self._select('orientation', ORIENTATIONS[1] if self.jaw_state.get('reversed') else ORIENTATIONS[0])
        for key, input_id in (('x', 'xoffset'), ('y', 'yoffset'), ('z', 'zoffset'), ('angle', 'angle')):
            inp.itemById(input_id).value = off[key]
        inp.itemById('flip').value = off['flip']
        inp.itemById('new_blank').value = False
        self._refresh(initial=True)
        self._refresh_plate(initial=True)

    # --- helpers

    def _entry(self):
        return self.by_label.get(_selected_text(self.inputs.itemById('fixture')))

    def _plate_entry(self):
        return self.plate_by_label.get(_selected_text(self.inputs.itemById('plate')))

    def _plate_choice(self):
        """(position, pattern) chosen in the 'plate_pos' list, or None."""
        return getattr(self, 'plate_choice_by_label', {}).get(_selected_text(self.inputs.itemById('plate_pos')))

    def _refresh_plate(self, initial=False):
        entry, plate = self._entry(), self._plate_entry()
        choices = jm.plate_choices(plate.get('plate_positions') or {}, entry.get('stud_patterns') or []) \
            if (entry and plate) else []
        self.plate_choice_by_label = {'{} ({} mm)'.format(pos, pat): (pos, pat) for pos, pat in choices}
        pref = None
        if initial and self.plate_state:
            pref = '{} ({} mm)'.format(self.plate_state.get('position'), self.plate_state.get('pattern'))
        self._fill('plate_pos', list(self.plate_choice_by_label), pref)
        show = bool(entry)
        self.inputs.itemById('plate').isVisible = show
        self.inputs.itemById('plate_pos').isVisible = show and plate is not None
        self.inputs.itemById('plate_rot').isVisible = show and plate is not None

    def _jaw_entry(self):
        return getattr(self, 'jaw_by_label', {}).get(_selected_text(self.inputs.itemById('jaws')))

    def _is_vise(self):
        """Vises and centering chucks: both take jaws."""
        e = self._entry()
        return bool(e) and library.FixtureLibrary.kind(e) in jm.JAWED_KINDS

    def _is_chuck(self):
        e = self._entry()
        return bool(e) and library.FixtureLibrary.kind(e) == jm.KIND_CHUCK

    def _fill(self, input_id, items, preferred=None):
        dd = self.inputs.itemById(input_id)
        dd.listItems.clear()
        for t in items:
            dd.listItems.add(t, t == preferred)
        if items and not dd.selectedItem:
            dd.listItems.item(0).isSelected = True

    def _width_input(self):
        # One "Jaw opening" field for every station and jaw kind (the old separate "Grip width"
        # input stays hidden).
        return self.inputs.itemById('opening')

    def _follows_stock(self, jaw):
        """OP1 hard jaws always close flush on the stock: their opening isn't typed."""
        return self.station == 'OP1' and bool(jaw) and jaw.get('kind') != jm.KIND_SOFT_JAW

    def _default_width(self, jaw):
        """Keep the current value when the same kind of jaws stay on; otherwise start from the
        stock width (hard jaws) or the blank's cut opening (soft jaws). OP1 hard jaws always
        show the stock width they will be linked to."""
        soft = jaw.get('kind') == jm.KIND_SOFT_JAW
        sel = self.inputs.itemById('workpiece')
        jo = adsk.fusion.JointOrigin.cast(sel.selection(0).entity) if sel.selectionCount else None
        if self._follows_stock(jaw) and jo and jo.assemblyContext:
            return ops._hard_jaw_width(self.design, jo, self.inputs.itemById('angle').value)[1]
        with ops.use_station(self.station):
            param = ops.station_name(jm.JAW_OPENING_PARAM if soft else jm.GRIP_WIDTH_PARAM)
        p = self.design.userParameters.itemByName(param)
        if p and (self.jaw_state.get('kind') == jaw.get('kind')):
            return p.value
        if soft:
            return jaw.get('cut_opening_cm') or 0.0
        if jo and jo.assemblyContext:
            ex, ey = ops.workpiece_extents(jo)
            return jm.grip_width(ex, ey, self.inputs.itemById('angle').value)
        return 0.0

    def _refresh(self, initial=False, changed=None):
        entry = self._entry()
        vise = self._is_vise()
        mount = self.inputs.itemById('mount')
        mount.isVisible = bool(entry) and not vise
        if changed in (None, 'fixture') and entry and not vise:
            names = library.stock_mount_candidates(entry.get('mount_points') or []) or entry.get('mount_points') or []
            pref = self.state.get('mount') if initial else None
            default = entry.get('default_mount') if entry.get('default_mount') in names else None
            self._fill('mount', names, pref if pref in names else default)

        if changed in (None, 'fixture') and vise:
            jaws = self.lib.jaws_for(entry)
            self.jaw_by_label = {'{} ({})'.format(j.get('name'), 'soft' if j.get('kind') == jm.KIND_SOFT_JAW
                                                  else 'hard'): j for j in jaws}
            cur = next((l for l, j in self.jaw_by_label.items() if j['urn'] == self.jaw_state.get('urn')), None)
            self._fill('jaws', list(self.jaw_by_label), cur)
            positions = entry.get('positions') or []
            self._fill('position', positions,
                       self.jaw_state.get('position') if self.jaw_state.get('position') in positions else None)
            seats = jm.seat_options(library.FixtureLibrary.kind(entry))
            self._fill('seat', [label for label, _ in seats],
                       next((label for label, s in seats if s == self.jaw_state.get('seat')), None))
            if self._is_chuck():
                counts = [str(n) for n in (entry.get('jaw_counts') or jm.DEFAULT_JAW_COUNTS)]
                self._fill('jaw_count', counts, str(self.jaw_state.get('count') or ''))
        self.inputs.itemById('jaw_count').isVisible = self._is_chuck()
        jaw = self._jaw_entry() if vise else None
        soft = bool(jaw) and jaw.get('kind') == jm.KIND_SOFT_JAW
        for input_id in ('jaws', 'position', 'seat'):
            self.inputs.itemById(input_id).isVisible = vise
        self.inputs.itemById('width').isVisible = False
        opening = self.inputs.itemById('opening')
        opening.isVisible = vise
        # OP1 hard jaws: shown for reference, set from StockY when you click OK.
        opening.isEnabled = not self._follows_stock(jaw)
        self.inputs.itemById('orientation').isVisible = vise and bool(jaw) and not soft and bool(
            jaw.get('reversible'))
        new_blank = self.inputs.itemById('new_blank')
        new_blank.isVisible = vise and soft and self.jaw_state.get('urn') == (jaw or {}).get('urn')
        # The blank was re-registered since these jaws were copied: replacing them is the default.
        if new_blank.isVisible and changed in (None, 'fixture', 'jaws') and jm.blank_is_stale(self.jaw_state, jaw):
            new_blank.value = True
        width = self._width_input()
        if jaw and changed in (None, 'fixture', 'jaws', 'workpiece', 'angle'):
            if not (changed == 'angle' and soft):
                width.value = self._default_width(jaw)

    def on_input_changed(self, args):
        if args.input.id == 'station':
            self._load_station(_selected_text(args.input))
            return
        if args.input.id in ('fixture', 'jaws', 'workpiece', 'angle'):
            self._refresh(changed=args.input.id)
        if args.input.id in ('fixture', 'plate'):
            self._refresh_plate()

    def on_validate(self, args):
        entry = self._entry()
        if entry is None:
            return True  # remove
        if self.inputs.itemById('workpiece').selectionCount != 1:
            return False
        if self._plate_entry() is not None and self._plate_choice() is None:
            return False  # this vise has no stud pattern that fits the plate
        if self._is_vise():
            jaw = self._jaw_entry()
            # OP1 hard jaws close flush on the stock by themselves; every other opening is typed.
            return (jaw is not None
                    and self.inputs.itemById('position').selectedItem is not None
                    and (self._follows_stock(jaw) or self._width_input().value > 0))
        return self.inputs.itemById('mount').selectedItem is not None

    def on_execute(self, args):
        inp = self.inputs
        update_setups = inp.itemById('setups').value
        entry = self._entry()
        if entry is None:
            _show(ops.remove_fixture(station=self.station))
            return
        jaws = None
        if self._is_vise():
            jaws = {
                'entry': self._jaw_entry(),
                'position': _selected_text(inp.itemById('position')),
                'reversed': (inp.itemById('orientation').isVisible
                             and _selected_text(inp.itemById('orientation')) == ORIENTATIONS[1]),
                'seat': dict(jm.seat_options(library.FixtureLibrary.kind(entry)))[_selected_text(inp.itemById('seat'))],
                'width_cm': self._width_input().value,
                'new_blank': inp.itemById('new_blank').isVisible and inp.itemById('new_blank').value,
            }
            if self._is_chuck():
                jaws['count'] = int(_selected_text(inp.itemById('jaw_count')))
        plate = None
        if self._plate_entry() is not None:
            pos, pat = self._plate_choice()
            plate = {'entry': self._plate_entry(), 'position': pos, 'pattern': pat,
                     'rotation_deg': int(_selected_text(inp.itemById('plate_rot')).split()[0])}
        msgs = ops.apply_fixture(
            entry,
            _selected_text(inp.itemById('mount')),
            adsk.fusion.JointOrigin.cast(inp.itemById('workpiece').selection(0).entity),
            z_offset_cm=inp.itemById('zoffset').value,
            angle_rad=inp.itemById('angle').value,
            flip=inp.itemById('flip').value,
            update_setups=update_setups,
            regenerate=inp.itemById('regen').value,
            x_offset_cm=inp.itemById('xoffset').value,
            y_offset_cm=inp.itemById('yoffset').value,
            jaws=jaws, plate=plate, station=self.station)
        if ops.has_pending_relinks():
            _app().fireCustomEvent(AFTER_APPLY_EVENT)  # runs once this command has finished
        _show(msgs)


# ------------------------------------------------------------------ Register

CLAMPINGS = [('Self-centering', jm.CLAMP_SELF_CENTERING), ('Fixed jaw', jm.CLAMP_FIXED_JAW)]


class RegisterFixtureCommand(BaseCommand):
    cmd_id = 'FM_RegisterFixture'
    name = 'Register Fixture'
    tooltip = ('Add (or update) the active, saved fixture, vise or jaw design in the library. '
               'Its URN is the key; joint origins define how it mounts.')
    icon = 'register'

    def on_create(self, cmd, inputs):
        try:
            self.info = ops.inspect_fixture_document()
        except ops.FixtureError as ex:
            inputs.addTextBoxCommandInput('err', '', str(ex), 4, True)
            cmd.isOKButtonVisible = False
            self.info = None
            return
        self.lib = _load_library()
        existing = self.lib.get(self.info['urn']) or {}
        cmd.setDialogMinimumSize(400, 380)
        um = _app().activeProduct.unitsManager if _app().activeProduct else None
        self.um = um

        inputs.addTextBoxCommandInput('urn', 'URN', self.info['urn'], 2, True)
        inputs.addStringValueInput('name', 'Display name', existing.get('name', self.info['name']))
        cat = inputs.addStringValueInput('category', 'Category', existing.get('category', ''))
        cats = self.lib.categories()
        cat.tooltip = 'Existing: ' + (', '.join(cats) if cats else '(none yet)')

        kind = existing.get('kind') or self.info['kind_suggestion']
        _dropdown(inputs, 'kind', 'Type', [jm.KIND_LABELS[k] for k in jm.KINDS], jm.KIND_LABELS[kind])
        iface = inputs.addStringValueInput('iface', 'Jaw interface', existing.get('jaw_interface', ''))
        iface.tooltip = 'Same text on a vise and its jaws, e.g. "KSC3-80". Jaws are only offered for matching vises.'
        _dropdown(inputs, 'clamping', 'Clamping', [c[0] for c in CLAMPINGS],
                  CLAMPINGS[1][0] if existing.get('clamping') == jm.CLAMP_FIXED_JAW else CLAMPINGS[0][0])
        inputs.addValueInput('opening', 'Cut opening (spacer)', um.defaultLengthUnits if um else 'mm',
                             adsk.core.ValueInput.createByReal(existing.get('cut_opening_cm') or 0.0))
        counts = inputs.addStringValueInput('jaw_counts', 'Jaw counts', ', '.join(
            str(n) for n in (existing.get('jaw_counts') or jm.DEFAULT_JAW_COUNTS)))
        counts.tooltip = ('How many jaws this chuck can run, e.g. "2, 3, 6". Apply offers these and '
                          'spaces the jaws evenly from the main jaw (2 = 180 deg, 3 = 120 deg ...).')

        names = self.info['mount_points']
        # Only points the stock can sit on: FM_ZP_ studs, FM_PlateMount and the like are left out.
        candidates = library.stock_mount_candidates(names) or names
        default = existing.get('default_mount')
        default = default if default in candidates else library.pick_default_mount(names)
        mount_dd = _dropdown(inputs, 'default_mount', 'Stock mount point', candidates, default)
        mount_dd.tooltip = 'Where the bottom center of the stock sits on this fixture (usually FM_Mount).'
        rows = []
        for n in names:
            z = self.info['heights_cm'].get(n)
            if z is not None and um:
                rows.append('{}: Z = {}'.format(n, um.formatInternalValue(z, um.defaultLengthUnits, True)))
        inputs.addTextBoxCommandInput('heights', 'Joint origins', '\n'.join(rows) or '(none)',
                                      min(max(len(rows), 1), 8), True)
        inputs.addTextBoxCommandInput('problems', 'Check', '', 3, True)
        loose = inputs.addBoolValueInput('lock_loose', 'Lock loose components', True, '', True)
        loose.tooltip = ('These components hold bodies but aren\'t locked to the file or jointed, so they '
                         'would stay behind when the fixture is placed on a plate or at OP2/OP3. Ticked: '
                         'they are grounded to their parent and the file is saved as a new version.')
        loose.isVisible = bool(self.info['loose'])
        inputs.addTextBoxCommandInput('notes', 'Notes', existing.get('notes', ''), 3, False)
        self._refresh()

    def _kind(self):
        label = _selected_text(self.inputs.itemById('kind'))
        return next((k for k, l in jm.KIND_LABELS.items() if l == label), jm.KIND_FIXTURE)

    def _problems(self):
        kind = self._kind()
        problems = jm.validate(kind, self.info['mount_points'])
        if self.info['loose'] and not self.inputs.itemById('lock_loose').value:
            problems.append('{} not locked to the file, so it would be left behind when the fixture is '
                            'placed. Tick "Lock loose components", or Ground to Parent it yourself.'.format(
                                ', '.join(self.info['loose'])))
        if self.info.get('is_configured'):
            # Apply refuses to insert configured designs, so catch it here instead.
            problems.append('This is a configured design. Rebuild it as an ordinary design and '
                            'register that file.')
        if kind == jm.KIND_FIXTURE and not library.stock_mount_candidates(self.info['mount_points']):
            problems.append('No joint origins found. Add one named "{}" at the part seat.'.format(
                library.MOUNT_PREFIX))
        if kind not in (jm.KIND_FIXTURE, jm.KIND_PLATE) and not self.inputs.itemById('iface').value.strip():
            problems.append('Enter a jaw interface name.')
        if kind == jm.KIND_CHUCK and not jm.parse_jaw_counts(self.inputs.itemById('jaw_counts').value):
            problems.append('Enter the jaw counts this chuck can run, e.g. "2, 3, 6".')
        if kind == jm.KIND_SOFT_JAW and self.inputs.itemById('opening').value <= 0:
            problems.append('Enter the cut opening (the spacer width between the jaws).')
        return problems

    def _refresh(self):
        kind = self._kind()
        self.inputs.itemById('iface').isVisible = kind not in (jm.KIND_FIXTURE, jm.KIND_PLATE)
        self.inputs.itemById('clamping').isVisible = kind == jm.KIND_VISE
        self.inputs.itemById('opening').isVisible = kind == jm.KIND_SOFT_JAW
        self.inputs.itemById('jaw_counts').isVisible = kind == jm.KIND_CHUCK
        self.inputs.itemById('default_mount').isVisible = kind == jm.KIND_FIXTURE
        problems = self._problems()
        notes = []
        if kind == jm.KIND_VISE:
            notes.append('Positions found: ' + (', '.join(self.info['positions']) or 'none'))
        if kind == jm.KIND_CHUCK:
            notes.append('Main jaw positions: ' + (', '.join(self.info['chuck_positions']) or 'none'))
        if kind == jm.KIND_HARD_JAW:
            notes.append('Reversible: ' + ('yes' if self.info['reversible'] else 'no - add FM_GripRev'))
        if kind in (jm.KIND_HARD_JAW, jm.KIND_SOFT_JAW):
            notes.append('Grips: ' + ', '.join(jm.jaw_grips(self.info['mount_points'])))
        if kind == jm.KIND_PLATE:
            notes.append('Positions: ' + (', '.join('{} ({})'.format(p, '/'.join(pats))
                                                     for p, pats in self.info['plate_positions'].items()) or 'none'))
        if kind in (jm.KIND_VISE, jm.KIND_CHUCK, jm.KIND_FIXTURE):
            notes.append('Stud patterns: ' + (', '.join(self.info['stud_patterns'])
                                               or 'none (add FM_ZP_52 / FM_ZP_96 to mount on plates)'))
        self.inputs.itemById('problems').text = '\n'.join(problems + notes) or 'OK'

    def on_input_changed(self, args):
        if args.input.id in ('kind', 'iface', 'opening', 'jaw_counts', 'lock_loose'):
            self._refresh()

    def on_validate(self, args):
        return self.info is not None and not self._problems()

    def on_execute(self, args):
        if not self.info:
            return
        inp = self.inputs
        fixed = []
        if self.info['loose'] and inp.itemById('lock_loose').value:
            fixed = ops.lock_loose_components(self.info['loose'])
            # The save adds one version; Fusion's DataFile still reports the old number for a while.
            self.info['version'] += 1
        kind = self._kind()
        jaw_kind = kind in (jm.KIND_HARD_JAW, jm.KIND_SOFT_JAW)
        extra = {
            'kind': kind,
            'jaw_interface': (inp.itemById('iface').value.strip()
                              if kind not in (jm.KIND_FIXTURE, jm.KIND_PLATE) else None),
            'plate_positions': self.info['plate_positions'] if kind == jm.KIND_PLATE else None,
            'stud_patterns': ((self.info['stud_patterns'] or None)
                              if kind in (jm.KIND_VISE, jm.KIND_CHUCK, jm.KIND_FIXTURE) else None),
            'clamping': dict(CLAMPINGS)[_selected_text(inp.itemById('clamping'))] if kind == jm.KIND_VISE else None,
            'positions': (self.info['positions'] if kind == jm.KIND_VISE
                          else self.info['chuck_positions'] if kind == jm.KIND_CHUCK else None),
            'jaw_counts': (jm.parse_jaw_counts(inp.itemById('jaw_counts').value)
                           if kind == jm.KIND_CHUCK else None),
            'reversible': self.info['reversible'] if kind == jm.KIND_HARD_JAW else None,
            'cut_opening_cm': inp.itemById('opening').value if kind == jm.KIND_SOFT_JAW else None,
        }
        metadata = {'mount_heights_cm': self.info['heights_cm'], 'roles': self.info['roles']}
        if jaw_kind:
            metadata['grip_local_cm'] = self.info.get('grip_local_cm', {})
        entry = self.lib.upsert(
            self.info['urn'],
            name=inp.itemById('name').value,
            category=inp.itemById('category').value,
            notes=inp.itemById('notes').text,
            mount_points=self.info['mount_points'],
            default_mount=_selected_text(inp.itemById('default_mount')) if kind == jm.KIND_FIXTURE else '',
            metadata=metadata,
            source={'project': self.info['project'], 'file_name': self.info['name'],
                    'version_at_register': self.info['version']},
            extra=extra)
        self.lib.save()
        _show(fixed + ['Saved "{}" ({}) to\n{}'.format(entry['name'], jm.KIND_LABELS[kind], self.lib.path)])


# ------------------------------------------------------------------ Library

ACTIONS = ['Close (save library path)', 'Open fixture design', 'Remove from library',
           'Verify all URNs']


class LibraryCommand(BaseCommand):
    cmd_id = 'FM_Library'
    name = 'Fixture Library'
    tooltip = 'Browse, open, verify or remove fixtures, and set the library file location.'
    icon = 'library'

    def on_create(self, cmd, inputs):
        cmd.setDialogMinimumSize(420, 360)
        inputs.addStringValueInput('libpath', 'Library file', library.library_path())
        self._reload()
        _dropdown(inputs, 'fixture', 'Fixture', list(self.by_label.keys()) or ['(library empty)'])
        inputs.addTextBoxCommandInput('details', 'Details', '', 9, True)
        _dropdown(inputs, 'action', 'On OK', ACTIONS)
        self._refresh_details()

    def _reload(self, path=None):
        try:
            self.lib = library.FixtureLibrary(path)
        except Exception as ex:
            self.lib = None
            self.load_error = str(ex)
            self.by_label = {}
            return
        self.load_error = ''
        self.by_label = {library.FixtureLibrary.label(e): e for e in self.lib.sorted_entries()}

    def _entry(self):
        return self.by_label.get(_selected_text(self.inputs.itemById('fixture')))

    def _refresh_details(self):
        box = self.inputs.itemById('details')
        if self.load_error:
            box.text = 'Could not read library:\n' + self.load_error
            return
        e = self._entry()
        if not e:
            box.text = 'No fixtures yet. Library file:\n' + self.lib.path
            return
        units, factor = 'mm', 10.0
        prod = _app().activeProduct
        if prod and hasattr(prod, 'unitsManager'):
            units = prod.unitsManager.defaultLengthUnits
            factor = prod.unitsManager.convert(1.0, 'cm', units)
        box.text = library.describe(e, units, factor)

    def on_input_changed(self, args):
        if args.input.id == 'libpath':
            self._reload(os.path.expanduser(args.input.value.strip()))
            dd = self.inputs.itemById('fixture')
            dd.listItems.clear()
            labels = list(self.by_label.keys()) or ['(library empty)']
            for i, t in enumerate(labels):
                dd.listItems.add(t, i == 0)
        if args.input.id in ('fixture', 'libpath'):
            self._refresh_details()

    def on_execute(self, args):
        path = self.inputs.itemById('libpath').value.strip()
        if path and path != library.library_path():
            library.set_library_path(path)
        action = _selected_text(self.inputs.itemById('action'))
        entry = self._entry()
        if action == ACTIONS[1] and entry:
            ops.open_fixture(entry['urn'])
        elif action == ACTIONS[2] and entry and self.lib:
            if _ui().messageBox('Remove "{}" from the library?\n(The Fusion file is not touched.)'.format(
                    entry['name']), 'Fixture Manager', adsk.core.MessageBoxButtonTypes.YesNoButtonType
                    ) == adsk.core.DialogResults.DialogYes:
                self.lib.remove(entry['urn'])
                self.lib.save()
        elif action == ACTIONS[3] and self.lib:
            bad = []
            for e in self.lib.sorted_entries():
                try:
                    ops.find_data_file(e['urn'])
                except ops.FixtureError:
                    bad.append(e['name'])
            _show(['All {} fixtures resolve.'.format(len(self.lib.fixtures))] if not bad
                  else ['These fixtures no longer resolve:'] + bad)


# ------------------------------------------------------------------ Publish / Update

def _updater_call(fn, *args):
    try:
        return fn(*args)
    except updater.UpdateError as ex:
        raise ops.FixtureError(str(ex))


class PublishCommand(BaseCommand):
    cmd_id = 'FM_Publish'
    name = 'Publish Version'
    tooltip = 'Save the add-in as a new version on GitHub: bumps the version, commits, tags and pushes.'
    icon = 'publish'

    def on_create(self, cmd, inputs):
        cmd.setDialogMinimumSize(460, 320)
        cmd.okButtonText = 'Publish'
        self.info = updater.status(fetch=True)
        inputs.addTextBoxCommandInput('status', 'Status', updater.describe(self.info), 7, True)
        current = self.info['version']
        base = max([current, self.info.get('latest') or current], key=updater.parse_version)
        self.part_by_label = {'{} ({})'.format(p.capitalize(), updater.bump(base, p)): p
                              for p in updater.PARTS}
        _dropdown(inputs, 'part', 'New version', list(self.part_by_label))
        inputs.addTextBoxCommandInput('note', 'What changed', '', 3, False)

    def on_validate(self, args):
        info = self.info
        return (info['is_repo'] and not info['error'] and not info.get('behind')
                and bool(info.get('changed') or info.get('ahead'))
                and bool(self.inputs.itemById('note').text.strip()))

    def on_execute(self, args):
        part = self.part_by_label[_selected_text(self.inputs.itemById('part'))]
        _show(_updater_call(updater.publish, self.inputs.itemById('note').text, part))


class UpdateCommand(BaseCommand):
    cmd_id = 'FM_Update'
    name = 'Check for Updates'
    tooltip = 'Install the latest Fixture Manager version published on GitHub.'
    icon = 'update'

    def on_create(self, cmd, inputs):
        cmd.setDialogMinimumSize(460, 280)
        self.latest, notes, error = None, [], ''
        try:
            self.latest, notes = updater.available_update()
        except updater.UpdateError as ex:
            error = str(ex)
        current = updater.read_version()
        if error:
            text = 'Installed version: {}\n\n{}'.format(current, error)
        elif self.latest:
            text = 'Installed version: {}\nAvailable: {}\n\n{}'.format(current, self.latest, '\n'.join(notes))
            cmd.okButtonText = 'Install {}'.format(self.latest)
        else:
            text = 'Version {} is the latest published version.'.format(current)
        inputs.addTextBoxCommandInput('info', 'Updates', text, 8, True)

    def on_execute(self, args):
        if self.latest:
            _show(_updater_call(updater.install_update))


# ------------------------------------------------------------------ UI registration

COMMANDS = [ApplyFixtureCommand, RegisterFixtureCommand, LibraryCommand, PublishCommand, UpdateCommand]
AFTER_APPLY_EVENT = 'FixtureManager_AfterApply'
_live = []


class _AfterApply(adsk.core.CustomEventHandler):
    """Follow-up work that must run outside the Apply command (re-linking soft jaw setups)."""

    def notify(self, args):
        try:
            msgs = ops.run_pending_relinks()
            failed = [m for m in msgs if 'could not' in m]
            if failed:
                _show(failed)
        except Exception:
            _report_exception()


def start():
    ui = _ui()
    app = _app()
    try:
        app.unregisterCustomEvent(AFTER_APPLY_EVENT)
    except Exception:
        pass
    event = app.registerCustomEvent(AFTER_APPLY_EVENT)
    h = _AfterApply()
    event.add(h)
    _handlers.append(h)
    for cls in COMMANDS:
        cmd_obj = cls()
        cmd_def = ui.commandDefinitions.itemById(cls.cmd_id)
        if cmd_def:
            cmd_def.deleteMe()
        icon = os.path.join(ICON_DIR, cls.icon)
        cmd_def = ui.commandDefinitions.addButtonDefinition(
            cls.cmd_id, cls.name, cls.tooltip, icon if os.path.isdir(icon) else '')
        h = _Created(cmd_obj)
        cmd_def.commandCreated.add(h)
        _handlers.append(h)
        _live.append(cmd_obj)

    for ws_id in WORKSPACES:
        ws = ui.workspaces.itemById(ws_id)
        if not ws:
            continue
        tab = ws.toolbarTabs.itemById(TAB_ID) or ws.toolbarTabs.add(TAB_ID, TAB_NAME)
        panel = tab.toolbarPanels.itemById(PANEL_ID) or tab.toolbarPanels.add(PANEL_ID, PANEL_NAME)
        for cls in COMMANDS:
            if not panel.controls.itemById(cls.cmd_id):
                ctrl = panel.controls.addCommand(ui.commandDefinitions.itemById(cls.cmd_id))
                ctrl.isPromoted = ctrl.isPromotedByDefault = True


def stop():
    ui = _ui()
    for ws_id in WORKSPACES:
        ws = ui.workspaces.itemById(ws_id)
        if not ws:
            continue
        tab = ws.toolbarTabs.itemById(TAB_ID)
        if not tab:
            continue
        panel = tab.toolbarPanels.itemById(PANEL_ID)
        if panel:
            for cls in COMMANDS:
                ctrl = panel.controls.itemById(cls.cmd_id)
                if ctrl:
                    ctrl.deleteMe()
            panel.deleteMe()
        tab.deleteMe()
    for cls in COMMANDS:
        d = ui.commandDefinitions.itemById(cls.cmd_id)
        if d:
            d.deleteMe()
    try:
        _app().unregisterCustomEvent(AFTER_APPLY_EVENT)
    except Exception:
        pass
    del _handlers[:]
    del _live[:]
