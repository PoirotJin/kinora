import json
from pathlib import Path
import sqlite3
import tempfile
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import unittest
from unittest.mock import patch
import review_monitor as module
from g2h.api_usage import record,connect
from contextlib import closing


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=self.root/'topics.json';self.config.write_text(json.dumps({'topics':[{'id':'walking','name':'走路'}]}))
        with sqlite3.connect(self.root/'prompts.sqlite3') as db:
            db.executescript("CREATE TABLE topic_counts(topic,total);INSERT INTO topic_counts VALUES('walking',2);CREATE TABLE embeddings(topic);INSERT INTO embeddings VALUES('walking');")
        (self.root/'review').mkdir()
        with sqlite3.connect(self.root/'review/reviews.sqlite3') as db:db.execute('CREATE TABLE latest(status)')
        self.folder=self.root/'topics/walking';self.folder.mkdir(parents=True)
        with sqlite3.connect(self.folder/'index.sqlite3') as db:
            db.executescript("CREATE TABLE tasks(state);INSERT INTO tasks VALUES('failed');INSERT INTO tasks VALUES('pending');CREATE TABLE records(id);INSERT INTO records VALUES('one');CREATE TABLE shards(number,bytes);INSERT INTO shards VALUES(0,100);")
        self.monitor=module.Monitor(self.root,self.config)

    def tearDown(self):self.tmp.cleanup()

    @patch.object(module, 'gpu_probe', return_value={'available': True, 'items': []})
    def test_host_probe_failure_keeps_storage_and_database_monitoring(self, gpu):
        original = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.is_relative_to('/proc'):
                raise FileNotFoundError(path)
            return original(path, *args, **kwargs)

        with patch.object(Path, 'read_text', read_text):
            first = self.monitor.sample()
            second = self.monitor.sample()
        self.assertTrue(second['ready'])
        self.assertEqual(second['topics'][0]['motions'], 1)
        self.assertIn('shard_walking', {alert['key'] for alert in second['alerts']})
        self.assertIsNone(first['cpu'])
        self.assertIsNone(second['memory']['percent'])
        self.assertIsNone(second['history'][-1]['rx'])

    @patch.object(module, 'gpu_probe', return_value={'available': True, 'items': []})
    def test_archive_committed_during_inspection_is_not_reported_lost(self, gpu):
        hot = self.folder/'motions_000000.pkl'
        cold = self.folder/'motions_000000.mf.zip'
        index = self.folder/'index.sqlite3'
        hot.write_bytes(b'h'*100)
        original = Path.stat
        moved = False

        def move_on_probe(path, *args, **kwargs):
            nonlocal moved
            if path == hot and not moved:
                moved = True
                cold.write_bytes(b'c'*80)
                with closing(sqlite3.connect(index)) as db, db:
                    db.execute('CREATE TABLE cold_shards(number,archive_bytes)')
                    db.execute('INSERT INTO cold_shards VALUES(0,80)')
                hot.unlink()
            return original(path, *args, **kwargs)

        with patch.object(Path, 'stat', move_on_probe):
            result = self.monitor.sample()
        self.assertTrue(moved)
        self.assertNotIn('shard_walking', {alert['key'] for alert in result['alerts']})
        cold.write_bytes(b'truncated')
        result = self.monitor.sample()
        self.assertIn('shard_walking', {alert['key'] for alert in result['alerts']})

    @patch.object(module, 'gpu_probe', return_value={'available': True, 'items': []})
    def test_offline_cold_storage_keeps_hot_shard_and_other_metrics_visible(self, gpu):
        (self.root/'storage_tiers.json').write_text(json.dumps({'cold_root':str(self.root/'unmounted')}))
        (self.folder/'motions_000001.pkl').write_bytes(b'h'*100)
        with closing(sqlite3.connect(self.folder/'index.sqlite3')) as db, db:
            db.execute('CREATE TABLE cold_shards(number,archive_bytes)')
            db.execute('INSERT INTO cold_shards VALUES(0,80)')
            db.execute('INSERT INTO shards VALUES(1,100),(2,0)')
        result = self.monitor.sample()
        self.assertTrue(result['ready'])
        self.assertTrue(result['topics'][0]['db_ok'])
        self.assertEqual(result['topics'][0]['motions'], 1)
        alerts = [a['key'] for a in result['alerts'] if a['key'].startswith('shard_')]
        self.assertEqual(alerts, ['shard_unavailable_walking'])

    @patch.object(module,'gpu_probe',return_value={'available':True,'items':[]})
    def test_faults_and_recovery(self,gpu):
        result=self.monitor.sample();keys={a['key'] for a in result['alerts']}
        self.assertTrue({'failed_walking','queue_walking','shard_walking'}.issubset(keys))
        self.assertEqual(result['topics'][0]['motions'],1);self.assertEqual(result['topics'][0]['texts'],2)
        self.assertIsNone(result['cpu'])
        (self.folder/'motions_000000.pkl').write_bytes(b'x'*100)
        with sqlite3.connect(self.folder/'index.sqlite3') as db:db.execute("UPDATE tasks SET state='done'")
        result=self.monitor.sample();keys={a['key'] for a in result['alerts']}
        self.assertFalse({'failed_walking','queue_walking','shard_walking'}&keys)
        self.assertTrue(any(e['state']=='恢复' for e in result['events']))

    @patch.object(module,'gpu_probe',return_value={'available':False,'items':[]})
    def test_unavailable_data_is_not_zero(self,gpu):
        (self.root/'prompts.sqlite3').unlink()
        result=self.monitor.sample()
        self.assertIsNone(result['topics'][0]['texts']);self.assertFalse(result['gpu']['available'])
        self.assertFalse(result['usage']['available'])

    def test_actual_usage_dedup_unknown_and_errors(self):
        path=self.root/'review/api_usage.sqlite3'
        response={'id':'request1','usage':{'prompt_tokens':10,'completion_tokens':4,'total_tokens':14}}
        self.assertTrue(record('qwen','chat',response,path=path));record('qwen','chat',response,path=path)
        record('embed','embedding',{'usage':{'input_tokens':8,'total_tokens':8}},path=path)
        record('qwen','chat',status='http_429',path=path)
        with closing(connect(path)) as db:
            self.assertEqual(db.execute("SELECT calls,errors,total,unknown FROM totals WHERE model='qwen'").fetchone(),(2,1,14,1))
            self.assertEqual(db.execute("SELECT total FROM totals WHERE model='embed'").fetchone()[0],8)

    @patch.object(module,'gpu_probe',return_value={'available':True,'items':[]})
    def test_api_wait_does_not_report_idle_queue_failure(self,gpu):
        import time
        folder=self.root/'review/production';folder.mkdir()
        with sqlite3.connect(folder/'jobs.sqlite3') as db:
            db.execute('CREATE TABLE jobs(stage,topic,updated,message,status)')
            db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',('backoff','walking',time.time(),'retry','running'))
        result=self.monitor.sample()
        self.assertNotIn('queue_walking',{a['key'] for a in result['alerts']})
        with sqlite3.connect(folder/'jobs.sqlite3') as db:db.execute('UPDATE jobs SET updated=0')
        result=self.monitor.sample();self.assertIn('heartbeat',{a['key'] for a in result['alerts']})


if __name__=='__main__':unittest.main()
