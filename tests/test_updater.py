import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from fm import updater  # noqa: E402

MANIFEST = '{\n\t"type": "addin",\n\t"version": "0.3.0",\n\t"runOnStartup": true\n}\n'


class VersionTests(unittest.TestCase):
    def test_parse_and_bump(self):
        self.assertEqual(updater.parse_version('v1.2.3'), (1, 2, 3))
        self.assertIsNone(updater.parse_version('1.2'))
        self.assertEqual(updater.bump('0.3.0'), '0.3.1')
        self.assertEqual(updater.bump('0.3.9', 'minor'), '0.4.0')
        self.assertEqual(updater.bump('0.3.9', 'major'), '1.0.0')
        with self.assertRaises(updater.UpdateError):
            updater.bump('x')

    def test_latest_and_newer(self):
        self.assertEqual(updater.latest_version(['v0.3.1', 'v0.10.0', 'v0.9.2', 'junk']), '0.10.0')
        self.assertIsNone(updater.latest_version([]))
        self.assertTrue(updater.is_newer('v0.10.0', '0.9.9'))
        self.assertFalse(updater.is_newer('v0.3.0', '0.3.0'))

    def test_manifest_keeps_formatting(self):
        out = updater.set_manifest_version(MANIFEST, '0.3.1')
        self.assertEqual(out, MANIFEST.replace('0.3.0', '0.3.1'))


@unittest.skipUnless(shutil.which('git'), 'git not installed')
class RoundTripTests(unittest.TestCase):
    """Publish from one clone and install the update in another, against a local bare repo."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.origin = os.path.join(self.tmp, 'origin.git')
        self._run('git', 'init', '--bare', '-b', 'main', self.origin)
        self.a = self._clone('a')
        with open(os.path.join(self.a, 'FixtureManager.manifest'), 'w') as f:
            f.write(MANIFEST)
        self._run('git', '-C', self.a, 'add', '-A')
        self._run('git', '-C', self.a, 'commit', '-m', 'start')
        self._run('git', '-C', self.a, 'tag', '-a', 'v0.3.0', '-m', 'start')
        self._run('git', '-C', self.a, 'push', 'origin', 'HEAD:main', '--tags')
        self.b = self._clone('b')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *cmd):
        subprocess.run(cmd, check=True, capture_output=True)

    def _clone(self, name):
        path = os.path.join(self.tmp, name)
        self._run('git', 'clone', self.origin, path)
        self._run('git', '-C', path, 'config', 'user.email', 'test@example.com')
        self._run('git', '-C', path, 'config', 'user.name', 'Test')
        return path

    def test_publish_then_update(self):
        with self.assertRaises(updater.UpdateError):
            updater.publish('nothing changed', root=self.a)
        with open(os.path.join(self.a, 'new.py'), 'w') as f:
            f.write('x = 1\n')
        msgs = updater.publish('Add new.py', 'minor', root=self.a)
        self.assertIn('0.4.0', ' '.join(msgs))
        self.assertEqual(updater.read_version(self.a), '0.4.0')

        latest, notes = updater.available_update(root=self.b)
        self.assertEqual(latest, '0.4.0')
        self.assertEqual(notes, ['v0.4.0: Add new.py'])
        updater.install_update(root=self.b)
        self.assertEqual(updater.read_version(self.b), '0.4.0')
        self.assertTrue(os.path.exists(os.path.join(self.b, 'new.py')))
        self.assertEqual(updater.available_update(root=self.b), (None, []))

    def test_update_refuses_local_edits_and_publish_refuses_when_behind(self):
        with open(os.path.join(self.a, 'new.py'), 'w') as f:
            f.write('x = 1\n')
        updater.publish('Add new.py', root=self.a)
        with open(os.path.join(self.b, 'local.py'), 'w') as f:
            f.write('y = 2\n')
        with self.assertRaises(updater.UpdateError):
            updater.install_update(root=self.b)
        with self.assertRaises(updater.UpdateError):
            updater.publish('from b', root=self.b)  # GitHub is ahead of b


if __name__ == '__main__':
    unittest.main()
