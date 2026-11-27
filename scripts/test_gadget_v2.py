"""Real local HTTP + SQLite, synthetic arrays; no GPU or paid API requests."""
from contextlib import closing
import io,json,sqlite3,threading,unittest,urllib.request,urllib.error
from unittest.mock import patch
import numpy as np
from test_review_server import ReviewTests
from review_server import Server,Handler
from review_auth import Auth
from review_retarget import Retarget
from review_custom import Custom
from gadget_keys import Keys
from g2h.farm_index import register_motion
from g2h.collection_store import CollectionStore
from g2h.cold_storage import package
from review_access import claim_owners,remove

class GadgetTests(unittest.TestCase):
    def setUp(self):
        ReviewTests.setUp(self)
        self.auth=Auth(self.root/'review')
        for name in ('alice','bob'):self.auth.create(name,'test-password-123')
        with closing(self.auth.connect()) as db,db:db.execute('UPDATE users SET must_change=0')
        self.key=Keys(self.root).create('alice',{})['key']
        self.server=Server(('127.0.0.1',0),Handler);self.server.store=self.store;self.server.auth=self.auth;self.server.web=self.root;self.server.retarget=Retarget(self.root)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)+'/api/gadget/v1/'
        with closing(self.store.catalog()) as db:pass
        import MotionFarm as farm
        db=farm.catalog(self.root);self.valid=register_motion(db,'a'*64,'walking','task2',43);db.close()
        with CollectionStore(self.root/'topics/walking') as store:
            with store.db:store.db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)',('task2','{}'))
            store.append({'id':'task2','motion':dict(fps=np.array(30),native_keypoints3d=np.zeros((2,52,3)),native_transl=np.zeros((2,3)),smpl_poses=np.zeros((2,156)))})
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3);self.tmp.cleanup()
    def call(self,path,value=None,key=None,binary=False):
        request=urllib.request.Request(self.base+path,data=json.dumps(value).encode() if value is not None else None,headers={'Authorization':'Bearer '+(key or self.key),'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=4) as response:return response.read() if binary else json.load(response)
    def test_discovery_search_and_details(self):
        info=self.call('info');self.assertEqual(['exact_english'],info['prompt_modes']);self.assertEqual(info['rewrite_location'],'local_agent')
        page=self.call('search?q=walk&limit=1');self.assertEqual(len(page['items']),1);self.assertIsNotNone(page['next_after'])
        second=self.call('search?q=walk&after='+str(page['next_after']));self.assertEqual(second['items'][0]['motion_id'],self.valid)
        result=self.call('motion?id='+self.valid);self.assertEqual(result['state'],'done');self.assertNotIn('location',result)
        self.assertEqual(self.call('search?q=nevermatches')['items'],[])
    def test_authored_farm_motion_is_shared_and_removed_is_denied_everywhere(self):
        claim_owners(self.root,[self.valid],'bob')
        self.assertIn(self.valid,[v['motion_id'] for v in self.call('search')['items']])
        self.assertEqual(self.call('motion?id='+self.valid)['owner'],'bob')
        remove(self.store,self.valid,dict(role='admin',username='test'),'test')
        for path,value in [('motion?id='+self.valid,None),('source?id='+self.valid,None),('retarget',dict(id='1'*32,motion_id=self.valid,robot='unitree_g1_23dof'))]:
            with self.assertRaises(urllib.error.HTTPError) as e:self.call(path,value)
            self.assertEqual(e.exception.code,404)
        remove(self.store,self.mid,dict(role='admin',username='test'),'test')
        self.assertEqual(self.call('search')['items'],[])
    def test_source_export_hot_cold_and_roundtrip_import(self):
        raw=self.call('source?id='+self.valid,binary=True)
        from gadget_imports import validate_npz
        self.assertEqual(validate_npz(raw)['native_keypoints3d'].shape,(2,52,3))
        self.store.save(self.mid,self.value);self.store.save(self.valid,self.value)
        package(self.root,'walking',0,min_age_days=0)
        cold=self.call('source?id='+self.valid,binary=True)
        np.testing.assert_array_equal(validate_npz(raw)['smpl_poses'],validate_npz(cold)['smpl_poses'])
        import base64
        imported=self.call('import',dict(id='2'*32,text='source roundtrip',npz_base64=base64.b64encode(raw).decode()))
        self.assertEqual(self.call('motion?id='+imported['motion_id'])['owner'],'alice')
        self.assertEqual(validate_npz(self.call('source?id='+imported['motion_id'],binary=True))['native_transl'].shape,(2,3))

    def test_import_replay_restores_missing_or_damaged_source(self):
        import base64

        source = self.call('source?id=' + self.valid, binary=True)
        request = {'id': '2' * 32, 'text': 'source roundtrip',
                   'npz_base64': base64.b64encode(source).decode()}
        imported = self.call('import', request)
        motion_id = imported['motion_id']
        path = self.root / 'review/gadget/imports' / (motion_id + '.npz')
        query = 'source?id=' + motion_id
        for failure in ('missing', 'damaged'):
            with self.subTest(failure=failure):
                if failure == 'missing':
                    path.unlink()
                    with self.assertRaises(urllib.error.HTTPError) as mismatch:
                        self.call('import', {**request, 'text': 'Different description'})
                    self.assertEqual(mismatch.exception.code, 400)
                    self.assertFalse(path.exists())
                else:
                    path.write_bytes(b'\0' * len(source))
                self.assertEqual(self.call('import', request), imported)
                self.assertEqual(path.read_bytes(), source)
                self.assertEqual(self.call(query, binary=True), source)
        with closing(sqlite3.connect(path.parent / 'index.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM imports').fetchone()[0], 1)

    def test_import_explains_unreadable_archives_and_allows_corrected_retry(self):
        import base64
        from gadget_imports import Imports
        from g2h import resource_quota

        source = self.call('source?id='+self.valid,binary=True)
        imports = Imports(self.root)
        key = '5'*32
        request = {'id':key,'text':'Corrected source'}
        payload = lambda raw: dict(request,npz_base64=base64.b64encode(raw).decode())

        def used_storage():
            return resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']

        before = used_storage()
        for damaged in (b'not an NPZ', source[:-22]):
            with self.subTest(size=len(damaged)):
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.call('import',payload(damaged))
                with failure.exception as response:
                    self.assertEqual(response.code,400)
                    self.assertIn('NPZ 压缩文件',json.load(response)['error'])
                self.assertEqual(used_storage(),before)
                self.assertFalse((imports.folder/('mimp'+key+'.npz')).exists())
        saved = self.call('import',payload(source))
        self.assertEqual(self.call('import',payload(source)),saved)
        self.assertEqual(self.call('source?id='+saved['motion_id'],binary=True),source)
        self.assertEqual(used_storage(),before+len(source))

    def test_direct_import_recovery_keeps_receipt_and_downloads_the_exact_uploaded_bytes(self):
        import base64
        import hashlib
        from gadget_imports import Imports

        raw = self.call('source?id='+self.valid,binary=True)
        output = io.BytesIO()
        with np.load(io.BytesIO(raw),allow_pickle=False) as arrays:
            np.savez(output,**{name:arrays[name] for name in reversed(arrays.files)})
        uploaded = output.getvalue()
        key = 'a'*32
        payload = dict(id=key,text='Recoverable original NPZ',npz_base64=base64.b64encode(uploaded).decode())
        saved = self.call('import',payload)
        mid = saved['motion_id']
        path = Imports(self.root).folder/(mid+'.npz')
        self.assertEqual(saved['sha256'],hashlib.sha256(uploaded).hexdigest())
        with self.subTest(check='exact original bytes'):
            request = urllib.request.Request(self.base+'source?id='+mid,
                                             headers={'Authorization':'Bearer '+self.key})
            with urllib.request.urlopen(request,timeout=4) as response:
                self.assertEqual(response.headers['X-MotionFarm-Content-SHA256'],saved['sha256'])
                self.assertEqual(response.read(),uploaded)

        other = Keys(self.root).create('bob',{'scopes':['read']})['key']
        for damage in ('missing','damaged'):
            with self.subTest(damage=damage):
                if damage=='missing':
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(b'x'*len(uploaded))
                info = self.call('motion?id='+mid)
                self.assertEqual(info['motion_id'],mid)
                self.assertEqual(info['state'],'done')
                self.assertEqual(info['source_status'],damage)
                self.assertFalse(info['source_download_available'])
                self.assertEqual(info['sha256'],saved['sha256'])
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.call('source?id='+mid,binary=True)
                with failure.exception as response:
                    self.assertEqual(response.code,503)
                    self.assertEqual(json.load(response)['source_status'],damage)
                for endpoint in ('motion','source'):
                    with self.assertRaises(urllib.error.HTTPError) as denied:
                        self.call(endpoint+'?id='+mid,key=other)
                    with denied.exception as response:
                        self.assertEqual(response.code,403)
                        self.assertNotIn('source_status',json.load(response))
        self.assertEqual(self.call('import',payload),saved)
        self.assertEqual(self.call('motion?id='+mid)['source_status'],'available')
        self.assertEqual(self.call('source?id='+mid,binary=True),uploaded)

    def test_recovery_preserves_receipt_and_identifies_missing_or_damaged_npz(self):
        import base64
        from gadget_imports import Imports

        raw = self.call('source?id='+self.valid,binary=True)
        key = '4'*32
        payload = dict(id=key,text='Recovery checks',npz_base64=base64.b64encode(raw).decode())
        saved = self.call('import',payload)
        path = Imports(self.root).folder/(saved['motion_id']+'.npz')

        def assert_status(expected):
            records = self.call('recovery?kind=source')['records']
            self.assertEqual(len(records),1)
            self.assertEqual(records[0]['id'],key)
            self.assertEqual(records[0]['server_status'],'done')
            self.assertEqual(records[0]['source_status'],expected)
            self.assertNotIn(str(path),json.dumps(records))

        assert_status('available')
        with patch('review_gadget.RECOVERY_HASH_BYTES',len(raw)-1):
            assert_status('unverified')
        path.unlink()
        with patch('review_gadget.RECOVERY_HASH_BYTES',0):
            assert_status('missing')
        path.write_bytes(b'x'*len(raw))
        assert_status('damaged')
        self.assertEqual(self.call('import',payload),saved)
        assert_status('available')

    def test_import_reports_disk_pressure_for_new_and_retained_sources(self):
        import base64
        import errno
        from types import SimpleNamespace
        from g2h import capacity, resource_quota
        from gadget_imports import Imports, write_source

        source = self.call('source?id='+self.valid,binary=True)
        imports = Imports(self.root)
        capacity.ensure(self.root)
        key = '7'*32
        mid = 'mimp'+key
        path = imports.folder/(mid+'.npz')
        payload = dict(id=key,text='Disk pressure retry',npz_base64=base64.b64encode(source).decode())

        def used_storage():
            return resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']

        def expect_rejection(message):
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.call('import',payload)
            with failure.exception as response:
                self.assertEqual(response.code,503)
                error = json.load(response)['error']
                self.assertIn(message,error)
                self.assertNotIn(str(path),error)

        before = used_storage()
        with patch('g2h.capacity.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            expect_rejection('实际数据盘剩余空间不足')
        self.assertFalse(path.exists())
        self.assertEqual(used_storage(),before)
        self.assertEqual(capacity.status(self.root)['reserved_bytes'],0)

        with closing(capacity.connect(self.root)) as db, db:
            db.execute('UPDATE capacity SET sampled=0')
        expect_rejection('等待后台存储核对后重试')
        self.assertEqual(used_storage(),before)
        self.assertTrue(capacity.Reconciler(self.root).step(seconds=3))

        for error_number in (errno.ENOSPC,errno.EDQUOT):
            with self.subTest(error_number=error_number):
                def interrupted_write(destination, raw):
                    write_source(destination,raw)
                    raise OSError(error_number,'simulated write failure',str(destination))

                with patch('gadget_imports.write_source',side_effect=interrupted_write):
                    expect_rejection('服务器写入空间不足')
                self.assertFalse(path.exists())
                self.assertEqual(used_storage(),before)
                self.assertTrue(capacity.Reconciler(self.root).step(seconds=3))
                self.assertEqual(capacity.status(self.root)['reserved_bytes'],0)

        saved = self.call('import',payload)
        self.assertEqual(used_storage(),before+len(source))
        path.unlink()
        with patch('g2h.capacity.shutil.disk_usage',return_value=SimpleNamespace(free=0)):
            expect_rejection('实际数据盘剩余空间不足')
        self.assertFalse(path.exists())
        self.assertEqual(self.call('import',payload),saved)
        self.assertEqual(self.call('source?id='+mid,binary=True),source)
        self.assertEqual(used_storage(),before+len(source))

    def test_import_requires_a_synced_source_directory_before_acknowledgement(self):
        import base64
        import errno
        import os
        from g2h import capacity, resource_quota
        from gadget_imports import Imports

        source = self.call('source?id='+self.valid,binary=True)
        imports = Imports(self.root)
        key = '8'*32
        mid = 'mimp'+key
        path = imports.folder/(mid+'.npz')
        payload = dict(id=key,text='Directory sync retry',npz_base64=base64.b64encode(source).decode())
        sync = os.fsync
        folder_info = imports.folder.stat()

        def used_storage():
            return resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']

        def fail_after_publish(descriptor):
            if os.path.samestat(os.fstat(descriptor),folder_info) and path.exists():
                self.assertEqual(path.read_bytes(),source)
                raise OSError(errno.ENOSPC,'simulated directory sync error')
            return sync(descriptor)

        def expect_sync_failure():
            with patch('gadget_imports.os.fsync',side_effect=fail_after_publish):
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    self.call('import',payload)
            with failure.exception as response:
                self.assertEqual(response.code,503)
                self.assertIn('服务器写入空间不足',json.load(response)['error'])

        before = used_storage()
        expect_sync_failure()
        self.assertFalse(path.exists())
        self.assertEqual(used_storage(),before)
        with closing(imports.db()) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM imports WHERE id=?',(mid,)).fetchone())
        self.assertTrue(capacity.Reconciler(self.root).step(seconds=3))
        saved = self.call('import',payload)
        self.assertEqual(used_storage(),before+len(source))

        path.unlink()
        expect_sync_failure()
        # The repair wrote the verified bytes, but its directory sync did not complete.
        self.assertEqual(path.read_bytes(),source)
        expect_sync_failure()
        self.assertEqual(self.call('import',payload),saved)
        self.assertEqual(self.call('source?id='+mid,binary=True),source)
        self.assertEqual(used_storage(),before+len(source))

    def test_import_cleans_up_when_the_database_cannot_commit(self):
        import base64
        from g2h import capacity, resource_quota
        from gadget_imports import Imports

        source = self.call('source?id='+self.valid,binary=True)
        imports = Imports(self.root)

        def used_storage():
            return resource_quota.snapshot(self.root,'alice')['resources']['storage_bytes']['used']

        for attempt, phase in enumerate(('transaction still open','automatic rollback','after commit')):
            with self.subTest(phase=phase):
                key = f'{attempt:032x}'
                mid = 'mimp'+key
                path = imports.folder/(mid+'.npz')
                payload = dict(id=key,text='Commit retry',npz_base64=base64.b64encode(source).decode())
                before = used_storage()

                class FailingCommit(sqlite3.Connection):
                    def commit(self):
                        if self.in_transaction:
                            if phase=='automatic rollback':
                                self.rollback()
                            elif phase=='after commit':
                                super().commit()
                            raise sqlite3.OperationalError('simulated database commit failure')
                        return super().commit()

                def interrupted_db(_):
                    db = sqlite3.connect(imports.path,timeout=5,factory=FailingCommit)
                    db.row_factory = sqlite3.Row
                    return db

                with patch.object(Imports,'db',interrupted_db):
                    with self.assertRaises(urllib.error.HTTPError) as failure:
                        self.call('import',payload)
                    with failure.exception as response:
                        self.assertEqual(response.code,503)
                committed = phase=='after commit'
                with closing(imports.db()) as db:
                    row = db.execute('SELECT 1 FROM imports WHERE id=?',(mid,)).fetchone()
                self.assertEqual(row is not None,committed)
                self.assertEqual(path.exists(),committed)
                self.assertEqual(used_storage(),before+(len(source) if committed else 0))
                self.assertTrue(capacity.Reconciler(self.root).step(seconds=3))
                self.assertEqual(capacity.status(self.root)['reserved_bytes'],0)
                saved = self.call('import',payload)
                self.assertEqual(saved['motion_id'],mid)
                self.assertEqual(self.call('import',payload),saved)
                self.assertEqual(self.call('source?id='+mid,binary=True),source)
                self.assertEqual(used_storage(),before+len(source))

    def test_concurrent_identical_imports_return_the_same_receipt(self):
        import base64
        from concurrent.futures import ThreadPoolExecutor
        from gadget_imports import write_source

        source = self.call('source?id=' + self.valid, binary=True)
        request = {'id': '3' * 32, 'text': 'concurrent source import',
                   'npz_base64': base64.b64encode(source).decode()}
        writing = threading.Event()
        finish = threading.Event()
        second_validating = threading.Event()
        from gadget_imports import validate_npz

        def pause_before_write(path, raw):
            writing.set()
            if not finish.wait(3):
                raise RuntimeError('Test import was not released')
            return write_source(path, raw)

        def observe_validation(raw):
            if writing.is_set():
                second_validating.set()
            return validate_npz(raw)

        with patch('gadget_imports.write_source', side_effect=pause_before_write), \
             patch('gadget_imports.validate_npz', side_effect=observe_validation), \
             ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.call, 'import', request)
            try:
                self.assertTrue(writing.wait(2))
                second = pool.submit(self.call, 'import', request)
                second_validating.wait(.5)
            finally:
                finish.set()
            self.assertEqual(first.result(timeout=4), second.result(timeout=4))
    def test_generate_only_exact_mode_cancel_and_idempotency(self):
        body=dict(request_id='gadget_test_request',description='A person walks slowly forward with arms relaxed and feet separated throughout the motion.',n=1,duration=8,prompt_mode='exact_english')
        self.call('generate',body);self.call('generate',body)
        custom=Custom(self.root)
        import MotionFarm as farm
        from g2h import semantic_index
        with patch.object(farm,'call_qwen',side_effect=AssertionError('exact mode must not rewrite')),patch.object(semantic_index,'embed',return_value=[np.ones(512)]):custom.prepare()
        job=self.call('generate?id=gadget_test_request')['job'];self.assertEqual(job['status'],'ready');self.assertEqual(job['prompt_mode'],'exact_english')
        with closing(self.server.retarget.db()) as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
        self.call('cancel_generate',{'id':body['request_id']});self.assertEqual(self.call('generate?id='+body['request_id'])['job']['status'],'cancelled')
    def test_local_agent_default_never_rewrites_and_rejects_translate(self):
        body=dict(request_id='local_agent_default',description='A person walks slowly forward with arms relaxed and feet separated throughout the motion.',n=1,duration=2,original_description='缓慢向前走',rewrite_agent={'name':'Codex','model':'user-selected'})
        self.call('generate',body)
        row=self.call('generate?id=local_agent_default')['job']
        self.assertEqual(row['prompt_mode'],'exact_english')
        self.assertEqual(json.loads(row['rewrite_meta'])['source'],'local_agent')
        custom=Custom(self.root)
        with patch('rewrite_models.call',side_effect=AssertionError('Gadget must never rewrite')),patch('g2h.semantic_index.embed',return_value=[np.ones(512)]):custom.prepare()
        self.assertEqual(self.call('generate?id=local_agent_default')['job']['status'],'ready')
        body.update(request_id='local_translate_forbidden',prompt_mode='translate')
        with self.assertRaises(urllib.error.HTTPError) as error:self.call('generate',body)
        self.assertEqual(error.exception.code,400)

    def test_scopes_and_other_user_cancel(self):
        read=Keys(self.root).create('alice',{'scopes':['read']})['key']
        self.assertEqual(self.call('info',key=read)['gadget_version'],'0.9')
        with self.assertRaises(urllib.error.HTTPError) as e:self.call('cancel_generate',{'id':'anything'},read)
        self.assertEqual(e.exception.code,403)
        Custom(self.root).submit(dict(request_id='bob_request',description='Walk forward slowly',n=1,duration=8),'bob')
        with self.assertRaises(urllib.error.HTTPError) as e:self.call('cancel_generate',{'id':'bob_request'})
        self.assertEqual(e.exception.code,403)
    def test_retarget_independent_and_retry_stable(self):
        body=dict(id='3'*32,motion_id=self.valid,robot='unitree_g1_23dof')
        a=self.call('retarget',body);b=self.call('retarget',body);self.assertEqual(a['id'],b['id'])
        self.call('cancel',{'id':body['id']});self.assertEqual(self.call('retarget?id='+body['id'])['status'],'cancelled')

    def test_finished_retarget_only_advertises_a_download_that_passes_export_checks(self):
        from retarget_cache import seal

        key = '6'*32
        self.call('retarget',dict(id=key,motion_id=self.valid,robot='unitree_g1_23dof'))
        folder = self.server.retarget.folder/key
        folder.mkdir(exist_ok=True)
        result = folder/'result.npz'
        quality = folder/'quality.json'
        self.server.retarget.update(key,status='done')
        with patch.dict('os.environ',{'KINORA_RETARGET_REQUIRE_DYNAMICS':'0'}):
            row = self.call('retarget?id='+key)
            self.assertEqual(row['task']['status'],'succeeded')
            self.assertTrue(row['result_missing'])
            self.assertEqual(row['task']['results'],[])
            result.write_bytes(b'fixture robot artifact')
            row = self.call('retarget?id='+key)
            self.assertFalse(row['export_verified'])
            self.assertEqual(row['task']['results'],[])
            quality.write_text(json.dumps(dict(version='robot-quality-v3',dynamics_required=False,export_allowed=True)))
            row = self.call('retarget?id='+key)
            self.assertTrue(row['export_verified'])
            self.assertEqual(row['task']['results'],[dict(job_id=key,download='download?id='+key)])
            (folder/'preview.json').write_text('{}')
            seal(folder,{'key':'fixture'})
            result.write_bytes(b'damaged robot artifact')
            row = self.call('retarget?id='+key)
            self.assertTrue(row['result_corrupt'])
            self.assertEqual(row['task']['results'],[])
    def test_retarget_report_keeps_job_status_when_evidence_cannot_be_read(self):
        from retarget_cache import seal

        key = '4'*32
        self.call('retarget',dict(id=key,motion_id=self.valid,robot='unitree_g1_23dof'))
        folder = self.server.retarget.folder/key
        folder.mkdir(exist_ok=True)
        result = folder/'result.npz'
        quality = folder/'quality.json'
        self.server.retarget.update(key,status='done')
        result.write_bytes(b'damaged artifact')
        quality.write_text('{"checks":{"finite":NaN}}')
        report = self.call('retarget_report?id='+key)
        self.assertEqual(report['job']['id'],key)
        self.assertEqual(report['job']['status'],'done')
        self.assertEqual(report['quality']['status'],'unavailable')
        self.assertIsNone(report['provenance'])
        self.assertTrue(report['provenance_reason'])
        np.savez(result,metadata=json.dumps({'source':'fixture'}))
        report = self.call('retarget_report?id='+key)
        self.assertEqual(report['quality']['status'],'unavailable')
        self.assertEqual(report['provenance'],{'source':'fixture'})
        quality.unlink()
        report = self.call('retarget_report?id='+key)
        self.assertEqual(report['quality']['status'],'not_checked')
        quality.write_text(json.dumps({'status':'passed','version':'robot-quality-v3'}))
        (folder/'preview.json').write_text('{}')
        seal(folder,{'key':'fixture'})
        result.write_bytes(b'damaged sealed artifact')
        report = self.call('retarget_report?id='+key)
        self.assertTrue(report['job']['result_corrupt'])
        self.assertEqual(report['quality']['status'],'unavailable')
        self.assertIsNone(report['provenance'])

    def test_bad_parameters_fail_without_work(self):
        for path in ('search?limit=1000','search?after=-1','motion?id=../../file','source?id=../../file'):
            with self.assertRaises(urllib.error.HTTPError):self.call(path)
        with self.assertRaises(urllib.error.HTTPError):self.call('generate',dict(request_id='bad_duration',description='Walk forward slowly',n=1,duration=8.5))

    def test_harness_import_and_create_accept_large_payloads(self):
        import base64
        from g2h.motion_assets import MotionAssets

        arrays = dict(
            fps=np.array(30, dtype=np.int32),
            native_keypoints3d=np.zeros((250, 52, 3), dtype=np.float32),
            native_transl=np.zeros((250, 3), dtype=np.float32),
            smpl_poses=np.zeros((250, 156), dtype=np.float32),
        )
        source = MotionAssets(self.root).register('alice', arrays, description='Harness gadget large patch source')
        big_goal = 'walk steadily ' + ('x' * 2500)
        created = self.call('harness_create', {
            'request_id': 'gadget_harness_create_1',
            'goal': big_goal,
            'source_motion_id': source['motion_id'],
            'selection': {'start_frame': 0, 'end_frame': 250},
        })
        self.assertIn('id', created)

        rng = np.random.default_rng(7)
        buf = io.BytesIO()
        np.savez_compressed(
            buf,
            fps=np.array(30, dtype=np.int32),
            native_keypoints3d=rng.normal(size=(250, 52, 3)).astype(np.float32),
            native_transl=rng.normal(size=(250, 3)).astype(np.float32),
            smpl_poses=rng.normal(size=(250, 156)).astype(np.float32),
        )
        encoded_patch = base64.b64encode(buf.getvalue()).decode()
        self.assertGreater(len(encoded_patch), 20000)
        imported = self.call('harness_import', {
            'task_id': created['id'],
            'request_id': 'gadget_harness_patch_1',
            'npz_base64': encoded_patch,
        })
        self.assertEqual(len(imported['candidates']), 1)

if __name__=='__main__':unittest.main()
