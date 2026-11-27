"""Real queues, collection discovery and access boundaries; no inference/API use."""
import io,json,unittest,zipfile
from contextlib import closing
import numpy as np
import test_review_server as fixtures
from review_auth import Auth
from review_custom import Custom
from review_access import Forbidden,claim_owners,remove
from review_collections import change,collections,export_collection
from gadget_imports import Imports
from gadget_library import search
from review_retarget import Retarget

TEXT='A person slowly crouches down, then rises to stand while lifting both arms above their head.'

class WorkflowRound3(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ReviewTests();self.fixture.setUp()
        self.root=self.fixture.root;self.store=self.fixture.store;self.mid=self.fixture.mid
        self.auth=Auth(self.root/'review')
        for owner in ('alice','bob'):self.auth.create(owner,'temporary-test-password')
        self.alice={'username':'alice','role':'user'};self.bob={'username':'bob','role':'user'}
        self.custom=Custom(self.root);self.retarget=Retarget(self.root)
        claim_owners(self.root,[self.mid],'bob','bob_request_01')
        self.custom.submit(self.request('bob_request_01'),'bob')
    def tearDown(self):self.fixture.tearDown()
    def request(self,key,**extra):
        return dict(request_id=key,description=TEXT,n=1,duration=2,prompt_mode='exact_english',seed=7,**extra)
    def upload(self,n=1,owner='alice',frames=60):
        buf=io.BytesIO();np.savez_compressed(buf,fps=np.array(30),native_keypoints3d=np.zeros((frames,52,3)),native_transl=np.zeros((frames,3)),smpl_poses=np.zeros((frames,156)))
        return Imports(self.root).save(owner,f'{n:032x}',TEXT,buf.getvalue())['motion_id']
    def collect(self,mid,owner=None):change(self.store,owner or self.alice,dict(id='selected',motion_id=mid))
    def query(self,owner=None,**filters):return search(self.store,owner or self.alice,{k:[str(v)] for k,v in dict(collection='selected',**filters).items()})
    def test_shared_replay_preserves_source_without_granting_private_job(self):
        result=self.custom.submit(self.request('alice_replay_01',source_motion_id=self.mid),'alice')
        with closing(self.custom.db()) as db:
            job=self.custom.authorize(db,result['request_id'],self.alice)
            self.assertEqual(json.loads(job['lineage']),{'source_motion_id':self.mid})
            self.assertEqual(job['owner'],'alice')
            with self.assertRaises(Forbidden):self.custom.authorize(db,'bob_request_01',self.alice)
        with self.assertRaises(Forbidden):self.custom.submit(self.request('alice_replay_02',source_motion_id=self.mid,parent_request_id='bob_request_01'),'alice')
    def test_replay_identity_survives_later_source_removal(self):
        body=self.request('alice_replay_01',source_motion_id=self.mid)
        self.custom.submit(body,'alice');remove(self.store,self.mid,{'username':'admin','role':'admin'})
        self.assertEqual(self.custom.submit(body,'alice')['request_id'],body['request_id'])
        with self.assertRaises(ValueError):self.custom.submit(self.request('alice_replay_02',source_motion_id=self.mid),'alice')
    def test_private_import_reference_stays_owner_only(self):
        mid=self.upload()
        self.custom.submit(self.request('alice_import_01',source_motion_id=mid),'alice')
        with self.assertRaises(Forbidden):self.custom.submit(self.request('bob_import_01',source_motion_id=mid),'bob')
        with self.assertRaises(ValueError):self.custom.submit(self.request('bad_source_01',source_motion_id='../../source'),'alice')
    def test_mixed_collection_has_no_missing_members_or_duplicate_pages(self):
        mids=[self.mid,self.upload(1),self.upload(2)]
        for mid in mids:self.collect(mid)
        seen=[];after=0
        for _ in range(4):
            data=self.query(after=after,limit=1);seen.extend(x['motion_id'] for x in data['items']);after=data['next_after']
            if after is None:break
        self.assertEqual(seen,mids);self.assertIsNone(after)
        self.assertEqual(self.query(self.bob)['items'],[])
        own=self.query(author='alice',duration_min=1,duration_max=3)
        self.assertEqual([x['motion_id'] for x in own['items']],mids[1:])
        self.assertEqual(self.query(q='unmatched keyword')['items'],[])
        self.assertEqual(self.query(status='accepted')['items'],[])
        self.assertEqual(self.query(robot='unitree_g1_23dof')['items'],[])
    def test_import_collection_search_and_export_agree(self):
        mid=self.upload();self.collect(mid)
        result=self.query(q='crouches',quality='not_checked')
        self.assertEqual([x['motion_id'] for x in result['items']],[mid])
        self.assertEqual(result['items'][0]['duration'],2)
        with zipfile.ZipFile(io.BytesIO(export_collection(self.store,self.alice,{'id':['selected']}))) as archive:
            manifest=json.loads(archive.read('manifest.json'))
        self.assertTrue(manifest['complete']);self.assertEqual(manifest['entries'][0]['motion_id'],mid)

    def test_collection_export_preserves_sources_and_valid_history_if_one_record_is_corrupt(self):
        first, second = self.upload(1), self.upload(2)
        self.collect(first)
        self.collect(second)
        with closing(self.retarget.db()) as db, db:
            db.executemany(
                'INSERT INTO jobs(id,owner,motion_id,robot,status,created,execution) VALUES(?,?,?,?,?,?,?)',
                [('broken', 'alice', first, 'unitree_g1_23dof', 'done', 2, '{'),
                 ('valid', 'alice', first, 'unitree_g1_23dof', 'done', 1, '{"scale":1}'),
                 ('private', 'bob', first, 'unitree_g1_23dof', 'done', 3, '{}')])

        raw = export_collection(self.store, self.alice, {'id': ['selected']})
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            entries = {entry['motion_id']: entry for entry in manifest['entries']}
            for motion in (first, second):
                self.assertEqual(entries[motion]['asset_status'], 'included')
                with np.load(io.BytesIO(archive.read(entries[motion]['file'])), allow_pickle=False) as data:
                    self.assertEqual(data['native_transl'].shape, (60, 3))
        self.assertFalse(manifest['complete'])
        history = entries[first]['retarget_executions']
        self.assertEqual([record['id'] for record in history], ['broken', 'valid'])
        self.assertIsNone(history[0]['execution'])
        self.assertEqual(history[1]['execution'], {'scale': 1})
        self.assertIn('retarget_history_error', entries[first])
        self.assertNotIn('retarget_history_error', entries[second])

    def test_collection_export_keeps_source_when_retarget_history_is_unreadable(self):
        motion = self.upload()
        self.collect(motion)
        with closing(self.retarget.db()) as db, db:
            db.execute('ALTER TABLE jobs RENAME TO old_jobs')

        raw = export_collection(self.store, self.alice, {'id': ['selected']})
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            entry, = manifest['entries']
            self.assertIn(entry['file'], archive.namelist())
        self.assertFalse(manifest['complete'])
        self.assertEqual(entry['retarget_executions'], [])
        self.assertIn('retarget_history_error', entry)

    def test_collection_export_and_listing_survive_missing_and_truncated_imports(self):
        intact, truncated, missing = (self.upload(n) for n in (1, 2, 3))
        for motion in (intact, truncated, missing):
            self.collect(motion)
        imports = self.root / 'review/gadget/imports'
        damaged_file = imports / (truncated + '.npz')
        damaged_file.write_bytes(damaged_file.read_bytes()[:32])
        (imports / (missing + '.npz')).unlink()

        query = {'id': ['selected']}
        raw = export_collection(self.store, self.alice, query)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            entries = {item['motion_id']: item for item in manifest['entries']}
            self.assertEqual(set(entries), {intact, truncated, missing})
            self.assertFalse(manifest['complete'])
            self.assertEqual(entries[intact]['asset_status'], 'included')
            with np.load(io.BytesIO(archive.read(entries[intact]['file'])), allow_pickle=False) as motion:
                self.assertEqual(motion['native_transl'].shape, (60, 3))
            for unavailable in (truncated, missing):
                self.assertEqual(entries[unavailable]['asset_status'], 'unavailable')
                self.assertIn('源动作', entries[unavailable]['reason'])
                self.assertNotIn('motions/' + unavailable + '.npz', archive.namelist())
        self.assertEqual([item['motion_id'] for item in collections(self.store, self.alice, query)['items']],
                         [intact])

    def test_collection_search_paginates_past_a_corrupt_import(self):
        first, damaged, malformed, last = (self.upload(n) for n in (1, 2, 3, 4))
        for motion in (first, damaged, malformed, last):
            self.collect(motion)
        imports = self.root / 'review/gadget/imports'
        path = imports / (damaged + '.npz')
        path.write_bytes(path.read_bytes()[:32])
        np.savez_compressed(imports / (malformed + '.npz'), native_transl=np.array(0))

        page = self.query(q='crouches', limit=1)
        self.assertEqual([item['motion_id'] for item in page['items']], [first])
        self.assertIsNotNone(page['next_after'])
        next_page = self.query(q='crouches', limit=1, after=page['next_after'])
        self.assertEqual([item['motion_id'] for item in next_page['items']], [last])
        self.assertEqual(next_page['scanned'], 3)
        self.assertIsNone(next_page['next_after'])

    def test_collection_listing_skips_empty_windows_and_keeps_the_cursor(self):
        first, last = self.upload(500), self.upload(600)
        self.collect(first)
        self.collect(last)
        with closing(self.store.reviews()) as db, db:
            db.executemany('INSERT INTO collection_items(owner,collection_id,motion_id,created) VALUES(?,?,?,0)',
                           [('alice','selected',f'mimp{i:032x}') for i in (*range(1,141), *range(501,541))])

        page = collections(self.store, self.alice, {'id':['selected']})
        self.assertEqual([item['motion_id'] for item in page['items']], [first])
        self.assertEqual(page['next_after'], 'mimp'+f'{509:032x}')
        tail = collections(self.store, self.alice, {'id':['selected'], 'after':[page['next_after']]})
        self.assertEqual([item['motion_id'] for item in tail['items']], [last])
        self.assertIsNone(tail['next_after'])

    def test_collection_search_advances_over_a_window_with_no_matches(self):
        from review_research import VERSION

        motion = self.upload()
        self.collect(motion)
        with closing(self.store.reviews()) as db, db:
            db.execute('UPDATE collection_items SET rowid=101 WHERE motion_id=?', (motion,))
            db.executemany('INSERT INTO collection_items(rowid,owner,collection_id,motion_id,created) VALUES(?,?,?,?,0)',
                           [(i, 'alice', 'selected', f'mimp{i+1:032x}') for i in range(1, 101)])
            db.execute('INSERT INTO quality_reports VALUES(?,?,?,?,0)', (motion, VERSION, 'issues', '{}'))

        first = self.query(quality='issues', limit=1)
        self.assertEqual(first['items'], [])
        self.assertEqual(first['scanned'], 100)
        self.assertIsNotNone(first['next_after'])
        second = self.query(quality='issues', limit=1, after=first['next_after'])
        self.assertEqual([entry['motion_id'] for entry in second['items']], [motion])
        self.assertIsNone(second['next_after'])

    def test_robot_search_without_a_history_database_returns_no_matches(self):
        robot = {'robot': ['unitree_g1_23dof']}
        with closing(self.retarget.db()) as db, db:
            db.execute('INSERT INTO jobs(id,owner,motion_id,robot,status) VALUES(?,?,?,?,?)',
                       ('history', 'alice', self.mid, robot['robot'][0], 'done'))
        self.assertEqual([item['motion_id'] for item in search(self.store, self.alice, robot)['items']],
                         [self.mid])
        self.retarget.path.rename(self.retarget.path.with_suffix('.bak'))

        result = search(self.store, self.alice, robot)

        self.assertEqual(result['items'], [])
        self.assertIsNone(result['next_after'])
        self.assertFalse(self.retarget.path.exists())

    def test_tag_filter_and_hidden_sources_are_respected(self):
        from review_tags import change as tag
        imported=self.upload();self.collect(imported);self.collect(self.mid)
        tag(self.store,self.mid,self.alice,dict(group='用途',tag='研究'))
        self.assertEqual([x['motion_id'] for x in self.query(tag='研究')['items']],[self.mid])
        remove(self.store,self.mid,{'username':'admin','role':'admin'})
        self.assertEqual([x['motion_id'] for x in self.query()['items']],[imported])
        self.assertEqual(self.query(tag='研究')['items'],[])

class PresetRound3HTTP(unittest.TestCase):
    def setUp(self):
        from test_gadget_v2 import GadgetTests
        GadgetTests.setUp(self)
    def tearDown(self):
        from test_gadget_v2 import GadgetTests
        GadgetTests.tearDown(self)
    def browser(self,*args,**kwargs):
        from test_shared_library_round2 import SharedLibrary
        return SharedLibrary.browser(self,*args,**kwargs)
    def test_archive_restore_http_and_owner_scope(self):
        import urllib.error
        body=dict(id='a'*32,version=0,name='Round3 API',robot='unitree_g1_23dof',notes='',parameters={})
        saved=self.browser('/api/personal/presets',body)
        action=dict(id=saved['id'],version=saved['version'],archive_revision=0,action='archive')
        archived=self.browser('/api/personal/presets',action)
        self.assertEqual(archived['archived'],1)
        self.assertEqual(self.browser('/api/personal/presets')['active_count'],0)
        self.assertEqual(self.browser('/api/personal/presets?archived=1')['items'][0]['id'],saved['id'])
        with self.assertRaises(urllib.error.HTTPError) as e:self.browser('/api/personal/presets',{**action,'action':'restore','archive_revision':1},username='bob')
        self.assertEqual(e.exception.code,403)
        restored=self.browser('/api/personal/presets',{**action,'action':'restore','archive_revision':1})
        self.assertEqual(restored['version'],saved['version']);self.assertEqual(restored['archived'],0)
        self.assertEqual(len(self.browser('/api/personal/presets?id='+saved['id'])['versions']),1)

if __name__=='__main__':unittest.main()
