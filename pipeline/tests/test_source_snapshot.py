import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from pipeline.tools import export_source_snapshot as snapshot


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'repo'
        self.dest = Path(self.tmp.name) / 'cloud'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Snapshot Test')
        self.git('config', 'user.email', 'snapshot@example.invalid')
        (self.root / 'scripts').mkdir()
        shutil.copy2(Path(__file__).resolve().parents[2] / 'scripts/scan-secrets.py', self.root / 'scripts/scan-secrets.py')
        (self.root / '.gitignore').write_text('.omo/\n.tools/\nconfig.json\n')
        (self.root / 'app.py').write_text('old\n')
        self.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.DEVNULL)

    def commit(self):
        self.git('add', '.')
        self.git('commit', '-qm', 'snapshot fixture')

    def test_only_commit_files_and_check_has_no_destination_writes(self):
        (self.root / 'app.py').write_text('uncommitted')
        (self.root / 'config.json').write_text('private config')
        (self.root / '.tools').mkdir()
        (self.root / '.tools/runtime').write_text('runtime')
        snapshot.export(self.root, self.dest)
        self.assertFalse(self.dest.exists())
        snapshot.export(self.root, self.dest, publish=True)
        self.assertEqual((self.dest / 'app.py').read_text(), 'old\n')
        self.assertFalse((self.dest / '.git').exists())
        self.assertFalse((self.dest / 'config.json').exists())
        self.assertFalse((self.dest / '.tools').exists())

    def test_human_edit_conflicts_and_unmanaged_files_survive(self):
        snapshot.export(self.root, self.dest, publish=True)
        (self.dest / 'notes.txt').write_text('human extra')
        (self.root / 'app.py').unlink()
        self.commit()
        snapshot.export(self.root, self.dest, publish=True)
        self.assertFalse((self.dest / 'app.py').exists())
        self.assertEqual((self.dest / 'notes.txt').read_text(), 'human extra')
        (self.dest / '.gitignore').write_text('human edit')
        with self.assertRaisesRegex(snapshot.SnapshotError, 'edited'):
            snapshot.export(self.root, self.dest, publish=True)
        self.assertEqual((self.dest / '.gitignore').read_text(), 'human edit')

    def test_interrupted_publication_resumes_and_does_not_swallow_human_edits(self):
        actual = snapshot.atomic_file
        count = 0
        def fail_once(path, data):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError('interrupted')
            actual(path, data)
        with patch.object(snapshot, 'atomic_file', side_effect=fail_once):
            with self.assertRaises(OSError):
                snapshot.export(self.root, self.dest, publish=True)
        self.assertTrue((self.dest / snapshot.PENDING).exists())
        snapshot.export(self.root, self.dest, publish=True)
        self.assertFalse((self.dest / snapshot.PENDING).exists())
        self.assertEqual((self.dest / 'app.py').read_text(), 'old\n')

    def test_credentials_and_repository_destinations_are_rejected(self):
        (self.root / 'bad.txt').write_text('sk-proj-' + 'a' * 40)
        self.commit()
        with self.assertRaisesRegex(snapshot.SnapshotError, 'credential'):
            snapshot.export(self.root, self.dest, publish=True)
        self.assertFalse(self.dest.exists())
        with self.assertRaises(snapshot.SnapshotError):
            snapshot.export(self.root, self.root, publish=True)


if __name__ == '__main__':
    unittest.main()
