"""Personal space integration: temporary accounts, SQLite, local HTTP only."""
from contextlib import closing
import json
import unittest
import urllib.request
import urllib.error
import test_gadget_v2 as fixture
from review_access import claim_owners,remove
from review_personal import listing,save,motions

class PersonalTests(unittest.TestCase):
    setUp=fixture.GadgetTests.setUp
    tearDown=fixture.GadgetTests.tearDown
    def call(self,path='/api/personal/presets',value=None,username='alice'):
        token,_=self.auth.login(username,'test-password-123','personal-test')
        self.server.origins={'http://localhost'}
        req=urllib.request.Request(self.base.split('/api/gadget/')[0]+path,data=json.dumps(value).encode() if value is not None else None,
            headers={'Cookie':'mf_session='+token,'Origin':'http://localhost','X-MotionFarm-Request':'1','Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=4) as r:return json.load(r)
    def body(self,**changes):return dict(id='a'*32,version=0,name='行走参数',robot='unitree_g1_23dof',notes='设计稿',parameters={'smooth_weight':4},**changes)
    def test_save_read_edit_and_history(self):
        body=self.body();first=self.call(value=body);self.assertEqual(first['owner'],'alice');self.assertEqual(first['version'],1)
        self.assertEqual(self.call(value=body)['version'],1)
        second=self.call(value={**body,'version':1,'parameters':{'smooth_weight':6}});self.assertEqual(second['version'],2)
        history=self.call('/api/personal/presets?id='+body['id'])
        self.assertEqual([v['parameters']['smooth_weight'] for v in history['versions']],[4,6])
        self.assertEqual(history['preset']['execution_mode'],'supported')
        with closing(self.server.retarget.db()) as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
    def test_no_cross_account_access_even_admin(self):
        self.call(value=self.body())
        self.assertEqual(self.call(username='bob')['items'],[])
        for path,value in [('/api/personal/presets?id='+'a'*32,None),('/api/personal/presets',self.body())]:
            with self.assertRaises(urllib.error.HTTPError) as e:self.call(path,value,'bob')
            self.assertEqual(e.exception.code,403)
        self.assertEqual(listing(self.store,{'username':'bob','role':'admin'},{})['items'],[])
        with self.assertRaises(urllib.error.HTTPError) as e:self.call(value={**self.body(),'owner':'bob'})
        self.assertEqual(e.exception.code,400)
    def test_concurrent_version_conflict(self):
        self.call(value=self.body());self.call(value={**self.body(),'version':1,'name':'新名称'})
        with self.assertRaises(urllib.error.HTTPError) as e:self.call(value={**self.body(),'version':1,'name':'旧页面覆盖'})
        self.assertEqual(e.exception.code,409)
        self.assertEqual(self.call()['items'][0]['name'],'新名称')
    def test_my_generated_motions_only(self):
        claim_owners(self.root,[self.mid],'alice');claim_owners(self.root,[self.valid],'bob')
        page=self.call('/api/personal/motions?owner=bob')
        self.assertEqual([x['motion_id'] for x in page['items']],[self.mid])
        self.assertEqual([x['motion_id'] for x in motions(self.store,{'username':'alice','role':'admin'},{})['items']],[self.mid])
        self.assertNotIn('location',page['items'][0])
        remove(self.store,self.mid,{'username':'alice','role':'user'},'test')
        self.assertEqual(self.call('/api/personal/motions')['items'],[])
    def test_validation_and_version_pagination(self):
        for update in ({'parameters':[]},{'parameters':{'x':float('nan')}},{'robot':'unknown'},{'name':''},{'id':'../bad'},{'version':True}):
            with self.assertRaises(urllib.error.HTTPError) as e:self.call(value={**self.body(),**update})
            self.assertEqual(e.exception.code,400)
        user={'username':'alice'}
        for version in range(52):save(self.store,user,{**self.body(),'version':version,'parameters':{'x':version}})
        first=self.call('/api/personal/presets?id='+'a'*32);self.assertEqual(len(first['versions']),50)
        second=self.call('/api/personal/presets?id='+'a'*32+'&after='+str(first['next_after']));self.assertEqual(len(second['versions']),2)
        self.assertIsNone(second['next_after'])
    def test_preset_keyset_pagination(self):
        for i in range(52):save(self.store,{'username':'alice'},{**self.body(),'id':format(i,'032x')})
        first=self.call();self.assertEqual(len(first['items']),50)
        second=self.call('/api/personal/presets?after='+first['next_after']);self.assertEqual(len(second['items']),2)
        self.assertEqual(len({x['id'] for x in first['items']+second['items']}),52)

if __name__=='__main__':unittest.main()
