"""Two-user HTTP/Gadget access tests on synthetic data; no GPU or external APIs."""
import base64,json,urllib.request,urllib.error,unittest
from contextlib import closing
from unittest.mock import Mock
import numpy as np
import test_gadget_v2 as fixtures
from review_access import claim_owners


class SharedLibrary(unittest.TestCase):
    setUp=fixtures.GadgetTests.setUp
    tearDown=fixtures.GadgetTests.tearDown
    call=fixtures.GadgetTests.call

    def browser(self,path,value=None,username='alice'):
        if not hasattr(self,'tokens'):self.tokens={}
        if username not in self.tokens:self.tokens[username]=self.auth.login(username,'test-password-123','library-test')[0]
        self.server.origins={'http://localhost'}
        request=urllib.request.Request(self.base.split('/api/gadget/')[0]+path,
            data=json.dumps(value).encode() if value is not None else None,
            headers={'Cookie':'mf_session='+self.tokens[username],'Origin':'http://localhost','X-MotionFarm-Request':'1','Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=4) as response:
            return json.load(response) if 'json' in response.headers.get('Content-Type','') else response.read()

    def denied(self,fn,code=403):
        with self.assertRaises(urllib.error.HTTPError) as error:fn()
        self.assertEqual(error.exception.code,code)

    def test_authored_custom_is_shared_across_gallery_search_report_tags_review_and_retarget(self):
        import MotionFarm as farm
        from g2h.farm_index import register_motion
        from g2h.collection_store import CollectionStore
        db=farm.catalog(self.root)
        item=dict(id='b'*64,text='A person walks forward.',topic_id='custom',attributes={},source={})
        with db:db.execute('INSERT INTO prompts VALUES(?,?,?,?)',(item['id'],'custom',item['text'],json.dumps(item)))
        mid=register_motion(db,item['id'],'custom','custom-fixture',44);db.close()
        with CollectionStore(self.root/'topics/custom') as store:
            with store.db:store.db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)',('custom-fixture','{}'))
            store.append({'id':'custom-fixture','motion':dict(fps=np.array(30),native_keypoints3d=np.zeros((2,52,3)),native_transl=np.zeros((2,3)),smpl_poses=np.zeros((2,156)))})
        claim_owners(self.root,[mid],'bob','bob-request','custom')
        np.array([-1]+[0]*51,dtype=np.int32).tofile(self.assets/'kintree.bin')
        np.zeros((52,3),dtype=np.float32).tofile(self.assets/'j_template.bin')
        (self.assets/'joint_names.json').write_text(json.dumps(['joint'+str(n) for n in range(52)]))
        self.assertIn(mid,[r['motion_id'] for r in self.browser('/api/motions')['items']])
        self.assertIn(mid,[r['motion_id'] for r in self.browser('/api/library/search?author=bob')['items']])
        self.assertIn(mid,[r['motion_id'] for r in self.call('search?author=bob')['items']])
        self.assertEqual(self.call('motion?id='+mid)['owner'],'bob')
        self.assertGreater(len(self.call('source?id='+mid,binary=True)),0)
        self.assertEqual(self.browser('/api/motions/'+mid+'/preview')['frames'],2)
        self.assertEqual(self.browser('/api/research/preview?id='+mid)['frames'],2)
        self.assertEqual(self.browser('/api/research/report?id='+mid)['provenance']['owner'],'bob')
        tag=self.browser('/api/motions/'+mid+'/tags',{'group':'用途','tag':'协作测试'})
        self.assertEqual(tag['tags'][0]['username'],'alice')
        review=self.browser('/api/motions/'+mid+'/review',self.value)
        self.assertEqual(review['reviewer'],'alice')
        self.assertEqual(len(self.browser('/api/motions/'+mid+'/review-history')['items']),1)
        self.browser('/api/library/collections',dict(id='shared',motion_id=mid,action='add'))
        self.assertEqual(self.browser('/api/library/collections?id=shared')['items'][0]['motion_id'],mid)
        job=self.browser('/api/retarget',dict(id='4'*32,motion_id=mid,robot='unitree_g1_23dof'))
        self.assertEqual(job['owner'],'alice')
        self.denied(lambda:self.browser('/api/retarget/cancel',{'id':job['id']},username='bob'))
        self.denied(lambda:self.browser('/api/motions/'+mid+'/remove',{}))
        self.assertEqual(self.browser('/api/motions/'+mid)['owner'],'bob')

    def test_local_import_is_owner_private_in_http_and_gadget(self):
        from gadget_keys import Keys
        raw=self.call('source?id='+self.valid,binary=True)
        mid=self.call('import',dict(id='5'*32,text='Private fixture',npz_base64=base64.b64encode(raw).decode()))['motion_id']
        self.assertEqual(self.call('motion?id='+mid)['owner'],'alice')
        self.assertGreater(len(self.call('source?id='+mid,binary=True)),0)
        np.array([-1]+[0]*51,dtype=np.int32).tofile(self.assets/'kintree.bin')
        np.zeros((52,3),dtype=np.float32).tofile(self.assets/'j_template.bin')
        (self.assets/'joint_names.json').write_text(json.dumps(['joint'+str(n) for n in range(52)]))
        self.assertTrue(self.browser('/api/motions/'+mid)['read_only'])
        self.assertEqual(self.browser('/api/motions/'+mid+'/preview')['frames'],2)
        self.assertTrue(self.browser('/api/motions/'+mid+'/preview.bin').startswith(b'MFPRV001'))
        self.assertEqual(len(self.browser('/api/motions/'+mid+'/thumbnail')['joints']),52)
        self.assertEqual(self.browser('/api/research/preview?id='+mid)['frames'],2)
        bob=Keys(self.root).create('bob',{})['key']
        for endpoint in ('motion','source','report'):
            self.denied(lambda endpoint=endpoint:self.call(endpoint+'?id='+mid,key=bob))
        self.denied(lambda:self.call('retarget',dict(id='6'*32,motion_id=mid,robot='unitree_g1_23dof'),key=bob))
        for suffix in ('','/preview','/preview.bin','/thumbnail','/tags','/review-history'):
            self.denied(lambda suffix=suffix:self.browser('/api/motions/'+mid+suffix,username='bob'))
        for path in ('/api/research/report?id=','/api/research/preview?id=','/api/research/calibration?motion_id='):
            self.denied(lambda path=path:self.browser(path+mid,username='bob'))
        self.denied(lambda:self.browser('/api/motions/'+mid+'/review',self.value,username='bob'))
        self.denied(lambda:self.browser('/api/motions/'+mid+'/tags',dict(group='x',tag='x'),username='bob'))
        self.denied(lambda:self.browser('/api/library/collections',dict(id='private',motion_id=mid),username='bob'))
        self.denied(lambda:self.browser('/api/retarget',dict(id='7'*32,motion_id=mid,robot='unitree_g1_23dof'),username='bob'))
        self.assertNotIn(mid,[r['motion_id'] for r in self.browser('/api/motions',username='bob')['items']])

    def test_http_delete_own_collection_keeps_sources_and_other_user_collection(self):
        for user in ('alice','bob'):
            self.browser('/api/library/collections',dict(id='favorites',motion_id=self.valid),username=user)
        self.browser('/api/library/collections',dict(id='favorites',action='delete'))
        self.assertEqual(self.browser('/api/library/collections')['collections'],[])
        self.assertEqual(len(self.browser('/api/library/collections?id=favorites',username='bob')['items']),1)
        self.assertEqual(self.call('motion?id='+self.valid)['state'],'done')

    def test_storage_retry_is_admin_only(self):
        self.server.storage=Mock();self.server.storage.retry.return_value={'job_id':'retry-job','status':'queued'}
        self.denied(lambda:self.browser('/api/storage/retry',{'job_id':'failed-job'}))
        self.server.storage.retry.assert_not_called()
        self.auth.create('admin','test-password-123',role='admin')
        with closing(self.auth.connect()) as db,db:db.execute("UPDATE users SET must_change=0 WHERE username='admin'")
        self.assertEqual(self.browser('/api/storage/retry',{'job_id':'failed-job'},username='admin')['job_id'],'retry-job')
        self.server.storage.retry.assert_called_once_with({'job_id':'failed-job'})


if __name__=='__main__':unittest.main()
