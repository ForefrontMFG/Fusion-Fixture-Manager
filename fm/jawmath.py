"""Jaw naming and placement math, kept free of the Fusion API so it can be unit-tested.

Conventions (see README.md). All values are in cm, Fusion's internal unit.
- A vise jaw seat ``FM_JawSeat_<pos><n>`` has Z up and X pointing toward the vise center.
- A jaw's ``FM_Bolt`` mates with a seat. Its ``FM_Grip`` (and ``FM_GripRev`` for the reversed
  side) sits on the gripping edge, Z up. The jaw faces from FM_Bolt toward FM_Grip; neither
  origin's X axis matters.
- "Slide" is how far a jaw moves along its seat's X axis from the seat to clamp.
"""

import math
import re

KIND_FIXTURE = 'fixture'
KIND_VISE = 'vise'
KIND_HARD_JAW = 'hard_jaw'
KIND_SOFT_JAW = 'soft_jaw'
KIND_PLATE = 'plate'
KIND_CHUCK = 'chuck'
KINDS = [KIND_FIXTURE, KIND_VISE, KIND_CHUCK, KIND_HARD_JAW, KIND_SOFT_JAW, KIND_PLATE]
JAWED_KINDS = (KIND_VISE, KIND_CHUCK)
KIND_LABELS = {
    KIND_FIXTURE: 'Fixture (single mount point)',
    KIND_VISE: 'Vise (takes jaws)',
    KIND_CHUCK: 'Centering chuck (main jaw + jaw count)',
    KIND_HARD_JAW: 'Hard jaw',
    KIND_SOFT_JAW: 'Soft jaw blank',
    KIND_PLATE: 'Zero point plate',
}

CLAMP_SELF_CENTERING = 'self_centering'
CLAMP_FIXED_JAW = 'fixed_jaw'

# Where the part sits: on the jaw's grip 1, 2 or 3 (FM_Grip / FM_GripRev, then ...2, ...3), or on
# a vise's FM_BaseTop. Stored in each station's jaw state, so the values never change.
SEAT_STEP = 'step'
SEAT_STEP2 = 'step2'
SEAT_STEP3 = 'step3'
SEAT_BASE_TOP = 'base_top'

CENTER = 'FM_Center'
BASE_TOP = 'FM_BaseTop'
BOLT = 'FM_Bolt'
GRIP = 'FM_Grip'
GRIP_REV = 'FM_GripRev'
GRIP_WIDTH_PARAM = 'FM_GripWidth'
JAW_OPENING_PARAM = 'FM_JawOpening'

PLATE_WCS = 'FM_WCS'
PLATE_MOUNT = 'FM_PlateMount'
ROTATIONS = (0, 90, 180, 270)

# Plate receivers: FM_ZP_<position>_<pattern>, e.g. FM_ZP_Center_96. Vise/fixture studs:
# FM_ZP_<pattern>, e.g. FM_ZP_52. All Z up; the receiver sits on the plate's top face and the
# stud on the vise's underside, so a joint between them seats the vise on the plate.
_RECEIVER_RE = re.compile(r'^FM_ZP_([A-Za-z0-9]+)_(\d+)$', re.IGNORECASE)
_STUD_RE = re.compile(r'^FM_ZP_(\d+)$', re.IGNORECASE)

_SEAT_RE = re.compile(r'^FM_JawSeat_([A-Za-z0-9]+?)_?(\d+)$', re.IGNORECASE)


def _leaf(key):
    return key.rsplit('/', 1)[-1]


def find_key(keys, name):
    """The key whose joint origin is named `name` (case-insensitive), preferring the shallowest."""
    hits = [k for k in keys if _leaf(k).lower() == name.lower()]
    return min(hits, key=lambda k: k.count('/')) if hits else None


def jaw_seats(keys):
    """{position: {1: key, 2: key, ...}} for every position whose seats run 1..N with no gap
    (N >= 2: a vise has 2, a 6-jaw chuck 6), in name order."""
    found = {}
    for k in keys:
        m = _SEAT_RE.match(_leaf(k))
        if m:
            found.setdefault(m.group(1).upper(), {})[int(m.group(2))] = k
    return {pos: seats for pos, seats in sorted(found.items())
            if len(seats) >= 2 and set(seats) == set(range(1, len(seats) + 1))}


def seat_list(seats):
    """A position's seat keys in jaw order (seat 1 first)."""
    return [seats[n] for n in sorted(seats)]


# A centering chuck has one main seat per position (FM_JawSeat_A1); Apply spaces the chosen
# number of jaws evenly around the chuck axis from it.
DEFAULT_JAW_COUNTS = (2, 3, 4, 6)


def main_seats(keys):
    """{position: key of seat 1} for a chuck's main jaw seats, in name order."""
    found = {}
    for k in keys:
        m = _SEAT_RE.match(_leaf(k))
        if m and int(m.group(2)) == 1:
            found.setdefault(m.group(1).upper(), k)
    return dict(sorted(found.items()))


