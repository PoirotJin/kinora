"""Bounded concurrency and crash-accounting regression tests; no API/GPU calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from g2h import api_usage,resource_quota
from review_access import active_account,allowed
from review_auth import Auth
from review_custom import Custom
from review_retarget import Retarget


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.auth=Auth(self.root/'review')
        self.auth.create('alice','temporary-password-123')
        self.auth.create('bob','temporary-password-123')
        self.auth.create('jfc','temporary-password-123')
        self.api=self.root/'review/api_usage.sqlite3'

    def tearDown(self):self.temp.cleanup()

    def test_api_admission_race_never_overbooks(self):
        barrier=threading.Barrier(2)
        def attempt(_):
            with api_usage.billed_to('alice',self.api):
                barrier.wait()
                try:return api_usage.reserve_api('test','chat',1500) is not None
                except api_usage.QuotaExceeded:return False
        with patch.object(api_usage,'LIMIT_5H',2000),ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt,range(2))),1)
        self.assertEqual(api_usage.spent('alice',time.time()-60,self.api),1500)

    def test_api_settlement_survives_telemetry_failure(self):
        with api_usage.billed_to('alice',self.api),api_usage.budgeted_request('test','chat','prompt',128):
            with patch.object(api_usage,'connect',side_effect=sqlite3.OperationalError('locked')):
                self.assertFalse(api_usage.record('test','chat',{'usage':{'total_tokens':137}}))
        self.assertEqual(api_usage.spent('alice',time.time()-60,self.api),137)

    def test_unknown_failure_keeps_reservation_and_zero_usage_is_valid(self):
        with api_usage.billed_to('alice',self.api):
            try:
                with api_usage.budgeted_request('test','chat','prompt',128):raise TimeoutError()
            except TimeoutError:pass
        unknown=api_usage.spent('alice',time.time()-60,self.api)
        self.assertGreaterEqual(unknown,1152)
        with api_usage.billed_to('alice',self.api),api_usage.budgeted_request('test','chat','prompt',128):
            api_usage.record('test','chat',{'id':'zero','usage':{'total_tokens':0}})
            api_usage.record('test','chat',{'id':'zero','usage':{'total_tokens':999}})
        self.assertEqual(api_usage.spent('alice',time.time()-60,self.api),unknown)

    def test_legacy_spend_and_admin_exemption(self):
        with closing(api_usage.connect(self.api)) as db,db:
            db.execute('INSERT INTO spend(username,ts,tokens,kind) VALUES(?,?,?,?)',('alice',time.time(),79000,'chat'))
        with api_usage.billed_to('alice',self.api):
            with self.assertRaises(api_usage.QuotaExceeded):api_usage.reserve_api('test','chat',2000)
        with api_usage.billed_to('jfc',self.api):
            self.assertIsNotNone(api_usage.reserve_api('test','chat',999999))

    def test_disabled_user_cannot_reserve_api(self):
        self.auth.set_disabled('alice',True,actor='jfc')
        with api_usage.billed_to('alice',self.api):
            with self.assertRaises(PermissionError):api_usage.reserve_api('test','chat',1)

    def test_quota_denial_precedes_chat_or_embedding_network_call(self):
        import MotionFarm as farm
        from g2h import semantic_index
        key=self.root/'dummy-key';key.write_text('test-key-not-a-secret')
        with patch.object(api_usage,'LIMIT_5H',1),api_usage.billed_to('alice',self.api),patch('urllib.request.build_opener') as network:
            with self.assertRaises(api_usage.QuotaExceeded):farm.call_qwen('test',key,[{'role':'user','content':'walk'}],max_tokens=32)
            with self.assertRaises(api_usage.QuotaExceeded):semantic_index.embed(['walk'],key)
            network.assert_not_called()

    def test_resource_atomic_multi_kind_admission_and_idempotence(self):
        caps=dict(resource_quota.DEFAULTS,gpu_seconds=10,motion_seconds=10)
        barrier=threading.Barrier(2)
        def attempt(i):
            barrier.wait()
            try:
                resource_quota.reserve(self.root,str(i),'alice',{'gpu_seconds':6,'motion_seconds':6})
                return True
            except resource_quota.QuotaExceeded:return False
        with patch.object(resource_quota,'limits',return_value=caps),ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(attempt,range(2))),1)
        with closing(resource_quota.connect(self.root)) as db:
            rows=db.execute('SELECT id FROM reservations').fetchall()
            self.assertEqual(len(rows),2)
            key=rows[0][0].split(':')[0]
        resource_quota.reserve(self.root,key,'alice',{'gpu_seconds':6,'motion_seconds':6})
        resource_quota.settle(self.root,key,{'gpu_seconds':2,'motion_seconds':4})
        resource_quota.settle(self.root,key,{'gpu_seconds':0,'motion_seconds':0})
        values=resource_quota.snapshot(self.root,'alice')['resources']
        self.assertEqual(values['gpu_seconds']['used'],2)
        self.assertEqual(values['motion_seconds']['used'],4)

    def test_resource_rejection_writes_no_partial_reservation(self):
        caps=dict(resource_quota.DEFAULTS,storage_bytes=1)
        with patch.object(resource_quota,'limits',return_value=caps):
            with self.assertRaises(resource_quota.QuotaExceeded):
                resource_quota.reserve(self.root,'bad','alice',{'gpu_seconds':2,'storage_bytes':2})
        with closing(resource_quota.connect(self.root)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reservations').fetchone()[0],0)

    def custom(self,key,owner='alice'):
        queue=Custom(self.root)
        queue.submit({'request_id':key,'description':'walk backwards','n':10000,'duration':12},owner)
        return queue

    def test_disable_cancels_queue_and_stale_claim_but_preserves_running_quantum(self):
        queue=self.custom('alice_job');self.custom('bob_job_1','bob')
        queue.update('alice_job',status='ready')
        with closing(queue.db()) as db:stale=queue.ordered(db,'gpu')[0]
        self.auth.set_disabled('alice',True,actor='jfc')
        self.assertFalse(queue.claim(stale,'gpu','must not launch'))
        with closing(queue.db()) as db:
            self.assertEqual(db.execute('SELECT status,cancel FROM requests WHERE id=?',('alice_job',)).fetchone()[:],('cancelled',1))
            self.assertEqual(db.execute('SELECT status,cancel FROM requests WHERE id=?',('bob_job_1',)).fetchone()[:],('queued',0))
        self.auth.set_disabled('alice',False,actor='jfc')
        queue.update('alice_job',status='running',cancel=0)
        self.auth.set_disabled('alice',True,actor='jfc')
        with closing(queue.db()) as db:
            self.assertEqual(db.execute('SELECT status,cancel FROM requests WHERE id=?',('alice_job',)).fetchone()[:],('running',1))

    def test_worker_independently_detects_disabled_user(self):
        queue=self.custom('alice_job');queue.update('alice_job',status='ready')
        with self.auth.connect() as db:db.execute("UPDATE users SET disabled=1 WHERE username='alice'")
        with patch.object(queue,'spawn') as spawn:queue.tick(False);spawn.assert_not_called()
        self.assertEqual(queue.status({'username':'alice','role':'user'})['jobs'][0]['status'],'cancelled')

    def test_gpu_quota_wait_preserves_large_job_without_launch(self):
        queue=self.custom('alice_job');queue.update('alice_job',status='ready')
        caps=dict(resource_quota.DEFAULTS,gpu_seconds=1)
        with (self.root/'gpu_worker.lock').open('a') as lock,patch.object(resource_quota,'limits',return_value=caps),patch('g2h.cold_storage.guard'),patch('review_custom.gpu_available',return_value=(True,'')),patch('review_custom.subprocess.Popen') as spawn:
            queue.run_batch(lock.fileno());spawn.assert_not_called()
        row=queue.status({'username':'alice','role':'user'})['jobs'][0]
        self.assertEqual(row['status'],'ready');self.assertEqual(row['done'],0)
        self.assertGreater(row['retry_at'],time.time());self.assertIn('gpu_seconds',row['message'])

    def test_retarget_queue_disabled_user_cannot_submit_or_run(self):
        queue=Retarget(self.root)
        class Store:
            def detail(self,mid):return {'location':True}
        value={'id':'1'*32,'motion_id':'m00000001'};user={'username':'alice','role':'user'}
        queue.submit(value,user,Store())
        self.auth.set_disabled('alice',True,actor='jfc')
        self.assertEqual(queue.get(value['id'],user)['status'],'cancelled')
        with self.assertRaises(PermissionError):queue.submit(dict(value,id='2'*32),user,Store())

    def test_account_audit_contains_no_secret_and_binary_preview_role(self):
        self.auth.login('alice','wrong-password','127.0.0.1')
        token,_=self.auth.login('alice','temporary-password-123','127.0.0.1')
        self.auth.logout(token)
        self.auth.set_disabled('alice',True,actor='jfc',peer='127.0.0.1')
        with self.auth.connect() as db:events=db.execute('SELECT action,actor,target,outcome FROM security_events').fetchall()
        self.assertIn(('account.disable','jfc','alice','ok'),events)
        self.assertIn(('login','alice','alice','invalid_credentials'),events)
        self.assertNotIn('temporary-password-123',repr(events));self.assertNotIn(token,repr(events))
        self.assertTrue(allowed({'role':'user'},'/api/motions/m00000001/preview.bin',False))
        self.assertFalse(allowed({'role':'user'},'/api/motions/m00000001/preview.bin',True))


if __name__=='__main__':unittest.main()
