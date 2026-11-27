import json
import os
import fcntl
import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch,Mock
from contextlib import closing
from review_production import Production


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.config=self.root/'topics.json';self.config.write_text(json.dumps({'topics':[{'id':'walking','name':'走路'}]}))
        self.p=Production(self.root,self.config)
        self.settings=dict(topics=['walking'],per_topic=1,k=2,rounds=1,duration=8)

    def tearDown(self): self.tmp.cleanup()

    def launch(self):
        with patch('review_production.subprocess.Popen') as popen:
            popen.return_value.pid=os.getpid();popen.return_value.wait.return_value=0
            return self.p.launch(self.settings,'tester')['job_id']

    def test_single_job_and_stop(self):
        job=self.launch()
        with self.assertRaises(ValueError): self.launch()
        self.p.cancel();self.assertTrue(self.p.cancelled(job))
        self.p.update(job,status='stopped')
        self.assertNotEqual(job,self.launch())

    def test_cancel_before_worker_claim_releases_slot_even_if_a_worker_already_read_the_job(self):
        job = self.launch()
        replacement = Production(self.root, self.config)
        validate = self.p.validate
        next_job = None

        def cancel_while_worker_is_starting(settings):
            nonlocal next_job
            validated = validate(settings)
            self.p.cancel()
            next_job = replacement.launch(self.settings, 'tester')['job_id']
            return validated

        with patch.object(self.p, 'validate', side_effect=cancel_while_worker_is_starting), \
                patch.object(self.p, 'command') as command:
            self.p.execute(job)
        command.assert_not_called()
        with closing(self.p.db()) as db:
            old = db.execute('SELECT status,stage,stop FROM jobs WHERE id=?', (job,)).fetchone()
            new = db.execute('SELECT status FROM jobs WHERE id=?', (next_job,)).fetchone()
        self.assertEqual(tuple(old), ('stopped', 'stopped', 1))
        self.assertEqual(new['status'], 'queued')

    def test_cancelled_queued_production_releases_slot_over_http_and_on_worker_recovery(self):
        from test_review_access import AccessTests

        fixture = AccessTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        api = fixture.server.production = Production(fixture.root, fixture.fixture.config)
        first_status, first = fixture.call('/api/production/start', 'jfc', self.settings)
        self.assertEqual(first_status, 202, first)
        self.assertEqual(fixture.call('/api/production/stop', 'jfc', {})[0], 202)
        second_status, second = fixture.call('/api/production/start', 'jfc', self.settings)
        self.assertEqual(second_status, 202, second)
        jobs = fixture.call('/api/production', 'jfc')[1]['jobs']
        self.assertEqual([(job['id'], job['status']) for job in jobs],
                         [(second['job_id'], 'queued'), (first['job_id'], 'stopped')])
        with closing(api.db()) as db, db:
            db.execute("UPDATE jobs SET stop=1,status='stopping',updated=0 WHERE id=?", (second['job_id'],))
        worker = Production(fixture.root, fixture.fixture.config, worker=True)
        with (fixture.root / 'gpu_worker.lock').open('a') as busy_gpu:
            fcntl.flock(busy_gpu, fcntl.LOCK_EX | fcntl.LOCK_NB)
            worker.recover()
        rows = fixture.call('/api/production', 'jfc')[1]['jobs']
        self.assertTrue(all(row['status'] == 'stopped' for row in rows))

    def test_scheduler_skip_and_pause_and_restart(self):
        self.p.configure(dict(enabled=True,settings=self.settings,interval_minutes=5),'tester')
        job=self.launch()
        with closing(self.p.db()) as db, db: db.execute('UPDATE schedule SET next_at=0')
        worker = Production(self.root, self.config, worker=True)
        with patch.object(worker,'launch') as launch, patch.object(worker,'_spawn'):
            worker.tick(custom=False);launch.assert_not_called()
        with closing(self.p.db()) as db:
            self.assertIn('跳过',db.execute('SELECT last_message FROM schedule').fetchone()[0])
        self.p.update(job,status='completed')
        with closing(self.p.db()) as db, db: db.execute('UPDATE schedule SET next_at=0')
        other=Production(self.root,self.config,worker=True)
        with patch.object(other,'launch') as launch:
            other.tick(custom=False);launch.assert_called_once_with({**self.settings,'continuous':False},'tester','scheduler')
        other.configure({'enabled':False},'tester')
        with patch.object(other,'launch') as launch: other.tick(custom=False);launch.assert_not_called()

    def test_stale_worker_recovery(self):
        job=self.launch()
        with closing(self.p.db()) as db, db:
            db.execute("UPDATE jobs SET identity=NULL,pid=NULL,updated=0,status='running'")
        worker = Production(self.root, self.config, worker=True)
        with patch.object(worker,'_spawn') as spawn:
            worker.recover();spawn.assert_called_once_with(job)
        with closing(self.p.db()) as db: self.assertEqual(db.execute('SELECT stage FROM jobs').fetchone()[0],'recovering')

    def test_pipeline_failure_and_cancel_before_gpu(self):
        job=self.launch()
        def api_cancel(*args,**kwargs): self.p.cancel();return False
        with patch.object(self.p,'command',side_effect=api_cancel) as command:
            self.p.execute(job);self.assertEqual(command.call_count,1)
        with closing(self.p.db()) as db:self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()[0],'stopped')
        job=self.launch()
        with patch.object(self.p,'command',side_effect=RuntimeError('test failure')):
            self.p.execute(job)
        with closing(self.p.db()) as db:self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()[0],'failed')

    def test_validation_and_process_launch_failure(self):
        for bad in [dict(k=0),dict(topics=['../bad']),dict(duration=99),dict(rounds=True)]:
            with self.assertRaises(ValueError):self.p.validate({**self.settings,**bad})
        worker = Production(self.root, self.config, worker=True)
        with patch('review_production.subprocess.Popen',side_effect=OSError('unavailable')) as popen:
            job = self.p.launch(self.settings,'tester')['job_id']
            popen.assert_not_called()
            with self.assertRaises(OSError):
                worker.recover()
        with closing(self.p.db()) as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0], 'queued')
        with patch.object(worker, '_spawn') as spawn:
            worker.recover()
            spawn.assert_called_once_with(job)

    def test_full_command_sequence_and_gpu_exclusion(self):
        folder=self.root/'topics/walking';folder.mkdir(parents=True)
        with closing(sqlite3.connect(folder/'index.sqlite3')) as db:
            db.executescript('CREATE TABLE tasks(state);CREATE TABLE shards(number,bytes);')
        job=self.launch()
        with patch.object(self.p,'command',return_value=True) as command:
            self.p.execute(job)
            self.assertEqual(command.call_count,1) # Empty queue does not launch GPU.
            self.assertIn('--enqueue',command.call_args_list[0].args[1])
        with closing(self.p.db()) as db:self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()[0],'completed')
        job=self.launch()
        with closing(sqlite3.connect(folder/'index.sqlite3')) as db, db:
            db.executemany('INSERT INTO tasks VALUES (?)', [('pending',)] * 5)
        with (self.root/'gpu_worker.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with patch.object(self.p,'command') as command,patch('production_flow.time.sleep',side_effect=lambda _:self.p.cancel()):
                self.p.execute(job);command.assert_not_called()
        with closing(self.p.db()) as db:self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()[0],'stopped')

    def test_continuous_without_rounds_and_stop_after_two_rounds(self):
        self.settings={k:v for k,v in self.settings.items() if k!='rounds'}
        self.settings['continuous']=True
        self.assertTrue(self.p.validate(self.settings)['continuous'])
        with self.assertRaises(ValueError):self.p.configure(dict(enabled=True,settings=self.settings,interval_minutes=5),'tester')
        job=self.launch();count=[]
        def command(*args):
            count.append(1)
            if len(count)==2:self.p.cancel();return False
            return True
        with patch.object(self.p,'command',side_effect=command):self.p.execute(job)
        self.assertEqual(len(count),2)
        with closing(self.p.db()) as db:self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?',(job,)).fetchone()[0],'stopped')

    def test_prompt_skip_is_recoverable_but_api_failure_is_not(self):
        job=self.launch()
        child=Mock();child.poll.return_value=3;child.returncode=3
        with patch('review_production.subprocess.Popen',return_value=child),patch('review_production.time.sleep'):
            self.assertTrue(self.p.command(job,['/project/qwen_motion_agent.py'],1))
            child.returncode=1
            with self.assertRaisesRegex(RuntimeError,'文本生成 / 语义审核'):
                self.p.command(job,['/project/qwen_motion_agent.py'],1)

    def test_legacy_batch_setting_becomes_one_prompt(self):
        self.assertEqual(self.p.validate({**self.settings,'per_topic':3})['per_topic'],1)

    def test_transient_backoff_capped_then_recovers(self):
        job=self.launch()
        with patch.object(self.p,'_command_once',side_effect=[75]*5+[True]) as run, \
                patch('review_production.time.sleep') as sleep:
            self.assertTrue(self.p.command(job,['qwen_motion_agent.py'],1))
        self.assertEqual(run.call_count,6)
        self.assertEqual(sleep.call_count,10+30+60+300+300)

    def test_stop_during_recovery_wait(self):
        job=self.launch()
        with patch.object(self.p,'_command_once',return_value=75) as run, \
                patch('review_production.time.sleep',side_effect=lambda _:self.p.cancel()):
            self.assertFalse(self.p.command(job,['qwen_motion_agent.py'],1))
        run.assert_called_once()

    def test_only_api_transient_exit_is_retryable(self):
        job=self.launch()
        child=Mock();child.poll.return_value=75;child.returncode=75
        with patch('review_production.subprocess.Popen',return_value=child):
            self.assertEqual(self.p._command_once(job,['qwen_motion_agent.py'],1),75)
            with self.assertRaisesRegex(RuntimeError,'动作生成'):
                self.p.command(job,['motion_collector.py'],1)


if __name__=='__main__':unittest.main()
