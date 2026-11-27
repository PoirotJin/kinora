import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import review_payload_backup as payload
import retarget_cache as cache
import retarget_scheduler as scheduler
import source_release as releases

class FixTests(unittest.TestCase):
    def test_inventory_bound_is_configurable(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'topics/a';p.mkdir(parents=True)
            for i in range(3):(p/('motions_%06d.pkl'%i)).write_bytes(b'test')
            with self.assertRaises(ValueError):payload.inventory(root,max_files=2)
            self.assertEqual(len(payload.inventory(root,max_files=3)),3)
    def test_digest_invalidates_replaced_asset(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'asset';p.write_bytes(b'abc');first=cache.digest(p)
            p.write_bytes(b'xyz');self.assertNotEqual(first,cache.digest(p))
    def test_scheduler_profile_bounds(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);p=root/'review/retarget/scheduler.json';p.parent.mkdir(parents=True)
            p.write_text(json.dumps({'total_slots':48}));self.assertEqual(scheduler.profile(root)['total_slots'],48)
            for invalid in (0,65,True,'48'):
                p.write_text(json.dumps({'total_slots':invalid}))
                with self.assertRaises(ValueError):scheduler.profile(root)

    def test_scheduler_health_with_external_dispatch(self):
        from pipeline_pool import status
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            heartbeat = root/'review/retarget/scheduler-status.json'
            heartbeat.parent.mkdir(parents=True)
            self.assertEqual(scheduler.health(root, now=100), 'offline')
            for sampled_at, low_memory, expected in (
                (85, False, 'online'), (84, False, 'offline'), (99, True, 'low_memory'),
                (101, False, 'offline'),
            ):
                with self.subTest(sampled_at=sampled_at, low_memory=low_memory):
                    heartbeat.write_text(json.dumps(dict(sampled_at=sampled_at, low_memory=low_memory)))
                    self.assertEqual(scheduler.health(root, now=100), expected)
                    with patch.object(scheduler.time, 'time', return_value=100):
                        self.assertEqual(status(root)['retarget_scheduler']['state'], expected)
            heartbeat.with_name('scheduler.json').write_text('{"external_scheduler":false}')
            self.assertEqual(scheduler.health(root, now=100), 'internal')
    def test_restore_drill_keeps_backup_and_proof_without_second_database(self):
        from review_recovery import restore_drill,sha256_file
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)/'snapshot';folder.mkdir();source=folder/'data.sqlite3'
            with sqlite3.connect(source) as db:db.execute('CREATE TABLE events(value)');db.execute('INSERT INTO events VALUES(1)')
            digest=sha256_file(source)
            (folder/'manifest.json').write_text(json.dumps(dict(id='snapshot',databases=[dict(path='data.sqlite3',sha256=digest)])))
            destination=Path(d)/'drill';result=restore_drill(folder,destination)
            self.assertEqual(result['verified'],1);self.assertFalse(destination.exists())
            self.assertTrue((folder/'restore-evidence.json').is_file());self.assertEqual(sha256_file(source),digest)

    def test_live_wal_backup_pins_snapshot_without_blocking_writes(self):
        from review_recovery import sqlite_snapshot
        with tempfile.TemporaryDirectory() as d:
            source=Path(d)/'live.sqlite3';dest=Path(d)/'snapshot.sqlite3'
            with sqlite3.connect(source) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE events(value BLOB)')
                writer.executemany('INSERT INTO events VALUES(?)',[(b'x'*4096,)]*1000);writer.commit()
                changed=[]
                def progress(status,remaining,total):
                    if not changed:
                        writer.execute('INSERT INTO events VALUES(?)',(b'new-live-event',));writer.commit();changed.append(True)
                sqlite_snapshot(source.resolve(),dest,progress)
                self.assertTrue(changed)
                self.assertEqual(writer.execute('SELECT count(*) FROM events').fetchone()[0],1001)
            with sqlite3.connect(dest) as restored:
                self.assertEqual(restored.execute('SELECT count(*) FROM events').fetchone()[0],1000)

    def test_model_signature_cache_is_shared_and_invalidates(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);model=root/'third_party/holosoma/holosoma_retargeting/models/robot.xml'
            model.parent.mkdir(parents=True);model.write_text('model-v1')
            executable=root/'.retarget-venv/bin/python';executable.parent.mkdir(parents=True);executable.write_text('runtime-v1')
            result=SimpleNamespace(stdout=json.dumps({'sha256':'a'*64,'mujoco':'3','numpy':'1'}))
            with patch.object(cache,'PROJECT',root),patch('retarget_robots.model_path',return_value=model),patch.object(cache.subprocess,'run',return_value=result) as run:
                cache._models.clear();first=cache.model_signature('robot')
                cache._models.clear();self.assertEqual(cache.model_signature('robot'),first)
                self.assertEqual(run.call_count,1)
                model.write_text('different model-v2')
                cache._models.clear();cache.model_signature('robot');self.assertEqual(run.call_count,2)
                executable.write_text('different runtime-v2')
                cache._models.clear();cache.model_signature('robot');self.assertEqual(run.call_count,3)
                # Corrupt cache content is discarded rather than trusted.
                for p in (root/'var/retarget-model-signatures').glob('*.json'):p.write_text('{}')
                cache._models.clear();cache.model_signature('robot');self.assertEqual(run.call_count,4)
            cache._models.clear()

    def test_source_release_restore_and_tamper(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'project';p=root/'scripts/app.py';p.parent.mkdir(parents=True);p.write_text('old')
            data=root/'outputs/data';data.parent.mkdir();data.write_text('runtime')
            receipt=releases.snapshot(root,Path(d)/'releases');folder=Path(receipt['directory'])
            p.write_text('new');releases.restore(folder,root)
            self.assertEqual(p.read_text(),'old');self.assertEqual(data.read_text(),'runtime')
            (folder/'source/scripts/app.py').write_text('tampered')
            with self.assertRaises(ValueError):releases.restore(folder,root)
    def test_no_context_never_reuses(self):
        self.assertIsNone(cache.reuse(None,None,'id','owner','mid','robot',{},None))

if __name__=='__main__':unittest.main()
