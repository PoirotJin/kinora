"""Queue-boundary tests: no model, paid API, production DB or GPU operations."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'scripts')]
import semantic_retarget
from motion_collector import collect_record
from g2h.motion_semantics import for_record


class PipelineTests(unittest.TestCase):
    def fixture(self):
        with np.load(ROOT/'scripts/fixtures/m000096vn-semantic.npz') as z:
            data={k:z[k].copy() for k in z.files}
        return dict(text=str(data['text']),duration_s=8,motion=data)

    def test_collector_checks_before_publish(self):
        record=self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'motion.npz';np.savez(p,**record['motion'])
            job=dict(id='candidate',motion=str(p),features=str(Path(tmp)/'features.pt'),
                     spec=dict(text=record['text'],duration_s=8,seed=1,cfg_scale=5.,model='HY-Motion-1.0'))
            metadata=dict(cache_profile=dict(config_sha256='fixture'),generator_checkpoint=dict(bytes=1))
            with patch('motion_collector.read_metadata',return_value=metadata):
                result=collect_record(job)
            self.assertEqual(result['quality']['direction_semantics']['status'],'fail')
            self.assertFalse(result['quality']['semantic_verified'])

    def test_original_intent_cannot_be_overridden_by_rewrite(self):
        record=self.fixture();record['text']='A person walks forward.'
        record['farm']=dict(source=dict(original_description='一个人后退。'))
        self.assertEqual(for_record(record)['status'],'fail')

    def test_bad_source_recovers_via_owned_generation_then_new_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue=MagicMock();queue.root=Path(tmp);queue.get.return_value={'cancel':0}
            queue._finish_pending_cancellation.return_value = False
            row=dict(id='a'*32,owner='alice',motion_id='m000096vn',robot='unitree_g1_23dof',execution='{}')
            custom=MagicMock();custom.db.return_value.execute.return_value.fetchone.return_value={'status':'completed','message':'完成','scene':None}
            custom.results.return_value={'items':[dict(state='failed',motion_id='m00000001'),dict(state='done',motion_id='m00000002')]}
            with patch('review_access.active_account',return_value='user'),patch('review_custom.Custom',return_value=custom),patch('semantic_retarget.source_record',return_value=self.fixture()):
                self.assertFalse(semantic_retarget.preflight(queue,row,Path(tmp)))
                request=custom.submit.call_args.args
                self.assertEqual(request[1],'alice');self.assertEqual(request[0]['n'],1)
                self.assertTrue((Path(tmp)/'source-semantic.json').exists())
                self.assertFalse(semantic_retarget.preflight(queue,row,Path(tmp)))
                child=queue.submit.call_args.args[0]
                self.assertEqual(child['motion_id'],'m00000002');self.assertNotEqual(child['id'],row['id'])
                self.assertEqual(child['parent_job_id'],row['id'])
                self.assertEqual(custom.submit.call_count,1)

    def test_scene_direction_retry_keeps_the_source_geometry(self):
        from g2h import human_interaction

        record = self.fixture()
        scene = {
            'kind': human_interaction.VERSION,
            'objects': [{'id': 'box', 'center_m': [0, .5, 2], 'size_m': [1, 1, 1], 'heading_deg': 45}],
            'waypoints': [{'frame': 0, 'xz_m': [0, 0], 'heading_deg': 0},
                          {'frame': 180, 'xz_m': [0, 1], 'heading_deg': 0, 'tolerance_m': .1}],
            'keyframes': [],
            'contacts': [{'start_frame': 180, 'end_frame': 240, 'object_id': 'box',
                          'joint_index': 21, 'anchor_m': [0, 0, -.5], 'tolerance_m': .08}],
        }
        self.assertEqual(for_record(record)['status'], 'fail')
        record['motion']['scene_spec_json'] = human_interaction.encode(scene)
        scene_report = for_record(record)
        self.assertEqual(scene_report['status'], 'review')
        self.assertEqual(scene_report['reasons'], [])
        self.assertTrue(scene_report['warnings'])
        with tempfile.TemporaryDirectory() as tmp:
            queue = MagicMock()
            queue.root = Path(tmp)
            queue.get.return_value = {'cancel': 0}
            queue._finish_pending_cancellation.return_value = False
            row = dict(id='a'*32, owner='alice', motion_id='m000096vn', robot='unitree_g1_23dof', execution='{}')
            custom = MagicMock()
            with patch('review_access.active_account', return_value='user'), \
                 patch('review_custom.Custom', return_value=custom), \
                 patch('semantic_retarget.source_record', return_value=record):
                self.assertTrue(semantic_retarget.preflight(queue, row, queue.root))
                custom.submit.assert_not_called()

                (queue.root / 'semantic-recovery.json').write_text(
                    json.dumps({'request_id': 'direction-' + row['id'], 'reason': '场景检查'}))
                custom.db.return_value.execute.return_value.fetchone.return_value = {
                    'status': 'completed', 'message': '完成', 'scene': None,
                }
                self.assertFalse(semantic_retarget.preflight(queue, row, queue.root))
                queue.submit.assert_not_called()
                self.assertIn('场景', queue.update.call_args.kwargs['message'])
                custom.db.return_value.execute.return_value.fetchone.return_value['scene'] = json.dumps(scene)
                custom.results.return_value = {'items': [{'state': 'done', 'motion_id': 'm00000002'}]}
                self.assertFalse(semantic_retarget.preflight(queue, row, queue.root))
                self.assertEqual(queue.submit.call_args.args[0]['motion_id'], 'm00000002')
                custom.db.return_value.execute.return_value.fetchone.return_value = {
                    'status': 'failed', 'message': '下游失败', 'scene': json.dumps(scene),
                }
                self.assertTrue(semantic_retarget.preflight(queue, row, queue.root))

    def test_cancel_does_not_launch_recovery(self):
        queue=MagicMock();queue.get.return_value={'cancel':1};queue.root=Path('/unused')
        with patch('review_access.active_account',return_value='user'),patch('review_custom.Custom') as custom:
            self.assertFalse(semantic_retarget.preflight(queue,dict(id='x',owner='alice',motion_id='m000096vn'),Path('/unused')))
            custom.assert_not_called()

    def test_robot_retry_is_bounded_and_keeps_identity(self):
        queue=MagicMock();queue.root=Path('/unused')
        row=dict(id='a'*32,owner='alice',motion_id='m000096vn',robot='unitree_g1_23dof',execution='{}')
        report=dict(status='fail',reasons=['要求后退'])
        with patch('review_access.active_account',return_value='user'):
            semantic_retarget.retry_robot(queue,row,report)
            value=queue.submit.call_args.args[0]
            self.assertEqual(value['parameters']['root_orientation_weight'],80.)
            self.assertNotEqual(value['id'],row['id'])
            queue.submit.reset_mock()
            row['execution']=json.dumps(dict(parent_job_id='b'*32))
            queue.get.side_effect=[dict(motion_id=row['motion_id'],execution=json.dumps(dict(parent_job_id='c'*32))),dict(motion_id=row['motion_id'],execution='{}')]
            semantic_retarget.retry_robot(queue,row,report)
            queue.submit.assert_not_called()
            self.assertIn('重试已停止',queue.update.call_args.kwargs['message'])

    def test_robot_retry_blocks_fixed_upright_v4_and_preserves_robot_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            batch=root/'exports/amp70_upright_v3'
            batch.mkdir(parents=True)
            (batch/'batch.json').write_text(json.dumps({'motions':[{'motion_id':'m000096vn'}]}))
            queue=MagicMock();queue.root=root
            row=dict(id='a'*32,owner='alice',motion_id='m000096vn',robot='unitree_g1_23dof',execution='{}')
            report=dict(status='fail',reasons=['要求后退'])
            with patch('review_access.active_account',return_value='user'):
                semantic_retarget.retry_robot(queue,row,report)
                queue.submit.assert_not_called()
                self.assertIn('固定版本求解器',queue.update.call_args.kwargs['message'])
                custom_row=dict(id='d'*32,owner='alice',motion_id='m000096vn',robot='robot_'+'1'*32,
                                execution=json.dumps({'custom_robot':{'version':'0'*64},'parameters':{}}))
                semantic_retarget.retry_robot(queue,custom_row,report)
                self.assertEqual(queue.submit.call_args.args[0]['robot_version'],'0'*64)


if __name__=='__main__':unittest.main()