def jaw_angles(count):
    """Angles (radians) of each jaw around the axis from the main jaw: 2 jaws 180 deg apart,
    3 jaws 120 deg, and so on."""
    if count < 2:
        raise ValueError('A chuck needs at least 2 jaws.')
    return [2 * math.pi * k / count for k in range(count)]


def parse_jaw_counts(text):
    """'2, 3, 6' -> [2, 3, 6]. Ignores anything that isn't a whole number of 2 or more."""
    out = set()
    for part in re.split(r'[\s,;]+', text or ''):
        if part.isdigit() and int(part) >= 2:
            out.add(int(part))
    return sorted(out)


def plate_positions(keys):
    """{position: {pattern: key}} for a plate's receivers, positions in name order."""
    out = {}
    for k in keys:
        m = _RECEIVER_RE.match(_leaf(k))
        if m:
            out.setdefault(m.group(1), {})[m.group(2)] = k
    return dict(sorted(out.items()))


def stud_patterns(keys):
    """{pattern: key} for a vise's or fixture's stud origins."""
    out = {}
    for k in keys:
        m = _STUD_RE.match(_leaf(k))
        if m:
            out.setdefault(m.group(1), k)
    return dict(sorted(out.items(), key=lambda kv: int(kv[0])))


def plate_choices(plate_pos, studs):
    """[(position, pattern)] the vise can use on the plate: its stud pattern must match the
    receiver's. Bigger patterns first within a position."""
    return [(pos, pat) for pos, pats in plate_pos.items()
            for pat in sorted(pats, key=int, reverse=True) if pat in studs]


def suggest_kind(keys):
    if jaw_seats(keys):
        return KIND_VISE
    if main_seats(keys) and find_key(keys, CENTER):
        return KIND_CHUCK
    if plate_positions(keys):
        return KIND_PLATE
    if find_key(keys, BOLT):
        return KIND_HARD_JAW
    return KIND_FIXTURE


def validate(kind, keys):
    """Problems that stop a file being registered as `kind` (empty list = fine)."""
    problems = []
    if kind == KIND_VISE:
        if not jaw_seats(keys):
            problems.append('No complete jaw seat pair. Add FM_JawSeat_A1 and FM_JawSeat_A2 '
                            '(Z up, X toward the vise center).')
        if not find_key(keys, CENTER):
            problems.append('No FM_Center joint origin at the vise center.')
    elif kind == KIND_CHUCK:
        if not main_seats(keys):
            problems.append('No main jaw seat. Add FM_JawSeat_A1 on one jaw slot (Z up, X toward the '
                            'chuck axis).')
        if not find_key(keys, CENTER):
            problems.append('No FM_Center joint origin on the chuck axis.')
    elif kind == KIND_PLATE:
        if not plate_positions(keys):
            problems.append('No receivers. Add joint origins named FM_ZP_<position>_<pattern> '
                            '(e.g. FM_ZP_Center_96) on the plate\'s top face, Z up.')
        if not find_key(keys, PLATE_WCS):
            problems.append('No FM_WCS joint origin at the top center of the plate.')
        if not find_key(keys, PLATE_MOUNT):
            problems.append('No FM_PlateMount joint origin on the plate\'s underside (where it sits '
                            'on the machine), Z up.')
    elif kind in (KIND_HARD_JAW, KIND_SOFT_JAW):
        if not find_key(keys, BOLT):
            problems.append('No FM_Bolt joint origin at the jaw\'s bolt interface.')
        if not find_key(keys, GRIP):
            problems.append('No FM_Grip joint origin on the gripping edge.')
    return problems


def is_reversible(keys):
    return find_key(keys, GRIP_REV) is not None


def seat_options(kind):
    """[(label, seat)] for the Apply dialog. A chuck's stepped jaws: FM_Grip / FM_GripRev sit on
    the chuck face, ...2 and ...3 on the first and second steps. A vise: its jaw step, or its
    FM_BaseTop."""
    if kind == KIND_CHUCK:
        return [('Chuck face', SEAT_STEP), ('Jaw step 1', SEAT_STEP2), ('Jaw step 2', SEAT_STEP3)]
    return [('Jaw step', SEAT_STEP), ('Top of jaw base', SEAT_BASE_TOP)]


def grip_for(reversed_, seat):
    """The jaw's grip joint origin for an orientation and seat: FM_Grip / FM_GripRev, or the
    ...2 / ...3 grips. On a vise's base top the first grip still sets where the jaws close; only
    the height comes from FM_BaseTop."""
    name = GRIP_REV if reversed_ else GRIP
    return name + {SEAT_STEP2: '2', SEAT_STEP3: '3'}.get(seat, '')


