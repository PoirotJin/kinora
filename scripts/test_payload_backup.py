import json
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import review_payload_backup as backup


class PayloadBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'farm'
        self.target = self.base / 'backup'
        self.target.mkdir()
        self.shard = self.root / 'topics/walking/motions_000001.pkl'
        self.shard.parent.mkdir(parents=True)
        self.shard.write_bytes(b'immutable motion bytes')
        self.disk = patch.object(backup.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 100_000_000_000})())
        self.disk.start()

    def tearDown(self):
        self.disk.stop()
        self.tmp.cleanup()

    def test_incremental_restore_and_corruption(self):
        first = backup.backup(self.root, self.target)
        second = backup.backup(self.root, self.target)
        self.assertTrue(first['complete'])
        self.assertEqual(second['copied_bytes'], 0)
        self.assertEqual(second['reused'], 1)
        self.shard.write_bytes(b'new motion bytes')
        third = backup.backup(self.root, self.target)
        self.assertNotEqual(first['files'][0]['sha256'], third['files'][0]['sha256'])
        restored = self.base / 'restore'
        self.assertEqual(backup.restore(self.target, first['id'], restored)['scope'], 'full')
        self.assertEqual((restored / 'hot/topics/walking/motions_000001.pkl').read_bytes(), b'immutable motion bytes')
        (self.target / 'objects' / first['files'][0]['sha256']).write_bytes(b'corruption')
        with self.assertRaises(ValueError):
            backup.restore(self.target, first['id'], self.base / 'bad')

    def test_duplicate_and_renamed_content_never_copied(self):
        first = backup.backup(self.root, self.target)
        renamed = self.shard.with_name('motions_000002.pkl')
        self.shard.rename(renamed)
        with patch.object(Path, 'open', autospec=True, side_effect=Path.open) as opened:
            second = backup.backup(self.root, self.target)
        self.assertEqual(second['copied_bytes'], 0)
        self.assertEqual(second['reused'], 1)
        self.assertEqual(second['files'][0]['bytes'], renamed.stat().st_size)
        self.assertEqual(len(list((self.target/'objects').iterdir())), 1)
        self.assertFalse(any('.partial' in str(call.args[0]) for call in opened.call_args_list))
        for name in ('video.mp4', 'model.ckpt', 'details.log'):
            (renamed.parent/name).write_bytes(b'excluded')
        self.assertEqual(len(backup.inventory(self.root)), 1)

    def test_corrupt_reused_object_rejected(self):
        first = backup.backup(self.root, self.target)
        (self.target / 'objects' / first['files'][0]['sha256']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'corrupt'):
            backup.backup(self.root, self.target)

    def test_missing_object_counts_against_copy_budget(self):
        first = backup.backup(self.root, self.target)
        (self.target / 'objects' / first['files'][0]['sha256']).unlink()
        with self.assertRaisesRegex(ValueError, 'budget'):
            backup.backup(self.root, self.target, max_bytes=1)

    def test_changed_source_not_claimed_complete(self):
        with patch.object(backup, 'copy_object', side_effect=RuntimeError('source changed')):
            report = backup.backup(self.root, self.target)
        self.assertFalse(report['complete'])
        self.assertFalse((self.target / 'latest.json').exists())
        self.assertEqual(len(report['skipped']), 1)

    def test_failed_attempt_resumes_verified_objects(self):
        second=self.shard.with_name('motions_000002.pkl');second.write_bytes(b'second motion')
        original=backup.copy_object
        def flaky(source,target,rate):
            if source.resolve()==second.resolve():raise RuntimeError('writer busy')
            return original(source,target,rate)
        with patch.object(backup,'copy_object',side_effect=flaky):
            first=backup.backup(self.root,self.target)
        self.assertFalse(first['complete'])
        with patch.object(backup,'copy_object',wraps=original) as copy:
            resumed=backup.backup(self.root,self.target)
        self.assertTrue(resumed['complete']);self.assertEqual(resumed['reused'],1)
        self.assertEqual(copy.call_count,1);self.assertFalse((self.target/'checkpoint.jsonl').exists())

    def test_payload_requires_fresh_restore_verified_metadata(self):
        metadata=self.base/'metadata';metadata.mkdir()
        (self.root/'backup_config.json').write_text(json.dumps({'target':str(metadata)}))
        for created,verified in ((0,True),(time.time(),False)):
            (metadata/'latest.json').write_text(json.dumps(dict(id='db-test',created=created,restore_verified=verified)))
            with self.assertRaisesRegex(ValueError,'restore-verified metadata'):
                backup.backup(self.root,self.target)
        (metadata/'latest.json').write_text(json.dumps(dict(id='db-test',created=time.time(),restore_verified=True)))
        report=backup.backup(self.root,self.target)
        self.assertEqual(report['metadata_snapshot']['id'],'db-test');self.assertTrue(report['complete'])

    def test_inventory_scans_artifact_types_and_rejects_parent_symlink(self):
        folder=self.root/'review/retarget/job';folder.mkdir(parents=True)
        for name in ('result.npz','preview.json','quality.json','execution.json'):(folder/name).write_bytes(b'artifact')
        self.assertEqual(len(backup.inventory(self.root)),5)
        outside=self.base/'outside';outside.mkdir();(outside/'result.npz').write_bytes(b'external')
        (folder.parent/'escape').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):backup.inventory(self.root)

    def test_status_uses_small_manifest_summary(self):
        first=backup.backup(self.root,self.target)
        (self.root/'payload_backup_config.json').write_text(json.dumps({'target':str(self.target)}))
        # A status request must not parse the full, potentially million-file manifest.
        (self.target/'latest.json').write_text('not read by status')
        status=backup.status(self.root)
        self.assertEqual(status['latest']['file_count'],1)
        self.assertEqual(status['latest']['id'],first['id'])

    def test_symlink_budget_and_nested_destination_rejected(self):
        with self.assertRaises(ValueError):
            backup.backup(self.root, self.target, max_bytes=1)
        with self.assertRaises(ValueError):
            backup.backup(self.root, self.root / 'backup')
        self.shard.unlink()
        self.shard.symlink_to(self.base / 'external')
        with self.assertRaises(ValueError):
            backup.inventory(self.root)

    def test_reboot_st_dev_change_reuses_verified_objects(self):
        for n_shards in (1, 1005):
            with self.subTest(n_shards=n_shards), tempfile.TemporaryDirectory() as sub:
                base = Path(sub)
                root = base / 'farm'
                target = base / 'backup'
                target.mkdir()
                folder = root / 'topics/walking'
                folder.mkdir(parents=True)
                for i in range(n_shards):
                    (folder / f'motions_{i:06d}.pkl').write_bytes(f'shard-{i}-payload'.encode())
                real_fp = backup.fingerprint
                dev_box = {'dev': 66309}
                def fake_fp(path):
                    fp = real_fp(path)
                    return [dev_box['dev'], *fp[1:]]
                with patch.object(backup.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 100_000_000_000})()), \
                     patch.object(backup, 'fingerprint', side_effect=fake_fp):
                    first = backup.backup(root, target)
                    self.assertTrue(first['complete'])
                    dev_box['dev'] = 66307
                    with patch.object(backup, 'hash_file', wraps=backup.hash_file) as hf:
                        second = backup.backup(root, target)
                    self.assertTrue(second['complete'])
                    self.assertEqual(second['copied_bytes'], 0)
                    self.assertEqual(second['reused'], n_shards)
                    self.assertTrue(all(f['fingerprint'][0] == 66307 for f in second['files']))
                    self.assertTrue(all(f['object_fingerprint'][0] == 66307 for f in second['files']))
                    if n_shards > 1000:
                        self.assertLessEqual(hf.call_count, 32)
                        self.assertEqual(hf.call_count, 32)


if __name__ == '__main__':
    unittest.main()
