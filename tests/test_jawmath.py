import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from fm import jawmath as jm  # noqa: E402
from fm import library  # noqa: E402


class JawMathTests(unittest.TestCase):
    def test_jaw_seats_need_both_sides(self):
        keys = ['FM_Center', 'FM_JawSeat_A1', 'FM_JawSeat_A2', 'FM_JawSeat_B1', 'FM_JawSeat_B2',
                'FM_JawSeat_C1', 'Master:1/FM_JawSeat_inner_1', 'Master:1/FM_JawSeat_inner_2']
        seats = jm.jaw_seats(keys)
        self.assertEqual(sorted(seats), ['A', 'B', 'INNER'])  # C has only side 1
        self.assertEqual(seats['A'], {1: 'FM_JawSeat_A1', 2: 'FM_JawSeat_A2'})

    def test_jaw_seats_any_count(self):
        chuck = ['FM_JawSeat_A{}'.format(n) for n in range(1, 7)]
        seats = jm.jaw_seats(chuck + ['FM_JawSeat_B1', 'FM_JawSeat_B2', 'FM_JawSeat_B4'])
        self.assertEqual(sorted(seats), ['A'])  # B skips seat 3
        self.assertEqual(jm.seat_list(seats['A']), chuck)
        self.assertEqual(jm.jaw_seats(chuck + ['FM_JawSeat_A10']), {})  # 7..9 missing: rejected
        self.assertEqual(jm.jaw_seats(['FM_JawSeat_A1']), {})

    def test_chuck_main_seat_and_counts(self):
        chuck = ['FM_Center', 'FM_BaseTop', 'FM_JawSeat_A1', 'FM_ZP_96']
        self.assertEqual(jm.main_seats(chuck), {'A': 'FM_JawSeat_A1'})
        self.assertEqual(jm.suggest_kind(chuck), jm.KIND_CHUCK)
        self.assertEqual(jm.validate(jm.KIND_CHUCK, chuck), [])
        self.assertEqual(len(jm.validate(jm.KIND_CHUCK, ['FM_JawSeat_A1'])), 1)  # no FM_Center
        self.assertEqual(jm.suggest_kind(chuck + ['FM_JawSeat_A2']), jm.KIND_VISE)  # a full seat set
        self.assertEqual([round(math.degrees(a)) for a in jm.jaw_angles(3)], [0, 120, 240])
        self.assertEqual([round(math.degrees(a)) for a in jm.jaw_angles(2)], [0, 180])
        with self.assertRaises(ValueError):
            jm.jaw_angles(1)
        self.assertEqual(jm.parse_jaw_counts('6, 2;3 3 x 1'), [2, 3, 6])

    def test_suggest_and_validate(self):
        vise = ['FM_Center', 'FM_BaseTop', 'FM_JawSeat_A1', 'FM_JawSeat_A2']
        jaw = ['FM_Bolt', 'FM_Grip', 'FM_GripRev']
        self.assertEqual(jm.suggest_kind(vise), jm.KIND_VISE)
        self.assertEqual(jm.suggest_kind(jaw), jm.KIND_HARD_JAW)
        self.assertEqual(jm.suggest_kind(['FM_Mount']), jm.KIND_FIXTURE)
        self.assertEqual(jm.validate(jm.KIND_VISE, vise), [])
        self.assertEqual(len(jm.validate(jm.KIND_VISE, ['FM_JawSeat_A1'])), 2)
        self.assertEqual(jm.validate(jm.KIND_HARD_JAW, jaw), [])
        self.assertEqual(len(jm.validate(jm.KIND_SOFT_JAW, ['FM_Bolt'])), 1)
        self.assertTrue(jm.is_reversible(jaw))
        self.assertFalse(jm.is_reversible(jaw[:2]))

    def test_ksc3_example(self):
        # Seat 60 mm from center, grip face 12 mm in front of the bolt, 50 mm stock:
        # the jaw slides 60 - 25 - 12 = 23 mm toward the center.
        self.assertAlmostEqual(jm.slide_to_center_gap(6.0, jm.grip_in_seat_x([1.2, 0, 2.5], False), 2.5), 2.3)
        # Reversed: the back face (FM_GripRev at -8 mm) turns to face the center at +8 mm.
        self.assertAlmostEqual(jm.grip_in_seat_x([-0.8, 0, 0], True), 0.8)
        self.assertAlmostEqual(jm.slide_to_center_gap(6.0, 0.8, 2.5), 2.7)

    def test_forward_ignores_bolt_rotation(self):
        # Grip straight ahead of the bolt: no turn needed.
        self.assertAlmostEqual(jm.forward_angle([1.45, 0, 2.4]), 0.0)
        # KSC jaw whose FM_Bolt X runs along the jaw: grip 18.27 mm off to the side.
        fwd = jm.forward_angle([0.0, 1.827, 2.5])
        self.assertAlmostEqual(fwd, math.pi / 2)
        g = jm.seated([0.0, 1.827, 2.5], fwd, False)
        self.assertAlmostEqual(g[0], 1.827)
        self.assertAlmostEqual(g[1], 0.0)
        self.assertAlmostEqual(g[2], 2.5)
        # Reversed: the same jaw turned a further 180 deg; its grip face now faces the center
        # from the other side of the bolt.
        r = jm.seated([0.0, -1.089, 2.498], fwd, True)
        self.assertAlmostEqual(r[0], 1.089)
        self.assertAlmostEqual(r[1], 0.0)
        # No grip offset at all: fall back to the bolt's own X axis.
        self.assertEqual(jm.forward_angle([0, 0, 1]), 0.0)

    def test_grip_for_seat(self):
        # Chuck: FM_Grip / FM_GripRev on the chuck face, ...2 and ...3 on jaw steps 1 and 2.
        chuck = dict(jm.seat_options(jm.KIND_CHUCK))
        self.assertEqual(list(chuck), ['Chuck face', 'Jaw step 1', 'Jaw step 2'])
        self.assertEqual(jm.grip_for(False, chuck['Chuck face']), 'FM_Grip')
        self.assertEqual(jm.grip_for(True, chuck['Chuck face']), 'FM_GripRev')
        self.assertEqual(jm.grip_for(True, chuck['Jaw step 1']), 'FM_GripRev2')
        self.assertEqual(jm.grip_for(True, chuck['Jaw step 2']), 'FM_GripRev3')
        # Vise: unchanged.
        self.assertEqual(list(dict(jm.seat_options(jm.KIND_VISE))), ['Jaw step', 'Top of jaw base'])
        self.assertEqual(jm.grip_for(True, jm.SEAT_BASE_TOP), 'FM_GripRev')  # X from the grip, Z from FM_BaseTop
        self.assertEqual(jm.jaw_grips(['FM_Bolt', 'FM_GripRev3', 'FM_Grip', 'FM_GripRev2', 'FM_GripRev']),
                         ['FM_Grip', 'FM_GripRev', 'FM_GripRev2', 'FM_GripRev3'])

    def test_wrap_and_mount_offsets(self):
        self.assertAlmostEqual(jm.wrap(3 * math.pi / 2), -math.pi / 2)
        self.assertAlmostEqual(jm.wrap(-math.pi), math.pi)
        # Grip X already points into the part: plain half-width on X.
        self.assertEqual(jm.mount_offset_exprs('FM_GripWidth / 2', 0.0), ('FM_GripWidth / 2', None))
        self.assertEqual(jm.mount_offset_exprs('FM_GripWidth / 2', math.pi), ('-(FM_GripWidth / 2)', None))
        # Grip X turned 90 deg away from into-the-part: the half-width goes on Y.
        x, y = jm.mount_offset_exprs('FM_GripWidth / 2', math.pi / 2)
        self.assertIsNone(x)
        self.assertEqual(y, '(FM_GripWidth / 2) * 1.000000000')

    def test_plate_receivers_and_studs(self):
        plate = ['FM_PlateMount', 'FM_WCS', 'FM_ZP_Center_96', 'FM_ZP_Center_52',
                 'FM_ZP_A_52', 'FM_ZP_B_52']
        pos = jm.plate_positions(plate)
        self.assertEqual(list(pos), ['A', 'B', 'Center'])
        self.assertEqual(pos['Center'], {'96': 'FM_ZP_Center_96', '52': 'FM_ZP_Center_52'})
        self.assertEqual(jm.suggest_kind(plate), jm.KIND_PLATE)
        self.assertEqual(jm.validate(jm.KIND_PLATE, plate), [])
        self.assertEqual(len(jm.validate(jm.KIND_PLATE, ['FM_ZP_A_52'])), 2)  # no FM_WCS / mount
        # A vise with both stud patterns; studs aren't mistaken for receivers.
        vise = ['FM_Center', 'FM_JawSeat_A1', 'FM_JawSeat_A2', 'FM_ZP_96', 'FM_ZP_52']
        self.assertEqual(jm.plate_positions(vise), {})
        self.assertEqual(jm.stud_patterns(vise), {'52': 'FM_ZP_52', '96': 'FM_ZP_96'})
        self.assertEqual(jm.suggest_kind(vise), jm.KIND_VISE)
        self.assertEqual(jm.plate_choices(pos, jm.stud_patterns(vise)),
                         [('A', '52'), ('B', '52'), ('Center', '96'), ('Center', '52')])
        # A vise with only the 96 pattern can't use the 52-only positions.
        self.assertEqual(jm.plate_choices(pos, {'96': 'FM_ZP_96'}), [('Center', '96')])

    def test_setup_station_from_name(self):
        for name, want in (('Op 1', 'OP1'), ('OP2 Finish', 'OP2'), ('op-2', 'OP2'), ('Op_1 Rough', 'OP1'),
                           ('Op 3', 'OP3'), ('OP3 Chamfer', 'OP3'), ('Op 4', None),
                           ('Cut Soft Jaws', None), ('Op 12', None), ('Setup1', None), ('Shop 1', None)):
            self.assertEqual(jm.setup_station(name), want, name)

    def test_grip_width_follows_rotation(self):
        # StockY runs across the jaws with no rotation (the shop's convention).
        self.assertAlmostEqual(jm.grip_width(5.0, 3.0, 0.0), 3.0)
        self.assertAlmostEqual(jm.grip_width(5.0, 3.0, math.pi / 2), 5.0)
        self.assertAlmostEqual(jm.grip_width(5.0, 3.0, math.pi), 3.0)

    def test_stock_width_expr(self):
        self.assertEqual(jm.stock_width_expr(0.0), 'StockY')
        self.assertEqual(jm.stock_width_expr(math.pi), 'StockY')
        self.assertEqual(jm.stock_width_expr(math.pi / 2), 'StockX')
        self.assertEqual(jm.stock_width_expr(-math.pi / 2), 'StockX')
        self.assertEqual(jm.stock_width_expr(math.radians(30)), '0.866025404 * StockY + 0.500000000 * StockX')

    def test_mm_expr(self):
        self.assertEqual(jm.mm_expr(4.8, 'FM_GripWidth', 2), '48.0000 mm - FM_GripWidth / 2')
        self.assertEqual(jm.mm_expr(1.25, 'FM_GripWidth'), '12.5000 mm - FM_GripWidth')
        self.assertEqual(jm.mm_expr(-0.5), '-5.0000 mm')

    def test_blank_is_stale(self):
        entry = {'urn': 'u1', 'source': {'version_at_register': 2}}
        self.assertTrue(jm.blank_is_stale({'urn': 'u1', 'blank_version': 1}, entry))
        self.assertFalse(jm.blank_is_stale({'urn': 'u1', 'blank_version': 2}, entry))
        self.assertFalse(jm.blank_is_stale({'urn': 'u2', 'blank_version': 1}, entry))  # other blank
        self.assertFalse(jm.blank_is_stale({'urn': 'u1'}, entry))  # made before versions were recorded
        self.assertFalse(jm.blank_is_stale({}, entry))


class LibraryKindTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lib = library.FixtureLibrary(os.path.join(self.dir.name, 'lib.json'))

    def tearDown(self):
        self.dir.cleanup()

    def test_jaws_for_matches_interface(self):
        lib = self.lib
        lib.upsert('v', 'KSC3', extra={'kind': 'vise', 'jaw_interface': 'KSC3-80'})
        lib.upsert('h', 'Hard', extra={'kind': 'hard_jaw', 'jaw_interface': 'ksc3-80 '})
        lib.upsert('s', 'Soft', extra={'kind': 'soft_jaw', 'jaw_interface': 'KSC3-80',
                                       'cut_opening_cm': 4.0})
        lib.upsert('k', 'Kurt jaw', extra={'kind': 'hard_jaw', 'jaw_interface': 'Kurt 6'})
        lib.upsert('f', 'Plate', mount_points=['FM_Mount'])
        self.assertEqual([e['urn'] for e in lib.jaws_for(lib.get('v'))], ['h', 's'])
        self.assertEqual(sorted(e['urn'] for e in lib.mountables()), ['f', 'v'])

    def test_extra_none_removes_field(self):
        self.lib.upsert('x', 'X', extra={'kind': 'vise', 'clamping': 'fixed_jaw'})
        self.lib.upsert('x', 'X', extra={'kind': 'fixture', 'clamping': None})
        self.assertNotIn('clamping', self.lib.get('x'))
        self.assertIn('Kind: fixture', library.describe(self.lib.get('x')))


if __name__ == '__main__':
    unittest.main()
