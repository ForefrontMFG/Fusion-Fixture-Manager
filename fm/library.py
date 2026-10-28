"""Fixture library: a plain JSON file keyed by Fusion lineage URN.

This module has no dependency on the Fusion API so it can be unit-tested
outside Fusion. The library file can live on a shared drive so a whole shop
uses one list of workholding.
"""

import datetime
import json
import os
import re
import tempfile

SCHEMA_VERSION = 1

_ADDIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(_ADDIN_DIR, 'config.json')
DEFAULT_LIBRARY_PATH = os.path.join(
    os.path.expanduser('~'), 'Documents', 'FixtureManager', 'fixture_library.json')


def _atomic_write_json(path, data):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.fm_', suffix='.json', dir=folder or None)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


# ---------------------------------------------------------------- config

def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    _atomic_write_json(CONFIG_PATH, cfg)


def library_path():
    return load_config().get('library_path') or DEFAULT_LIBRARY_PATH


def set_library_path(path):
    cfg = load_config()
    cfg['library_path'] = os.path.expanduser(path.strip())
    save_config(cfg)


# ---------------------------------------------------------------- library

class FixtureLibrary(object):
    """In-memory view of the library file. Call save() after edits."""

    def __init__(self, path=None):
        self.path = path or library_path()
        self.fixtures = {}
        self.load()

    def load(self):
        self.fixtures = {}
        if not os.path.exists(self.path):
            return
        with open(self.path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('version', 1) > SCHEMA_VERSION:
            raise ValueError('Library file was written by a newer Fixture Manager: ' + self.path)
        self.fixtures = data.get('fixtures', {})

    def save(self):
        _atomic_write_json(self.path, {'version': SCHEMA_VERSION, 'fixtures': self.fixtures})

    # --- queries

    def get(self, urn):
        return self.fixtures.get(urn)

    def sorted_entries(self):
        return sorted(self.fixtures.values(),
                      key=lambda e: (e.get('category', '').lower(), e.get('name', '').lower()))

    @staticmethod
    def label(entry):
        cat = entry.get('category') or 'Uncategorized'
        return '{}  |  {}'.format(cat, entry.get('name', entry['urn']))

    def categories(self):
        return sorted({e.get('category') or 'Uncategorized' for e in self.fixtures.values()})

    # --- edits

    def upsert(self, urn, name, category='', notes='', mount_points=None,
               default_mount='', metadata=None, source=None, extra=None):
        if not urn:
            raise ValueError('A fixture needs a URN (save the fixture design first).')
        now = datetime.datetime.now().isoformat(timespec='seconds')
        entry = self.fixtures.get(urn, {'urn': urn, 'added': now})
        entry.update({
            'name': name.strip() or urn,
            'category': category.strip(),
            'notes': notes,
            'mount_points': list(mount_points or []),
            'default_mount': default_mount,
            'metadata': dict(metadata or {}),
            'updated': now,
        })
        if source:
            entry['source'] = dict(source)
        # Kind-specific fields: kind, jaw_interface, clamping, positions, reversible,
        # cut_opening_cm. Keys set to None are removed (e.g. a vise re-registered as a fixture).
        for key, value in (extra or {}).items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
        self.fixtures[urn] = entry
        return entry

    def remove(self, urn):
        return self.fixtures.pop(urn, None) is not None

    # --- kinds

    @staticmethod
    def kind(entry):
        return entry.get('kind') or 'fixture'

    def mountables(self):
        """Entries you can apply directly: fixtures, vises and chucks (jaws are chosen per
        vise or chuck, plates separately)."""
        return [e for e in self.sorted_entries() if self.kind(e) in ('fixture', 'vise', 'chuck')]

    def plates(self):
        return [e for e in self.sorted_entries() if self.kind(e) == 'plate']

    def jaws_for(self, vise_entry):
        """Hard jaws and soft jaw blanks with the same jaw interface as `vise_entry`."""
        iface = (vise_entry.get('jaw_interface') or '').strip().lower()
        return [e for e in self.sorted_entries()
                if self.kind(e) in ('hard_jaw', 'soft_jaw')
                and (e.get('jaw_interface') or '').strip().lower() == iface]


# ---------------------------------------------------------------- helpers

MOUNT_PREFIX = 'FM_Mount'
WORKPIECE_ORIGIN_NAME = 'FM_Stock'

# Joint origin names by role. The FM_ names are this add-in's own; the others match the
# AU2024 "Workflow Template" framework (Mestenhauser) so files rigged that way register as-is.
# Matching is case-insensitive on the joint origin's own name (the part after any "/").
ROLE_NAMES = {
    'stock': ['FM_Mount', 'Stock Attachment - Fixturing Assembly', 'Stock Attachment'],
    'zero_point': ['FM_ZeroPoint', 'Zero Point Attachment'],
    'jaw': ['FM_Jaw', 'Jaw Attachment', 'Jaw Position'],
    'wcs': ['FM_WCS', 'WCS Attachment'],
    'machine': ['FM_Machine', 'Machine Model Attachment'],
}


def _leaf(key):
    return key.rsplit('/', 1)[-1]


def _role_of_name(name):
    name = name.lower()
    best, best_len = None, 0
    for role, aliases in ROLE_NAMES.items():
        for alias in aliases:
            a = alias.lower()
            if name.startswith(a) and len(a) > best_len:
                best, best_len = role, len(a)
    return best


def role_of(key):
    """Role of a joint origin from its name, or None. Longest alias wins. If the joint origin's
    own name says nothing, fall back to the container component it sits in (the framework's
    JOC pattern): "Jaw Attachment 1:1/Joint Origin" is a jaw."""
    role = _role_of_name(_leaf(key))
    if role is None and '/' in key:
        container = key.rsplit('/', 1)[0].rsplit('/', 1)[-1].rsplit(':', 1)[0]
        role = _role_of_name(container)
    return role


def classify(keys):
    """{role: [keys]} for every recognised joint origin, in input order."""
    out = {}
    for k in keys:
        r = role_of(k)
        if r:
            out.setdefault(r, []).append(k)
    return out


def joint_origin_keys(items):
    """Stable keys for [(occurrence_path_list, name)]: the bare name when unique, else
    "<path>/<name>". A duplicate at the fixture's top level keeps its bare name."""
    counts = {}
    for _, name in items:
        counts[name] = counts.get(name, 0) + 1
    return [name if counts[name] == 1 or not path else '/'.join(list(path) + [name])
            for path, name in items]


def order_mounts(keys):
    """Stock mount candidates first (in alias priority order), then everything else."""
    def rank(k):
        leaf = _leaf(k).lower()
        for i, alias in enumerate(ROLE_NAMES['stock']):
            if leaf == alias.lower():
                return (0, i, k.count('/'), k)
            if leaf.startswith(alias.lower()):
                return (1, i, k.count('/'), k)
        return (2, 0, k.count('/'), k)
    return sorted(keys, key=rank)


# The add-in's own joint origins that mark how a fixture is placed or built, never where the stock
# sits, so they are never offered as a stock mount.
_STRUCTURAL = re.compile(r'^FM_(ZP_.*|PlateMount|WCS.*|MachinePos|JawSeat_.*|Bolt|Grip|GripRev|Center|BaseTop'
                         r'|OP\d|Stock|Part|PartPlace|SeatsCenter)$', re.IGNORECASE)


def stock_mount_candidates(keys):
    """The joint origins a workpiece can mount on, best first: FM_Mount*, the framework's Stock
    Attachment names, then any other non-structural origin."""
    return order_mounts([k for k in keys if not _STRUCTURAL.match(_leaf(k))])


def pick_default_mount(names):
    """Best stock-mount candidate: exact FM_Mount, then FM_Mount*, then the framework's
    Stock Attachment names, then the first name."""
    candidates = stock_mount_candidates(names)
    return candidates[0] if candidates else ''



def describe(entry, units_label='mm', cm_to_units=10.0):
    lines = [
        'Name: ' + entry.get('name', ''),
        'Category: ' + (entry.get('category') or 'Uncategorized'),
        'URN: ' + entry.get('urn', ''),
        'Mount points: ' + (', '.join(entry.get('mount_points', [])) or '(none cached)'),
        'Default mount: ' + (entry.get('default_mount') or '-'),
    ]
    heights = entry.get('metadata', {}).get('mount_heights_cm', {})
    for name, z in sorted(heights.items()):
        lines.append('  {} height: {:.4f} {}'.format(name, z * cm_to_units, units_label))
    roles = entry.get('metadata', {}).get('roles', {})
    for role in sorted(roles):
        lines.append('  {}: {}'.format(role, ', '.join(roles[role])))
    kind = entry.get('kind') or 'fixture'
    lines.insert(2, 'Kind: ' + kind.replace('_', ' '))
    if entry.get('jaw_interface'):
        lines.append('Jaw interface: ' + entry['jaw_interface'])
    if kind == 'vise':
        lines.append('Clamping: {} | Positions: {}'.format(
            (entry.get('clamping') or 'self_centering').replace('_', ' '),
            ', '.join(entry.get('positions') or []) or '-'))
    if kind == 'chuck':
        lines.append('Main jaw positions: {} | Jaw counts: {}'.format(
            ', '.join(entry.get('positions') or []) or '-',
            ', '.join(str(n) for n in entry.get('jaw_counts') or []) or '-'))
    if kind == 'hard_jaw':
        lines.append('Reversible: ' + ('yes' if entry.get('reversible') else 'no (no FM_GripRev)'))
    if kind in ('hard_jaw', 'soft_jaw') and entry.get('metadata', {}).get('grip_local_cm'):
        lines.append('Grips: ' + ', '.join(sorted(entry['metadata']['grip_local_cm'])))
    if kind == 'plate':
        lines.append('Positions: ' + (', '.join('{} ({})'.format(pos, '/'.join(pats))
                                              for pos, pats in (entry.get('plate_positions') or {}).items()) or '-'))
    if kind in ('vise', 'chuck', 'fixture') and entry.get('stud_patterns'):
        lines.append('Stud patterns: ' + ', '.join(entry['stud_patterns']))
    if kind == 'soft_jaw' and entry.get('cut_opening_cm') is not None:
        lines.append('Cut opening: {:.4f} {}'.format(entry['cut_opening_cm'] * cm_to_units, units_label))
    src = entry.get('source') or {}
    if src.get('project'):
        lines.append('Project: ' + src['project'])
    if entry.get('notes'):
        lines.append('Notes: ' + entry['notes'])
    lines.append('Updated: ' + entry.get('updated', ''))
    return '\n'.join(lines)
