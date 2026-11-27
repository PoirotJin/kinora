"""Real HTTP permission/ownership tests; bounded concurrency without API or GPU."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import fcntl
import json
import sqlite3
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock,patch
import test_review_server as fixtures
from review_server import Server,Handler
from review_auth import Auth
from review_custom import Custom


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ReviewTests();self.fixture.setUp()
        self.root=self.fixture.root;self.store=self.fixture.store
        self.auth=Auth(self.root/'review');self.tokens={}
        for username,role in [('jfc','admin'),('alice','user'),('bob','user')]:
            self.auth.create(username,'temporary-password-123',role)
            self.tokens[username]=self.auth.change_password(username,'temporary-password-123','changed-password-123')
        self.server=Server(('127.0.0.1',0),Handler)
        self.server.store=self.store;self.server.auth=self.auth;self.server.web=self.root
        self.server.production=Mock();self.server.production.status.return_value={'jobs':[],'latest':[]}
        self.server.production.launch.return_value={'job_id':'automatic_job'}
        self.server.storage=Mock();self.server.monitor=Mock()
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3);self.fixture.tearDown()
    def call(self,path,who='alice',body=None):
        headers={'Cookie':'mf_session='+self.tokens.get(who,''),'Content-Type':'application/json','X-MotionFarm-Request':'1','Origin':self.url}
        request=urllib.request.Request(self.url+path,headers=headers,data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(request,timeout=5) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as exc:return exc.code,json.load(exc)
    def job(self,key='alice_job',who='alice'):
        return self.call('/api/custom',who,dict(request_id=key,description='Slowly crouch down and stand back up.',n=2,duration=8))
    def test_role_boundary_and_no_self_promotion(self):
        self.server.storage.status.return_value={'jobs':[],'next_after':None}
        self.assertEqual(self.call('/api/storage'),(200,{'jobs':[],'next_after':None}))
        self.server.storage.status.assert_called_once_with(0)
        for path in ['/api/production','/api/monitor','/api/agent','/api/export','/api/users','/api/removed']:
            self.assertEqual(self.call(path)[0],403,path)
        for path in ['/api/production/start','/api/production/stop','/api/production/schedule','/api/storage','/api/storage/package','/api/storage/settings','/api/storage/retry','/api/users','/api/motions/'+self.fixture.mid+'/remove']:
            self.assertEqual(self.call(path,body={'role':'admin'})[0],403,path)
        self.server.production.launch.assert_not_called()
        self.server.storage.launch.assert_not_called();self.server.storage.configure.assert_not_called();self.server.storage.retry.assert_not_called()
        code,review=self.call('/api/motions/'+self.fixture.mid+'/review',body={**self.fixture.value,'reviewer':'jfc'})
        self.assertEqual(code,200);self.assertEqual(review['reviewer'],'alice')
        self.assertEqual(self.call('/api/motions/'+self.fixture.mid+'/review',body={'role':'admin'})[0],400)
        self.assertEqual(self.call('/api/production/start','jfc',{})[0],202)
        self.assertEqual(self.call('/api/auth/me')[1]['user']['role'],'user')
        self.assertEqual(self.call('/api/users','jfc',{'username':'new_member','role':'admin'})[0],201)
        self.assertEqual(next(u for u in self.auth.users() if u['username']=='new_member')['role'],'user')
    def test_private_jobs_results_and_cancellation(self):
        self.assertEqual(self.job()[0],202);self.assertEqual(self.job('bob_job_1','bob')[0],202)
        self.assertEqual(len(self.call('/api/custom')[1]['jobs']),1)
        self.assertEqual(len(self.call('/api/custom','jfc')[1]['jobs']),2)
        self.assertEqual(self.call('/api/custom/results?id=bob_job_1')[0],403)
        self.assertEqual(self.call('/api/custom/cancel',body={'request_id':'bob_job_1'})[0],403)
        self.assertEqual(self.call('/api/custom/cancel','jfc',{'request_id':'bob_job_1'})[0],202)
        self.assertEqual(self.call('/api/custom/results?id=alice_job')[0],200)
        self.assertEqual(self.call('/api/custom','unknown')[0],401)
    def test_parallel_idempotency_and_user_quotas(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.job(),range(8)))
        self.assertTrue(all(code == 202 for code, _ in results), results)
        self.assertEqual(len(self.call('/api/custom')[1]['jobs']),1)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda i:self.job('job_long_'+str(i),'alice' if i%2 else 'bob'),range(20)))
        self.assertEqual(sum(code==202 for code,_ in results),9)
        custom=Custom(self.root)
        with closing(custom.db()) as db:
            rows=db.execute('SELECT seed,n FROM requests ORDER BY seed').fetchall()
        self.assertEqual(len(rows),10)
        self.assertTrue(all(a['seed']+a['n']<=b['seed'] for a,b in zip(rows,rows[1:])))
    def test_user_fairness_and_global_queue_positions(self):
        for i in range(4):self.job('alice_job_'+str(i))
        self.job('bob_first','bob')
        custom=Custom(self.root)
        with closing(custom.db()) as db,db:db.execute("UPDATE requests SET status='ready'")
        with closing(custom.db()) as db:order=custom.ordered(db,'gpu')
        self.assertEqual([r['owner'] for r in order[:2]],['alice','bob'])
        bob=self.call('/api/custom','bob')[1]['jobs'][0]
        self.assertEqual(bob['queue_position'],2)
        with closing(custom.db()) as db,db:db.execute("INSERT INTO user_turns(owner,gpu) VALUES('alice',?)",(time.time(),))
        self.assertEqual(self.call('/api/custom','bob')[1]['jobs'][0]['queue_position'],1)
    def test_resource_pressure_waits_without_spawning(self):
        self.job();custom=Custom(self.root);custom.update('alice_job',status='ready')
        with (self.root/'gpu_worker.lock').open('a') as lock,patch('review_custom.gpu_available',return_value=(False,'waiting for VRAM')),patch('review_custom.subprocess.Popen') as spawn,patch('g2h.cold_storage.guard'):
            custom.run_batch(lock.fileno());spawn.assert_not_called()
        row=self.call('/api/custom')[1]['jobs'][0]
        self.assertEqual(row['status'],'ready');self.assertEqual(row['done'],0)
    def test_consumer_and_preparation_lock_exclusion(self):
        self.job();custom=Custom(self.root);custom.update('alice_job',status='ready')
        with (custom.folder/'batch.lock').open('a') as gate,patch('review_custom.subprocess.Popen') as spawn:
            fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
            custom.run_batch(0);spawn.assert_not_called()
        with (custom.folder/'prepare.lock').open('a') as gate,patch('review_custom.subprocess.Popen') as spawn:
            fcntl.flock(gate,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:custom.spawn('prepare'),range(4)))
            spawn.assert_not_called()
    def test_cancel_wins_before_claim(self):
        self.job();custom=Custom(self.root);custom.update('alice_job',status='ready')
        with closing(custom.db()) as db:row=custom.ordered(db,'gpu')[0]
        custom.cancel('alice_job',{'username':'alice','role':'user'})
        self.assertFalse(custom.claim(row,'gpu','must not start'))
        self.assertEqual(self.call('/api/custom')[1]['jobs'][0]['status'],'cancelled')
    def test_gallery_collaborative_review_and_reversible_removal(self):
        mid=self.fixture.mid
        self.store.save(mid,{**self.fixture.value,'notes':'shared reviewer notes'})
        code,detail=self.call('/api/motions/'+mid)
        self.assertEqual(code,200);self.assertEqual(len(detail['history']),1)
        self.assertEqual(detail['history'][0]['notes'],'shared reviewer notes')
        self.assertEqual(detail['review']['reviewer'],'test')
        self.assertEqual(detail['prompt'],{'text':'A person walks forward.'})
        self.assertIs(detail['location'],True);self.assertIsNone(detail['screening'])
        for key in ('task_id','text_id','seed','error'):self.assertNotIn(key,detail)
        code,second=self.call('/api/motions/'+mid+'/review',body={**self.fixture.value,'revision':1,'notes':'second opinion','reviewer':'jfc'})
        self.assertEqual(code,200);self.assertEqual(second['reviewer'],'alice')
        bob=self.call('/api/motions/'+mid,'bob')[1]
        self.assertEqual([r['reviewer'] for r in bob['history']],['alice','test'])
        self.assertEqual(bob['review']['notes'],'second opinion')
        self.assertEqual(self.call('/api/motions')[1]['items'][0]['review'],{'status':'accepted'})
        before=self.store.preview(mid)
        self.assertEqual(self.call('/api/motions/'+mid+'/remove','jfc',{})[0],200)
        self.assertEqual(self.call('/api/motions')[1]['items'],[])
        for suffix in ['', '/preview','/thumbnail']:
            self.assertEqual(self.call('/api/motions/'+mid+suffix)[0],404)
        self.assertEqual(self.call('/api/motions/'+mid+'/restore',body={})[0],403)
        self.assertEqual(self.call('/api/motions/'+mid+'/restore','jfc',{})[0],200)
        self.assertEqual(self.store.preview(mid),before)
        with closing(self.store.reviews()) as db:self.assertEqual(db.execute('SELECT count(*) FROM removal_events').fetchone()[0],2)
    def test_existing_jfc_migrates_without_promoting_others(self):
        path=self.root/'legacy';path.mkdir()
        with closing(sqlite3.connect(path/'accounts.sqlite3')) as db, db:
            db.execute('CREATE TABLE users(username TEXT PRIMARY KEY,salt TEXT,digest TEXT,must_change INTEGER)')
            db.executemany('INSERT INTO users VALUES(?,?,?,0)',[('jfc','salt','digest'),('other','salt','digest')])
        auth=Auth(path);self.assertEqual({u['username']:u['role'] for u in auth.users()},{'jfc':'admin','other':'user'})
        with closing(auth.connect()) as db, db:
            db.execute("UPDATE users SET role='user' WHERE username='jfc'")
        self.assertEqual(Auth(path).users()[0]['role'],'admin') # jfc is always re-asserted admin on load.

    def test_restore_keeps_collection_membership_when_the_motion_index_is_offline(self):
        mid = self.fixture.mid
        base = '/api/motions/' + mid
        collection = '/api/library/collections'
        self.assertEqual(self.call(collection, body={'id': 'kept', 'action': 'add', 'motion_id': mid})[0], 200)
        self.assertEqual(self.call(base + '/remove', 'jfc', {}), (200, {'ok': True, 'motion_id': mid, 'removed': True}))
        self.assertEqual(self.call(collection + '?id=kept')[1]['items'], [])
        index = self.root / 'topics/walking/index.sqlite3'
        offline = index.with_suffix('.offline')
        index.rename(offline)
        try:
            self.assertEqual(self.call(base + '/restore', 'alice', {})[0], 403)
            for _ in range(2):
                self.assertEqual(self.call(base + '/restore', 'jfc', {}),
                                 (200, {'ok': True, 'motion_id': mid, 'removed': False}))
            self.assertEqual(self.call('/api/removed', 'jfc')[1]['items'], [])
        finally:
            offline.rename(index)
        self.assertEqual(self.call(collection + '?id=kept')[1]['items'][0]['motion_id'], mid)
        self.assertEqual(self.call(base + '/preview')[0], 200)
        with closing(self.store.reviews()) as db:
            self.assertEqual(db.execute('SELECT action FROM removal_events ORDER BY id').fetchall(), [('remove',), ('restore',)])

    def test_add_to_an_existing_collection_cannot_recreate_it_after_deletion(self):
        route = '/api/library/collections'
        mid = self.fixture.mid
        stale = {'action':'add','id':'selected','motion_id':mid,'require_existing':True}
        self.assertEqual(self.call(route,body={'action':'create','id':'selected','name':'Selected'})[0],200)
        self.assertEqual(self.call(route,body=stale)[0],200)
        self.assertEqual(self.call(route,body=stale)[0],200)
        self.assertEqual(self.call(route,body={'action':'remove','id':'selected','motion_id':mid})[0],200)
        self.assertEqual(self.call(route,body=stale)[0],200)
        self.assertEqual(self.call(route+'?id=selected')[1]['items'][0]['motion_id'],mid)
        self.assertEqual(self.call(route,body={'action':'delete','id':'selected'})[0],200)
        for _ in range(2):
            status, error = self.call(route,body=stale)
            self.assertEqual(status,409,error)
            self.assertIn('集合已删除',error['error'])
        self.assertEqual(self.call(route)[1]['collections'],[])
        with closing(self.store.reviews()) as db:
            self.assertEqual(db.execute('SELECT action FROM collection_events ORDER BY id').fetchall(),
                             [('add',),('remove',),('add',),('delete',)])
        self.assertEqual(self.call(route,body={'id':'favorites','motion_id':mid})[0],200)
        self.assertEqual(self.call(route+'?id=favorites')[1]['items'][0]['motion_id'],mid)

    def test_concurrent_startup_can_migrate_the_same_account_database(self):
        path = self.root / 'concurrent-legacy'
        path.mkdir()
        with closing(sqlite3.connect(path / 'accounts.sqlite3')) as db, db:
            db.execute('CREATE TABLE users(username TEXT PRIMARY KEY, salt TEXT, digest TEXT, must_change INTEGER)')
            db.executemany('INSERT INTO users VALUES(?,?,?,0)',
                           [('jfc', 'salt', 'digest'), ('member', 'salt', 'digest')])

        workers = 6
        start = threading.Barrier(workers)

        def read_accounts(_):
            start.wait(timeout=5)
            return {row['username']: row['role'] for row in Auth(path).users()}

        with ThreadPoolExecutor(max_workers=workers) as pool:
            accounts = list(pool.map(read_accounts, range(workers)))
        self.assertEqual(accounts, [{'jfc': 'admin', 'member': 'user'}] * workers)

    def test_admin_disable_kick_reset_and_protected_jfc(self):
        self.assertEqual(self.call('/api/users/alice/disable','alice',{})[0],403)
        self.assertEqual(self.call('/api/users/jfc/disable','jfc',{})[0],400)
        self.assertEqual(self.call('/api/users/alice/disable','jfc',{})[0],200)
        self.assertEqual(self.call('/api/auth/me','alice')[0],401)
        self.assertEqual(self.call('/api/custom','alice',dict(request_id='blocked_job',description='Slowly crouch down and stand back up.',n=1,duration=8))[0],401)
        self.assertEqual(self.call('/api/users/alice/enable','jfc',{})[0],200)
        password=self.call('/api/users/alice/reset','jfc',{})[1]['temporary_password']
        self.assertGreaterEqual(len(password),12)
        self.assertEqual(self.call('/api/auth/me','alice')[0],401)
        from review_auth import Auth
        token,user=self.auth.login('alice',password,'local')
        self.assertTrue(user['must_change'])
        self.tokens['alice']=token
        self.assertEqual(self.call('/api/users/alice/kick','jfc',{})[0],200)
        self.assertEqual(self.call('/api/auth/me','alice')[0],401)

    def test_listing_hides_internal_fields_and_filters_owner(self):
        self.job();custom=Custom(self.root)
        with closing(custom.db()) as db:row=dict(db.execute('SELECT * FROM requests').fetchone())
        from review_access import claim_owners
        claim_owners(self.root,[self.fixture.mid],'alice',row['id'],'custom')
        code,body=self.call('/api/motions')
        self.assertEqual(code,200);item=body['items'][0]
        self.assertNotIn('seed',item);self.assertNotIn('task_id',item);self.assertNotIn('error',item)
        self.assertEqual(item['owner'],'alice')
        self.assertEqual(self.call('/api/motions?owner=me')[1]['items'][0]['motion_id'],self.fixture.mid)
        self.assertEqual(self.call('/api/motions?owner=me','bob')[1]['items'],[])
        admin_item=self.call('/api/motions','jfc')[1]['items'][0]
        self.assertIn('seed',admin_item)

    def test_user_quota_and_hidden_queue_counts(self):
        from g2h.api_usage import connect,LIMIT_5H
        path=self.root/'review/api_usage.sqlite3'
        with closing(connect(path)) as db, db:
            db.execute('INSERT INTO spend(username,ts,tokens,kind) VALUES(?,?,?,?)',('alice',time.time(),LIMIT_5H,'chat'))
        self.assertEqual(self.call('/api/custom',body=dict(request_id='quota_job',description='Slowly crouch down and stand back up.',n=1,duration=8))[0],429)
        code,body=self.call('/api/custom')
        self.assertEqual(code,200);self.assertNotIn('gpu_jobs',body['queue']);self.assertTrue(body['quota']['limited'])
        admin=self.call('/api/custom','jfc')[1]
        self.assertIn('gpu_jobs',admin['queue']);self.assertFalse(admin['quota']['limited'])

    def test_write_requires_allowlisted_origin(self):
        self.assertEqual(self.call('/api/custom',body=dict(request_id='origin_job',description='Slowly crouch down and stand back up.',n=1,duration=8))[0],202)
        headers={'Cookie':'mf_session='+self.tokens['alice'],'Content-Type':'application/json','X-MotionFarm-Request':'1','Origin':'http://evil.invalid'}
        request=urllib.request.Request(self.url+'/api/custom',headers=headers,data=json.dumps(dict(request_id='evil_job',description='Slowly crouch down and stand back up.',n=1,duration=8)).encode())
        with self.assertRaises(urllib.error.HTTPError) as exc: urllib.request.urlopen(request,timeout=5)
        self.assertEqual(exc.exception.code,403)


if __name__=='__main__':unittest.main()
