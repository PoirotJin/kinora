"""Integrated HTTP acceptance with synthetic worker outputs, NOT live GPU acceptance.
Run: PYTHONPATH=.:scripts python -m unittest scripts.test_workflow_acceptance -v
Two real accounts/sessions, durable queues/SQLite and real binary transports.
Only embedding, generation and solver computation are replaced by small arrays.
"""
from contextlib import closing
import io,json,struct,unittest,urllib.request,urllib.error
from unittest.mock import patch
import numpy as np
import test_gadget_v2 as fixture
from review_custom import Custom
from gadget_keys import Keys
from g2h.collection_store import CollectionStore
from g2h.farm_index import register_motion

class WorkflowAcceptance(unittest.TestCase):
    setUp=fixture.GadgetTests.setUp
    tearDown=fixture.GadgetTests.tearDown
    call=fixture.GadgetTests.call

    def browser(self,name,path,value=None,binary=False):
        if not hasattr(self,'tokens'):self.tokens={}
        if name not in self.tokens:self.tokens[name]=self.auth.login(name,'test-password-123','127.0.0.1')[0]
        request=urllib.request.Request(self.base.split('/api/')[0]+path,data=None if value is None else json.dumps(value).encode(),headers={'Origin':self.base.split('/api/')[0],'Cookie':'mf_session='+self.tokens[name],'X-MotionFarm-Request':'1','Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=5) as response:return response.read() if binary else json.load(response)

    def denied(self,fn):
        with self.assertRaises(urllib.error.HTTPError) as error:fn()
        self.assertEqual(error.exception.code,403)

    def test_two_user_durable_workflow(self):
        custom=Custom(self.root)
        # Real submit + lost-response retry: same ID never creates another job.
        body=dict(request_id='e2e_alice_generate',description='A person walks slowly forward with arms relaxed and feet separated throughout the motion.',n=1,duration=2,prompt_mode='exact_english',seed=42)
        self.call('generate',body);self.call('generate',body)
        bobkey=Keys(self.root).create('bob',{})['key']
        self.denied(lambda:self.call('generate?id='+body['request_id'],key=bobkey))
        self.denied(lambda:self.call('cancel_generate',{'id':body['request_id']},key=bobkey))
        from g2h import semantic_index
        import MotionFarm as farm
        with patch.object(farm,'call_qwen',side_effect=AssertionError('exact prompt must bypass Qwen')),patch.object(semantic_index,'embed',return_value=[np.ones(512)]):custom.prepare()
        manifest=json.loads((custom.folder/body['request_id']/'manifest.json').read_text());item=manifest['prompts'][0]
        with closing(custom.db()) as db:seed=db.execute('SELECT seed FROM requests WHERE id=?',(body['request_id'],)).fetchone()[0]
        # Synthetic worker output: no HY model or retarget solver is executed.
        with closing(farm.catalog(self.root)) as db:mid=register_motion(db,item['id'],'custom','e2e_task',seed)
        motion=dict(fps=np.array(30),native_keypoints3d=np.zeros((60,52,3)),native_transl=np.zeros((60,3)),smpl_poses=np.zeros((60,156)))
        with CollectionStore(self.root/'topics/custom') as store:
            with store.db:store.db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)',('e2e_task','{}'))
            store.append(dict(id='e2e_task',motion=motion))
        custom.update(body['request_id'],status='completed',done=1)
        result=self.call('generate?id='+body['request_id']);self.assertEqual(result['items'][0]['motion_id'],mid)
        # Reconnect with a new browser session and retrieve the durable result.
        self.browser('alice','/api/auth/me');self.tokens.pop('alice')
        self.assertEqual(self.browser('alice','/api/custom/results?id='+body['request_id'])['items'][0]['motion_id'],mid)
        parents=np.array([-1]+[0]*51,dtype=np.int32);parents.tofile(self.assets/'kintree.bin')
        np.zeros((52,3),dtype=np.float32).tofile(self.assets/'j_template.bin');(self.assets/'joint_names.json').write_text(json.dumps(['joint'+str(i) for i in range(52)]))
        binary=self.browser('alice','/api/motions/'+mid+'/preview.bin',binary=True)
        self.assertEqual(binary[:8],b'MFPRV001');length=struct.unpack('<I',binary[8:12])[0];meta=json.loads(binary[12:12+length]);self.assertEqual(meta['frames'],60)
        self.assertEqual(len(binary),12+length+60*52*12)
        self.browser('alice','/api/motions/'+mid+'/tags',dict(group='QA',tag='walk'))
        tagged=self.call('search?tag_group=QA&tag=walk&tagged_by=alice')['items'];self.assertEqual([x['motion_id'] for x in tagged],[mid]);self.assertEqual(tagged[0]['tags'][0]['username'],'alice')
        shared = self.call('search?tag_group=QA&tag=walk', key=bobkey)['items']
        self.assertEqual([row['motion_id'] for row in shared], [mid])
        preset=self.browser('alice','/api/personal/presets',dict(id='a'*32,version=0,name='QA plan',robot='unitree_g1_23dof',parameters={'smooth_weight':1},notes='synthetic acceptance'))
        job=dict(id='b'*32,motion_id=mid,robot=preset['robot'],preset_id=preset['id'],preset_version=1)
        queued=self.call('retarget',job);self.assertEqual(self.call('retarget',job)['id'],queued['id'])
        self.assertEqual(json.loads(queued['execution'])['parameters']['smooth_weight'],1)
        self.denied(lambda:self.call('retarget?id='+job['id'],key=bobkey))
        self.denied(lambda:self.call('cancel',{'id':job['id']},key=bobkey))
        folder=self.server.retarget.folder/job['id'];folder.mkdir(exist_ok=True)
        np.savez(folder/'result.npz',fps=30,qpos=np.zeros((60,30)))
        self.server.retarget.update(job['id'],status='done',done=60,total=60)
        with self.assertRaises(urllib.error.HTTPError) as unverified:
            self.call('download?id='+job['id'],binary=True)
        self.assertEqual(unverified.exception.code, 400)
        (folder/'quality.json').write_text(json.dumps({
            'version': 'robot-quality-v3', 'dynamics_required': False, 'export_allowed': True,
        }))
        with patch.dict('os.environ', {'KINORA_RETARGET_REQUIRE_DYNAMICS': '0'}):
            raw=self.call('download?id='+job['id'],binary=True)
        with np.load(io.BytesIO(raw),allow_pickle=False) as data:self.assertEqual(data['qpos'].shape,(60,30))
        self.denied(lambda:self.call('download?id='+job['id'],key=bobkey))
        # Cancellation is isolated, retry retains request identity, changed args rejected.
        cancel={**body,'request_id':'e2e_cancel_generate'};self.call('generate',cancel);self.call('cancel_generate',{'id':cancel['request_id']});self.call('generate',cancel)
        self.assertEqual(self.call('generate?id='+cancel['request_id'])['job']['status'],'cancelled')
        with self.assertRaises(urllib.error.HTTPError):self.call('generate',{**cancel,'seed':43})
        cancelled={**job,'id':'c'*32};self.call('retarget',cancelled);self.call('cancel',{'id':cancelled['id']})
        self.assertEqual(self.call('retarget?id='+cancelled['id'])['status'],'cancelled')

if __name__=='__main__':unittest.main()
