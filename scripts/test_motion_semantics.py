import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from g2h.motion_semantics import analyze, human, robot, requirements, sequence
from g2h.semantic_queue import reject, batch_state, quarantine_bytes
from g2h.collection_store import CollectionStore


class SemanticsTests(unittest.TestCase):
    def track(self, axis=2, sign=1):
        p=np.zeros((120,3));p[:,axis]=np.arange(120)/30*sign
        f=np.tile([0.,0.,1.],(120,1))
        return p,f

    def test_backward_failure_and_success(self):
        p,f=self.track()
        self.assertEqual(analyze('A person walks backward.',p,f,30,1)['status'],'fail')
        self.assertEqual(analyze('A person walks backward.',-p,f,30,1)['status'],'pass')

    def test_left_right_and_rotation_invariance(self):
        p,f=self.track(0)
        self.assertEqual(analyze('A person steps to their left.',p,f,30,1)['status'],'pass')
        self.assertEqual(analyze('A person steps to their right.',p,f,30,1)['status'],'fail')
        rotation=np.array([[0,0,1],[0,1,0],[-1,0,0]])
        self.assertEqual(analyze('A person steps to their left.',p@rotation.T,f@rotation.T,30,1)['status'],'pass')

    def test_turn_and_stationary(self):
        p,f=self.track();p[:]=0
        angle=np.linspace(0,np.pi/2,len(p));f[:,0]=np.sin(angle);f[:,2]=np.cos(angle)
        self.assertEqual(analyze('A person turns left in place.',p,f,30,1)['status'],'pass')
        self.assertEqual(analyze('A person turns right in place.',p,f,30,1)['status'],'fail')
        p[:,0]=np.arange(len(p))/30
        self.assertEqual(analyze('A person stands still.',p,f,30,1)['status'],'fail')

    def test_no_limb_or_temporal_false_positive(self):
        for text in ['The arms swing backward.', 'A person moves their left hand.',
                     'A person walks forward then backward.', 'A person does not walk backward.']:
            self.assertEqual(requirements(text)[0],[])
        self.assertIn('backward',requirements('一个人后退。')[0])
        p,f=self.track()
        self.assertEqual(analyze('A person dances.',p,f,30,1)['status'],'review')

    def test_m0000002d_stationary_start_and_doubling_back(self):
        text=('A person explodes from a stationary crouch into a forward sprint, driving knees high and '
              'swinging arms forcefully to accelerate rapidly over eight seconds.')
        self.assertEqual(requirements(text)[0],['forward','moving','one_way'])
        # Sprints 8 m along +Z, turns around and sprints back, always facing travel.
        n=240;z=np.r_[np.linspace(0,8,120),np.linspace(8,0,120)]
        p=np.zeros((n,3));p[:,2]=z;f=np.tile([0.,0.,1.],(n,1));f[120:]*=-1
        report=analyze(text,p,f,30,1)
        # Doubling back goes to human review, never to automatic regeneration.
        self.assertEqual(report['status'],'review')
        self.assertEqual(report['reasons'],[])
        self.assertEqual([c['requirement'] for c in report['checks'] if c['status']=='review'],['one_way'])
        self.assertTrue(any('折返' in w for w in report['warnings']))
        self.assertNotIn('原地',''.join(report['warnings']))
        straight=np.zeros((n,3));straight[:,2]=np.linspace(0,16,n)
        self.assertEqual(analyze(text,straight,np.tile([0.,0.,1.],(n,1)),30,1)['status'],'pass')
        self.assertIn('stationary',requirements('A person stands still.')[0])
        self.assertIn('stationary',requirements('A person marches in place.')[0])
        self.assertNotIn('one_way',requirements('A person runs forward and comes back to the start.')[0])

    def test_vocabulary(self):
        cases={
            'A person sprints forward.':['forward','moving','one_way'],
            'A person dashes forward at full speed.':['forward','moving','one_way'],
            'A person jogs slowly and steadily forward.':['forward','moving','one_way'],
            'A person charges ahead.':['forward','moving','one_way'],
            'A person strides forward confidently.':['forward','moving','one_way'],
            'A person tiptoes backward.':['backward','moving','one_way'],
            'A person backpedals quickly.':['backward','moving','one_way'],
            'A person staggers backwards.':['backward','moving','one_way'],
            'A person backs away cautiously.':['backward','moving','one_way'],
            'A person hops forward on both feet.':['forward','moving','one_way'],
            'A person leaps forward.':['forward','moving','one_way'],
            'A person shuffles to the left.':['left','moving','one_way'],
            'A person sidesteps to their right.':['moving','one_way','right'],
            'A person crab walks to the left.':['left','moving','one_way'],
            'A person limps along.':['moving'],
            'A person marches in place.':['stationary'],
            'A person explodes into a forward dash.':['forward','moving','one_way'],
            '一个人向前冲刺。':['forward','moving','one_way'],
            '一个人向后跑。':['backward','moving','one_way'],
            '一个人向左侧步移动。':['left','moving','one_way'],
            '一个人原地慢跑。':['stationary'],
        }
        for text,want in cases.items():
            self.assertEqual(requirements(text)[0],want,text)
        for text in ['A person walks and kicks backward.','A person shifts their weight backward.',
                     'A person leans forward.','A person looks ahead.']:
            got=requirements(text)[0]
            self.assertNotIn('backward',got,text);self.assertNotIn('forward',got,text)

    def test_segment_order(self):
        p,f=self.track();p[60:,2]=p[59,2]-np.arange(60)/30
        good=[dict(text='A person walks forward.',frames=60),dict(text='A person walks backward.',frames=60)]
        self.assertEqual(sequence('',p,f,30,1,good)['status'],'pass')
        bad=[dict(text=s['text'],frames=60) for s in reversed(good)]
        result=sequence('',p,f,30,1,bad)
        self.assertEqual(result['status'],'fail');self.assertIn('第1段',result['reasons'][0])

    def test_invalid_geometry(self):
        p,f=self.track();p[0,0]=np.nan
        with self.assertRaises(ValueError):analyze('walk backward',p,f,30,1)
        p,f=self.track();f[:]=[0,1,0]
        self.assertEqual(analyze('walk backward',p,f,30,1)['status'],'review')

    def test_real_m000096vn(self):
        with np.load(Path(__file__).parent/'fixtures/m000096vn-semantic.npz',allow_pickle=False) as data:
            text=str(data['text']);a=human(text,data);b=robot(text,data['qpos'],data['fps'])
        for report in (a,b):
            self.assertEqual(report['status'],'fail')
            backward=next(c for c in report['checks'] if c['requirement']=='backward')
            self.assertEqual(backward['status'],'fail');self.assertIn('后退',backward['reason'])
            self.assertLess(report['metrics']['backward_fraction'],.1)

    def test_bounded_durable_retry_and_immutable_ids(self):
        with tempfile.TemporaryDirectory() as tmp, CollectionStore(tmp) as store:
            spec=dict(text='A person walks backward.',seed=99,topic='walking',model='test')
            current='original';seen=set()
            for attempt in range(1,4):
                with store.db:store.db.execute('INSERT OR IGNORE INTO tasks(id,spec) VALUES (?,?)',(current,json.dumps(spec)))
                source=Path(tmp)/f'{current}.npz';source.write_bytes(b'fixture')
                job=dict(id=current,spec=spec,motion=str(source))
                report=reject(store,job,dict(status='fail',reasons=['要求后退，实际前进']))
                self.assertTrue((Path(tmp)/'quarantine'/current/source.name).exists())
                self.assertFalse(source.exists());seen.add(current)
                state=batch_state(tmp,[99])[0]
                if attempt<3:
                    self.assertEqual(state['state'],'pending')
                    next_id=report['next_task_id'];self.assertNotIn(next_id,seen)
                    spec=json.loads(store.db.execute('SELECT spec FROM tasks WHERE id=?',(next_id,)).fetchone()[0]);current=next_id
                    self.assertEqual(spec['seed'],99);self.assertNotEqual(spec['inference_seed'],99)
                else:
                    self.assertIsNone(report['next_task_id']);self.assertEqual(state['state'],'failed')
            self.assertEqual(store.db.execute('SELECT count(*) FROM tasks').fetchone()[0],3)

    def test_quarantine_measurement_ignores_a_file_removed_after_listing(self):
        with tempfile.TemporaryDirectory() as tmp, CollectionStore(tmp) as store:
            with store.db:
                store.db.execute("INSERT INTO tasks(id,spec,state) VALUES (?,?,'failed')", ('first','{"seed":12}'))
            folder=Path(tmp)/'quarantine/first'
            folder.mkdir(parents=True)
            temporary=folder/'expired.npz'
            temporary.write_bytes(b'deleted artifact')
            (folder/'semantic.json').write_bytes(b'retained metadata')
            is_file, stat_file=Path.is_file, Path.stat

            def stat_after_removal(path,*args,**kwargs):
                if path==temporary:path.unlink(missing_ok=True)
                return stat_file(path,*args,**kwargs)

            with patch.object(Path,'is_file',lambda path: path==temporary or is_file(path)), \
                 patch.object(Path,'stat',stat_after_removal):
                self.assertEqual(quarantine_bytes(tmp,[12]),len(b'retained metadata'))

    def test_object_turn_does_not_trigger_body_turn_and_scene_downgrades(self):
        from g2h.motion_semantics import for_record
        self.assertNotIn('turn', requirements('A person turns a wrench to tighten two bolts.')[0])
        self.assertNotIn('turn', requirements('人物右手旋转阀门并拧螺丝。')[0])
        self.assertIn('turn', requirements('A person turns around slowly.')[0])
        self.assertIn('turn', requirements('人物原地旋转。')[0])

        with np.load(Path(__file__).parent/'fixtures/m000096vn-semantic.npz', allow_pickle=False) as data:
            motion = {k: data[k].copy() for k in data.files}
            text = str(data['text'])
        motion['scene_spec_json'] = np.frombuffer(b'{}', dtype=np.uint8)
        report = for_record(dict(text=text, motion=motion))
        self.assertEqual(report['status'], 'review')
        self.assertEqual(report['reasons'], [])
        self.assertTrue(report['warnings'])
        self.assertEqual(for_record(dict(text=text, motion=motion), qpos=motion['qpos'], fps=motion['fps'])['status'], 'fail')

    def test_reject_max_attempts_override(self):
        with tempfile.TemporaryDirectory() as tmp, CollectionStore(tmp) as store:
            spec = dict(text='A person walks backward.', seed=7, topic='custom', model='test')
            with store.db:
                store.db.execute('INSERT INTO tasks(id,spec) VALUES (?,?)', ('first', json.dumps(spec)))
            source = Path(tmp) / 'first.npz'
            source.write_bytes(b'fixture')
            r1 = reject(store, dict(id='first', spec=spec, motion=str(source)), dict(status='fail', reasons=['要求后退']), max_attempts=2)
            self.assertEqual(r1['max_attempts'], 2)
            self.assertIsNotNone(r1['next_task_id'])
            next_spec = json.loads(store.db.execute('SELECT spec FROM tasks WHERE id=?', (r1['next_task_id'],)).fetchone()[0])
            source2 = Path(tmp) / 'second.npz'
            source2.write_bytes(b'fixture')
            r2 = reject(store, dict(id=r1['next_task_id'], spec=next_spec, motion=str(source2)), dict(status='fail', reasons=['要求后退']), max_attempts=2)
            self.assertIsNone(r2['next_task_id'])


if __name__=='__main__':unittest.main()
