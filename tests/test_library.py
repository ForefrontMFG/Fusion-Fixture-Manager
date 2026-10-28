import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from fm import library  # noqa: E402


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'sub', 'lib.json')

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip_and_sort(self):
        lib = library.FixtureLibrary(self.path)
        lib.upsert('urn:b', 'Kurt 6in', 'Vise', mount_points=['FM_Mount'], default_mount='FM_Mount',
                   metadata={'mount_heights_cm': {'FM_Mount': 4.445}})
        lib.upsert('urn:a', 'Orange Vise', 'Self-centering', mount_points=['FM_Mount', 'FM_Mount_Step2'])
        lib.save()
        again = library.FixtureLibrary(self.path)
        self.assertEqual([e['urn'] for e in again.sorted_entries()], ['urn:a', 'urn:b'])
        self.assertEqual(again.get('urn:b')['metadata']['mount_heights_cm']['FM_Mount'], 4.445)
        self.assertIn('44.4500 mm', library.describe(again.get('urn:b')))

    def test_upsert_keeps_added_and_remove(self):
        lib = library.FixtureLibrary(self.path)
        added = lib.upsert('urn:x', 'A')['added']
        lib.upsert('urn:x', 'B')
        self.assertEqual(lib.get('urn:x')['added'], added)
        self.assertEqual(lib.get('urn:x')['name'], 'B')
        self.assertTrue(lib.remove('urn:x'))
        self.assertFalse(lib.remove('urn:x'))

    def test_requires_urn(self):
        with self.assertRaises(ValueError):
            library.FixtureLibrary(self.path).upsert('', 'x')

    def test_default_mount(self):
        self.assertEqual(library.pick_default_mount(['Base', 'FM_Mount_2', 'FM_Mount']), 'FM_Mount')
        self.assertEqual(library.pick_default_mount(['Base', 'FM_Mount_2']), 'FM_Mount_2')
        self.assertEqual(library.pick_default_mount(['Base']), 'Base')
        self.assertEqual(library.pick_default_mount([]), '')


class NamingTests(unittest.TestCase):
    def test_framework_names_classify(self):
        keys = ['Stock Attachment', 'Zero Point Attachment', 'Jaw Attachment 1', 'Jaw Attachment 2',
                'WCS Attachment', 'Machine Model Attachment', 'Sketch JO', 'FM_Mount_Step2']
        roles = library.classify(keys)
        self.assertEqual(roles['stock'], ['Stock Attachment', 'FM_Mount_Step2'])
        self.assertEqual(roles['jaw'], ['Jaw Attachment 1', 'Jaw Attachment 2'])
        self.assertEqual(roles['wcs'], ['WCS Attachment'])
        self.assertEqual(roles['machine'], ['Machine Model Attachment'])
        self.assertNotIn('Sketch JO', sum(roles.values(), []))

    def test_role_from_container_component(self):
        # Seen live: framework-style file whose joint origins are generically named.
        self.assertEqual(library.role_of('Jaw Attachment 1:1/Jaw Origin'), 'jaw')
        self.assertEqual(library.role_of('Zero Point Attachment:2/Joint Origin1'), 'zero_point')
        self.assertIsNone(library.role_of('Vise:1/Body:1/Joint Origin3'))
        # A joint origin's own name still wins over its container's.
        self.assertEqual(library.role_of('Jaw Attachment 1:1/Stock Attachment'), 'stock')

    def test_longest_alias_wins(self):
        self.assertEqual(library.role_of('a:1/Stock Attachment - Fixturing Assembly'), 'stock')
        self.assertEqual(library.role_of('fm_zeropoint_B'), 'zero_point')

    def test_default_mount_prefers_fm_then_framework(self):
        self.assertEqual(library.pick_default_mount(['Stock Attachment', 'FM_Mount']), 'FM_Mount')
        self.assertEqual(library.pick_default_mount(
            ['Zero Point Attachment', 'Vise:1/Stock Attachment',
             'Stock Attachment - Fixturing Assembly']), 'Stock Attachment - Fixturing Assembly')
        self.assertEqual(library.order_mounts(['Other', 'Stock Attachment'])[0], 'Stock Attachment')

    def test_unique_keys(self):
        items = [([], 'Stock Attachment'),
                 (['Zero Point Attachment:1'], 'Zero Point Attachment'),
                 (['Zero Point Attachment:2'], 'Zero Point Attachment'),
                 (['WCS:1'], 'WCS Attachment')]
        self.assertEqual(library.joint_origin_keys(items), [
            'Stock Attachment',
            'Zero Point Attachment:1/Zero Point Attachment',
            'Zero Point Attachment:2/Zero Point Attachment',
            'WCS Attachment'])


class StockMountTests(unittest.TestCase):
    def test_structural_points_are_not_stock_mounts(self):
        keys = ['FM_ZP_52', 'FM_ZP_96', 'FM_PlateMount', 'FM_WCS', 'FM_Mount_Stop20', 'FM_Mount',
                'FM_Center', 'Base:1/Stock Attachment', 'Joint Origin1']
        self.assertEqual(library.stock_mount_candidates(keys),
                         ['FM_Mount', 'Base:1/Stock Attachment', 'FM_Mount_Stop20', 'Joint Origin1'])
        self.assertEqual(library.pick_default_mount(['FM_ZP_52', 'FM_Mount']), 'FM_Mount')
        self.assertEqual(library.pick_default_mount(['FM_ZP_52']), '')


if __name__ == '__main__':
    unittest.main()
