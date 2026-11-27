"""Bounded tests: mocked API/GPU; real SQLite queues and replay identities."""
from contextlib import closing,redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np
import MotionFarm as farm
from g2h import semantic_index
from review_custom import Custom,visible_topics

TEXT='A person slowly crouches down, then rises to stand while lifting both arms above their head.'


class CustomTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.c=Custom(self.root)
        from review_auth import Auth
        Auth(self.root/'review').create('jfc','temporary-password-123')
    def tearDown(self):self.tmp.cleanup()
    def submit(self,key='request_1',n=5):
        return self.c.submit(dict(request_id=key,description='先蹲下再举起双手',n=n,duration=8),'jfc')['request_id']
    def prepare(self):
        response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'supported':True,'text':TEXT,'reason':''})}}]}
        with patch.object(farm,'call_qwen',return_value=response),patch.object(semantic_index,'embed',return_value=[np.ones(512)]):self.c.prepare()
    def row(self,key='request_1'):
        with closing(self.c.db()) as db:return dict(db.execute('SELECT * FROM requests WHERE id=?',(key,)).fetchone())
    def fake_process(self,args,**kwargs):
        self.calls.append(args)
        lo=int(args[args.index('--seed-min')+1]);hi=int(args[args.index('--seed-max')+1])
        from g2h.collection_store import CollectionStore
        with CollectionStore(self.root/'topics/custom',max_bytes=1000000) as store:
            ids=[r[0] for r in store.db.execute("SELECT id FROM tasks WHERE state='pending' AND json_extract(spec,'$.seed') BETWEEN ? AND ?",(lo,hi))]
            for key in ids:store.append({'id':key,'test_fixture':True})
        class Child:
            returncode=0
            def poll(self):return 0
        return Child()
    def batches(self,n):
        with (self.root/'gpu_worker.lock').open('a') as lock,patch('review_custom.subprocess.Popen',side_effect=self.fake_process),patch('review_custom.gpu_available',return_value=(True,'')),patch('g2h.cold_storage.guard'),redirect_stdout(io.StringIO()):
            for _ in range(n):self.c.run_batch(lock.fileno())
    def test_validate_idempotency_and_seed_reservation(self):
        key=self.submit();self.submit();self.assertEqual(len(self.c.status()['jobs']),1)
        with self.assertRaises(ValueError):self.submit(n=6)
        self.submit('request_2',3)
        self.assertEqual(self.row('request_2')['seed'],5)
        with self.assertRaises(ValueError):self.submit('bad',1)
        with self.assertRaises(ValueError):self.submit('bad_count',True)
    def test_exact_n_small_batches_and_same_text_requests(self):
        self.submit();self.prepare();self.submit('request_2',2);self.prepare();self.calls=[]
        self.batches(4)
        self.assertEqual(self.row()['done'],5);self.assertEqual(self.row('request_2')['done'],2)
        self.assertTrue(all(int(a[a.index('--batch-size')+1])<=2 for a in self.calls))
        with sqlite3.connect(self.root/'prompts.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM motion_links').fetchone()[0],7)
            self.assertEqual(db.execute('SELECT count(*) FROM prompts').fetchone()[0],1)
        self.assertEqual(len(self.c.results('request_1')['items']),5)
        self.assertEqual(len(self.c.results('request_2')['items']),2)
    def test_replay_finished_batch_does_not_duplicate(self):
        self.submit(n=2);self.prepare();self.calls=[];self.batches(1)
        self.c.update('request_1',status='running',done=0);self.batches(1)
        self.assertEqual(self.row()['done'],2)
        with sqlite3.connect(self.root/'prompts.sqlite3') as db:self.assertEqual(db.execute('SELECT count(*) FROM motion_links').fetchone()[0],2)
    def test_prepare_reuses_paid_response(self):
        self.submit();self.prepare();self.c.update('request_1',status='preparing')
        with patch.object(farm,'call_qwen',side_effect=AssertionError('must reuse')),patch.object(semantic_index,'embed',side_effect=AssertionError('must reuse')):self.c.prepare()
        self.assertEqual(self.row()['status'],'ready')
    def test_cancel_does_not_generate_or_change_other_job(self):
        self.submit();self.prepare();self.submit('request_2',2);self.prepare()
        self.c.cancel('request_1');self.calls=[];self.batches(1)
        self.assertEqual(self.row()['status'],'cancelled');self.assertEqual(self.row()['done'],0)
        self.assertEqual(self.row('request_2')['done'],2)
    def test_api_outage_does_not_take_gpu(self):
        self.submit()
        with patch.object(farm,'call_qwen',side_effect=farm.TemporaryAPIError('timeout')):self.c.prepare()
        self.assertEqual(self.row()['status'],'queued');self.assertGreater(self.row()['retry_at'],self.row()['updated'])
        with patch.object(self.c,'spawn') as spawn:self.c.tick(True);spawn.assert_not_called()
    def test_active_automatic_prepares_without_gpu_spawn(self):
        self.submit()
        with patch.object(self.c,'spawn') as spawn:self.c.tick(True);spawn.assert_called_once_with('prepare')
        self.prepare()
        with patch.object(self.c,'spawn') as spawn:self.c.tick(True);spawn.assert_not_called()
        with patch.object(self.c,'spawn') as spawn:self.c.tick(False);spawn.assert_called_once_with('gpu')
    def test_custom_gpu_error_isolated(self):
        self.submit();self.prepare()
        with (self.root/'gpu_worker.lock').open('a') as lock,patch('g2h.cold_storage.guard',side_effect=RuntimeError('capacity')):
            self.c.run_batch(lock.fileno())
        self.assertEqual(self.row()['status'],'failed');self.assertEqual(self.row()['done'],0)
    def test_topic_is_not_an_automatic_topic(self):
        config=self.root/'topics.json';config.write_text('{"topics":[{"id":"walking","name":"走路"}]}')
        topics=visible_topics(config);self.assertEqual(topics[-1]['id'],'custom');self.assertTrue(topics[-1]['manual_only'])

    def test_partial_failure_counts_committed_records_even_when_task_state_failed(self):
        self.submit(n=4);self.prepare()
        from g2h.collection_store import CollectionStore
        def partial(args,**kwargs):
            with CollectionStore(self.root/'topics/custom',max_bytes=1000000) as store:
                keys=[r[0] for r in store.db.execute('SELECT id FROM tasks ORDER BY rowid')]
                store.append({'id':keys[1],'test_fixture':True})
                # An error after append (e.g. telemetry) can mark a committed task failed.
                with store.db:store.db.execute("UPDATE tasks SET state='failed'")
            return type('Child',(),{'returncode':0,'poll':lambda self:0})()
        self.fake_process=partial;self.batches(1)
        self.assertEqual((self.row()['status'],self.row()['done']),('failed',1))
        self.assertEqual(self.c.status()['jobs'][0]['done'],1)
        self.assertEqual(self.row()['n']-self.row()['done'],3)

    def partially_rejected_process(self, args, **kwargs):
        from g2h.collection_store import CollectionStore
        from g2h.semantic_queue import reject

        folder = self.root/'topics/custom'
        with CollectionStore(folder) as store:
            first, second = store.db.execute('SELECT id,spec FROM tasks ORDER BY rowid').fetchall()
            store.append({'id':first[0],'test_fixture':True})
            with store.db:
                store.db.execute("UPDATE tasks SET state='failed',error=NULL WHERE id=?", (first[0],))
            output = folder/'candidate.npz'
            output.write_bytes(b'fixture')
            reject(store, {'id':second[0],'spec':json.loads(second[1]),'motion':str(output)},
                   {'status':'fail','reasons':['要求后退，实际前进']})
        return type('Child',(),{'returncode':0,'poll':lambda self:0})()

    def test_partial_semantic_retry_uses_the_pending_candidates_parent(self):
        self.submit(n=2);self.prepare()
        generate = self.fake_process
        self.fake_process = self.partially_rejected_process
        self.batches(1)
        job, = self.c.status()['jobs']
        self.assertEqual((job['status'],job['done']),('ready',1))
        self.assertIn('要求后退，实际前进',job['message'])
        self.assertIn('等待第 2/3 次生成',job['message'])
        self.fake_process = generate
        self.calls = []
        self.batches(1)
        self.assertEqual((self.row()['status'],self.row()['done']),('completed',2))

    def test_unreadable_quarantine_keeps_known_gpu_and_motion_charges(self):
        self.submit(n=2);self.prepare()
        self.fake_process = self.partially_rejected_process
        with patch('g2h.semantic_queue.quarantine_bytes', side_effect=[0, OSError('quarantine unavailable')]):
            self.batches(1)
        with closing(sqlite3.connect(self.root/'review/resource_quota.sqlite3')) as db:
            charges = {kind: (state,charged) for kind,state,charged in db.execute('SELECT kind,state,charged FROM reservations')}
        self.assertEqual((self.row()['status'],self.row()['done']),('ready',1))
        self.assertEqual(charges['gpu_seconds'][0], 'settled')
        self.assertLess(charges['gpu_seconds'][1], 10)
        self.assertEqual(charges['motion_seconds'], ('settled',8))
        self.assertEqual(charges['storage_bytes'], ('reserved',32_000_000))

    def test_restart_with_noncontiguous_commit_schedules_only_missing_seeds(self):
        self.submit(n=4);self.prepare()
        farm.enqueue_manifest(self.root,self.c.folder/'request_1/manifest.json',[0,1])
        from g2h.collection_store import CollectionStore
        with CollectionStore(self.root/'topics/custom',max_bytes=1000000) as store:
            key=store.db.execute("SELECT id FROM tasks WHERE json_extract(spec,'$.seed')=1").fetchone()[0]
            store.append({'id':key,'test_fixture':True})
        # Simulate a crash after the second candidate committed, before the counter.
        self.c.update('request_1',status='running',done=0);self.c=Custom(self.root);self.calls=[];self.batches(2)
        ranges=[(int(a[a.index('--seed-min')+1]),int(a[a.index('--seed-max')+1])) for a in self.calls]
        self.assertEqual(ranges,[(0,0),(2,3)])
        self.assertEqual((self.row()['status'],self.row()['done']),('completed',4))
        with sqlite3.connect(self.root/'topics/custom/index.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM records').fetchone()[0],4)

    def test_cancel_during_partial_batch_preserves_progress(self):
        self.submit(n=4);self.prepare()
        from g2h.collection_store import CollectionStore
        def stopping(args,**kwargs):
            self.c.cancel('request_1',{'username':'jfc','role':'user'})
            with CollectionStore(self.root/'topics/custom',max_bytes=1000000) as store:
                key=store.db.execute('SELECT id FROM tasks ORDER BY rowid LIMIT 1').fetchone()[0]
                store.append({'id':key,'test_fixture':True})
            return type('Child',(),{'returncode':1,'poll':lambda self:1})()
        self.fake_process=stopping;self.batches(1)
        self.assertEqual((self.row()['status'],self.row()['done']),('cancelled',1))
        self.batches(1);self.assertEqual(self.row()['done'],1)

    def test_final_records_win_over_child_cleanup_failure(self):
        self.submit(n=2);self.prepare();self.calls=[];original=self.fake_process
        def cleanup_error(args,**kwargs):
            original(args,**kwargs)
            self.c.cancel('request_1',{'username':'jfc','role':'user'})
            return type('Child',(),{'returncode':1,'poll':lambda self:1})()
        self.fake_process=cleanup_error;self.batches(1)
        self.assertEqual((self.row()['status'],self.row()['done']),('completed',2))

    def test_restart_of_cancelled_quantum_counts_commits_without_launch(self):
        self.submit(n=4);self.prepare();self.calls=[];self.batches(1)
        # Crash after a durable quantum and cancellation, before writing its count.
        self.c.update('request_1',status='running',cancel=1,done=0,progress_checked=0)
        self.c=Custom(self.root);self.calls=[];self.batches(1)
        self.assertEqual(self.calls,[])
        self.assertEqual((self.row()['status'],self.row()['done']),('cancelled',2))

    def test_restart_after_all_records_committed_and_cancelled_is_completed(self):
        self.submit(n=2);self.prepare();self.calls=[];self.batches(1)
        self.c.update('request_1',status='running',cancel=1,done=0,progress_checked=0)
        self.c=Custom(self.root);self.calls=[];self.batches(1)
        self.assertEqual(self.calls,[])
        self.assertEqual((self.row()['status'],self.row()['done']),('completed',2))

    def test_task_state_without_durable_record_is_not_counted(self):
        self.submit(n=2);self.prepare()
        def uncommitted(args,**kwargs):
            with sqlite3.connect(self.root/'topics/custom/index.sqlite3') as db:
                db.execute("UPDATE tasks SET state='done'")
            return type('Child',(),{'returncode':0,'poll':lambda self:0})()
        self.fake_process=uncommitted;self.batches(1)
        self.assertEqual((self.row()['status'],self.row()['done']),('failed',0))

    def test_slow_cancel_reconciliation_cannot_overwrite_newer_batch_completion(self):
        self.submit(n=2);self.prepare();self.calls=[]
        stale=threading.Event();resume=threading.Event();errors=[];threads=[]
        read=self.c.committed_seeds;produce=self.fake_process
        def delayed_read(row):
            snapshot=read(row)
            if threading.current_thread().name=='custom-cancel-test':
                stale.set()
                if not resume.wait(5):raise AssertionError('cancel wait timed out')
            return snapshot
        def cancel():
            try:self.c.cancel('request_1',{'username':'jfc','role':'user'})
            except Exception as exc:errors.append(exc)
        def concurrent(args,**kwargs):
            thread=threading.Thread(target=cancel,name='custom-cancel-test');threads.append(thread);thread.start()
            self.assertTrue(stale.wait(5))
            return produce(args,**kwargs)
        self.fake_process=concurrent
        try:
            with patch.object(self.c,'committed_seeds',side_effect=delayed_read):
                self.batches(1)
                self.assertEqual((self.row()['status'],self.row()['done']),('completed',2))
                resume.set()
                for thread in threads:thread.join(5)
            self.assertEqual(errors,[])
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual((self.row()['status'],self.row()['done']),('completed',2))
        finally:
            resume.set()
            for thread in threads:thread.join(5)

    def test_terminal_legacy_progress_repaired_once(self):
        self.submit(n=2);self.prepare();self.calls=[];self.batches(1)
        self.c.update('request_1',status='failed',done=0,progress_checked=0)
        self.assertEqual(self.c.status()['jobs'][0]['done'],2)
        self.assertEqual(self.row()['status'],'completed')
        with patch.object(self.c,'committed_seeds',side_effect=AssertionError('already repaired')):
            self.assertEqual(self.c.status()['jobs'][0]['done'],2)

    def test_replay_after_quota_exhaustion_still_checks_identity_and_inputs(self):
        from review_auth import Auth
        from g2h import api_usage
        auth=Auth(self.root/'review');auth.create('ordinary','temporary-password-123');auth.create('other_user','temporary-password-123')
        value=dict(request_id='quota_replay_1',description=TEXT,n=2,duration=8,prompt_mode='exact_english',seed=7,
                   original_description='慢慢蹲下',rewrite_agent={'name':'test','model':'local'})
        accepted=self.c.submit(value,'ordinary',source='local_agent')
        with api_usage.billed_to('ordinary',self.root/'review/api_usage.sqlite3'):
            ticket=api_usage.reserve_api('qwen','chat',api_usage.LIMIT_5H)
            api_usage.settle_api(ticket,{'usage':{'total_tokens':api_usage.LIMIT_5H}})
        self.assertEqual(self.c.submit(value,'ordinary',source='local_agent'),accepted)
        for changed in ({'n':3},{'duration':7},{'seed':8},{'description':TEXT+' Slowly.'},
                        {'original_description':'不同来源'},{'rewrite_agent':{'name':'other'}}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                self.c.submit(dict(value,**changed),'ordinary',source='local_agent')
        with self.assertRaises(ValueError):self.c.submit(value,'other_user',source='local_agent')
        with self.assertRaises(api_usage.QuotaExceeded):self.c.submit(dict(value,request_id='quota_new_02'),'ordinary',source='local_agent')
        with closing(self.c.db()) as db:self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],1)
        auth.set_disabled('ordinary',True)
        with self.assertRaises(PermissionError):self.c.submit(value,'ordinary',source='local_agent')

    def test_replay_survives_model_retirement_but_new_submit_checks_catalog(self):
        value=dict(request_id='model_replay',description='先蹲下再举起双手',n=2,duration=8,rewrite_model='qwen-default')
        self.c.submit(value,'jfc')
        with patch('rewrite_models.resolve',side_effect=ValueError('model retired')) as resolve:
            self.assertEqual(self.c.submit(value,'jfc'),{'request_id':'model_replay'});resolve.assert_not_called()
            with self.assertRaises(ValueError):self.c.submit(dict(value,request_id='model_new_01'),'jfc')
        with self.assertRaises(ValueError):self.c.submit(dict(value,rewrite_model='other-model'),'jfc')

    def test_replay_lineage_and_metadata_are_not_replaceable(self):
        self.submit('parent_01',1);self.submit('parent_02',1)
        value=dict(request_id='child_replay',description=TEXT,n=1,duration=8,prompt_mode='exact_english',parent_request_id='parent_01')
        self.c.submit(value,'jfc')
        self.assertEqual(self.c.submit(value,'jfc'),{'request_id':'child_replay'})
        with self.assertRaises(ValueError):self.c.submit(dict(value,parent_request_id='parent_02'),'jfc')
        with self.assertRaises(ValueError):self.c.submit(dict(value,prompt_mode='translate'),'jfc')


if __name__=='__main__':unittest.main()
