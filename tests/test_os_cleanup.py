import os
from pathlib import Path
import tempfile
import unittest

from migratarr.os_census import census
from migratarr.os_cleanup import prepare_batch, apply_batch, validate_progress
from migratarr.os_execute import load_state
from migratarr.recovery import _write_json


class CleanupJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'root'; self.root.mkdir()
        (self.root / 'old').mkdir()
        for name in ('a', 'b'): (self.root / 'old' / name).write_text('original')
        (self.root / 'keep').write_text('retain')
        self.original = census(self.root, [])
        self.paths = ['/old/a', '/old/b', '/old']

    def test_partial_batch_replays_missing_entries_and_removes_exact_remaining_leaves(self):
        batch = prepare_batch(self.root, self.original, self.paths)
        apply_batch(self.root, self.original, batch[:1])
        validate_progress(self.root, self.original, self.paths, 0, batch, [])
        apply_batch(self.root, self.original, batch)
        validate_progress(self.root, self.original, self.paths, 3, [], [])
        self.assertEqual((self.root / 'keep').read_text(), 'retain')
        self.assertFalse((self.root / 'old').exists())

    def test_replacement_after_unlink_cannot_be_deleted_as_an_old_pending_leaf(self):
        batch = prepare_batch(self.root, self.original, self.paths[:2])
        apply_batch(self.root, self.original, batch[:1])
        outside = Path(self.temp.name) / 'outside'; outside.write_text('keep outside')
        (self.root / 'old/a').symlink_to(outside)
        with self.assertRaises(ValueError): validate_progress(self.root, self.original, self.paths, 0, batch, [])
        self.assertEqual((self.root / 'old/b').read_text(), 'original')
        self.assertEqual(outside.read_text(), 'keep outside')

    def test_pending_hash_catches_tampering_after_a_legitimate_hardlink_ctime_change(self):
        (self.root / 'old/b').unlink(); os.link(self.root / 'old/a', self.root / 'old/b')
        original = census(self.root, [])
        batch = prepare_batch(self.root, original, self.paths[:2])
        apply_batch(self.root, original, batch[:1])
        validate_progress(self.root, original, self.paths, 0, batch, [])
        (self.root / 'old/b').write_text('modified')
        os.utime(self.root / 'old/b', ns=(original['/old/b'][7], original['/old/b'][7]))
        with self.assertRaisesRegex(ValueError, 'contents changed'):
            validate_progress(self.root, original, self.paths, 0, batch, [])
        self.assertTrue((self.root / 'old/b').exists())

    def test_new_paths_and_replaced_ancestors_invalidate_resume(self):
        batch = prepare_batch(self.root, self.original, self.paths[:2])
        (self.root / 'new').write_text('unreviewed')
        with self.assertRaises(ValueError): validate_progress(self.root, self.original, self.paths, 0, batch, [])
        (self.root / 'new').unlink()
        (self.root / 'old').rename(self.root / 'saved')
        (self.root / 'old').mkdir()
        (self.root / 'saved/a').rename(self.root / 'old/a')
        with self.assertRaisesRegex(ValueError, 'parent was replaced'):
            apply_batch(self.root, self.original, batch[:1])
        self.assertTrue((self.root / 'old/a').exists())

    def test_checkpoint_cannot_skip_cleanup_or_claim_an_unfinished_scope(self):
        path = Path(self.temp.name)
        job = {'job_id': 'job', 'target_uuid': 'target'}
        saved = {'scope': 'offline_qemu_whole_os_only', 'remove': ['/old']}
        state = dict(schema_version=2, job_id='job', target_uuid='target', preview_sha256='digest',
                     scope=saved['scope'], migration_ready=False, phase='interrupted', stage='copy_root',
                     removed=1, pending_cleanup=[], completed_scopes=[], attempt=1)
        def save(value):
            p = path / 'os-transfer.json'; _write_json(p, value, replace=p.exists())
        save(state); self.assertEqual(load_state(path, job, saved, 'digest'), state)
        for changes in ({'removed': 0}, {'stage': 'copy_boot'}, {'stage': 'cleanup', 'completed_scopes': ['root']}, {'attempt': True}):
            save(dict(state, **changes))
            with self.assertRaises(ValueError): load_state(path, job, saved, 'digest')