def jaw_grips(keys):
    """The grip joint origins a jaw has, in the order Register measures them."""
    return [g + n for g in (GRIP, GRIP_REV) for n in ('', '2', '3') if find_key(keys, g + n)]


def grip_in_seat_x(grip_local, reversed_):
    """X of the grip face in the seat frame once the jaw is bolted on with no slide.
    `grip_local` is the grip origin's position in the jaw's FM_Bolt frame. A reversed jaw is
    turned 180 degrees about Z, which negates X and Y."""
    return -grip_local[0] if reversed_ else grip_local[0]


def forward_angle(grip_local):
    """Which way a jaw faces, in its own FM_Bolt frame: the plan direction from FM_Bolt to
    FM_Grip. Using this instead of FM_Bolt's X axis means FM_Bolt's rotation doesn't matter."""
    x, y = grip_local[0], grip_local[1]
    return math.atan2(y, x) if math.hypot(x, y) > 1e-6 else 0.0


def rotate_z(local, angle):
    c, s = math.cos(angle), math.sin(angle)
    return (c * local[0] - s * local[1], s * local[0] + c * local[1], local[2])


def seated(local, forward, reversed_):
    """A point given in the jaw's FM_Bolt frame, expressed in the seat frame once the jaw is
    turned to face the vise center (and turned a further 180 degrees when reversed)."""
    return rotate_z(local, -forward + (math.pi if reversed_ else 0.0))


def wrap(angle):
    """Angle in (-pi, pi]."""
    a = math.fmod(angle + math.pi, 2 * math.pi)
    if a <= 0:
        a += 2 * math.pi
    return a - math.pi


def mount_offset_exprs(half_expr, delta):
    """X/Y joint offsets (expressions) that move the workpiece `half_expr` into the part, when
    the grip origin's X axis is turned `delta` away from the into-the-part direction. With no
    turn this is just (half_expr, None), the common case."""
    c, s = math.cos(delta), math.sin(delta)
    if abs(s) < 1e-9:
        return (half_expr if c > 0 else '-({})'.format(half_expr)), None
    x = None if abs(c) < 1e-9 else '({}) * {:.9f}'.format(half_expr, c)
    y = '({}) * {:.9f}'.format(half_expr, s)
    return x, y


def slide_to_center_gap(center_x, grip_x, half_gap):
    """Slide that puts the grip face `half_gap` short of the center (both measured along the
    seat's X axis): grip_x + slide = center_x - half_gap."""
    return center_x - half_gap - grip_x


# The stock's Y axis runs across the jaws (the shop's convention: jaws grip StockY). The
# workpiece is turned this much from "X into the part" so its Y lies across the jaws.
STOCK_ACROSS_TURN = -math.pi / 2


def grip_width(extent_x, extent_y, angle_rad):
    """Stock width across the jaws, from the workpiece's X/Y extents and the mount rotation
    about Z. With no rotation the stock's Y axis runs across the jaws."""
    c, s = abs(math.cos(angle_rad)), abs(math.sin(angle_rad))
    return c * extent_y + s * extent_x


STOCK_X_PARAM = 'StockX'
STOCK_Y_PARAM = 'StockY'


def stock_width_expr(angle_rad, x_name=STOCK_X_PARAM, y_name=STOCK_Y_PARAM):
    """The stock width across the jaws as an expression of the stock parameters, so hard jaws
    stay flush when the stock changes: StockY with no rotation, StockX when turned 90 deg."""
    c, s = abs(math.cos(angle_rad)), abs(math.sin(angle_rad))
    if s < 1e-9:
        return y_name
    if c < 1e-9:
        return x_name
    return '{:.9f} * {} + {:.9f} * {}'.format(c, y_name, s, x_name)


def mm_expr(value_cm, minus_param=None, divisor=1):
    """A Fusion expression: '<value> mm' optionally minus '<param> / <divisor>'."""
    text = '{:.4f} mm'.format(value_cm * 10.0)
    if minus_param:
        text += ' - {}'.format(minus_param) + (' / {}'.format(divisor) if divisor != 1 else '')
    return text


def blank_version(entry):
    """The file version a library entry was registered from (None if unknown)."""
    return ((entry or {}).get('source') or {}).get('version_at_register')


def blank_is_stale(jaw_state, entry):
    """True when the station's soft jaws were copied from an older registration of the same blank
    than the library now holds, so they no longer match the file. Jaws from before versions were
    recorded count as current."""
    if not jaw_state or not entry or jaw_state.get('urn') != entry.get('urn'):
        return False
    made, now = jaw_state.get('blank_version'), blank_version(entry)
    return made is not None and now is not None and made != now


def setup_station(name):
    """'OP1' / 'OP2' / 'OP3' from a CAM setup name such as 'Op 1', 'OP2 Finish', 'op-3'; else None."""
    m = re.search(r'\bop[\s_-]*([123])(?!\d)', name, re.IGNORECASE)
    return 'OP' + m.group(1) if m else None
