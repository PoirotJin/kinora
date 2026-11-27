"""Real SQLite edit queue; mock only model/API/resource execution."""
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, Mock
import numpy as np
from g2h.harness import Store
from g2h.motion_assets import MotionAssets, load_numeric_npz
from g2h.kimodo_edit import VERSION, window, compose, read_source
from g2h.generation_backend import KIMODO_MODEL
from inference_metadata import digest, write_json, metadata_path
from review_auth import Auth
from harness_service import HarnessService
from review_motion_edits import MotionEdits
from test_kimodo_edit import fixture, generated

PROMPT='A person raises the left arm gently while continuing to move forward.'


class QueueFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.queue=MotionEdits(self.root)
        Auth(self.root/'review').create('alice','fixture-password-123')
        self.arrays,self.rest,self.parents=fixture()
        self.assets=MotionAssets(self.root);self.parent=self.assets.register('alice',self.arrays)
        self.task=Store(self.root).create_task('alice','create_edit_test',PROMPT,
            source_motion_id=self.parent['motion_id'],selection={'start_frame':20,'end_frame':70})
        self.ready=patch('review_motion_edits.kimodo_edit_readiness',return_value=True)
        self.ready.start();self.addCleanup(self.ready.stop)
        self.capacity=Mock()
        self.patches=[patch('g2h.cold_storage.guard'),patch('g2h.capacity.reservation',side_effect=lambda *a,**k:nullcontext(self.capacity))]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def submit(self, key='hedit_request_123', **kwargs):
        return self.queue.submit('alice',self.task['id'],key,self.parent['motion_id'],self.task['selection'],kwargs.pop('prompt',PROMPT),**kwargs)

    def fake_worker(self, root, batch, gpu_fd, **kwargs):
        job=json.loads(Path(batch).read_text())[0]
        source,plan=read_source(job['edit'])
        result,report=compose(source,generated(source,plan,self.rest,self.parents),20,70,plan,self.rest,self.parents)
        if getattr(self,'fail_gate',False):report['passed']=False;report['failures']=['过渡不符合要求']
        if getattr(self,'corrupt_outside',False):result['native_transl'][0,0]=10
        np.savez_compressed(job['motion'],**result)
        write_json(metadata_path(job['motion']),{'generator_name':KIMODO_MODEL,'edit_request_sha256':digest(job['edit']),'editing':report})
        return {'model':KIMODO_MODEL}


class EditQueueTests(QueueFixture, unittest.TestCase):
    def test_splice_gap_follows_planned_root_path(self):
        self.parent=self.assets.register('alice',self.arrays,request_id='splice-join:fixture',
            provenance={'tool':'splice_motions','join':{'selection':{'start_frame':20,'end_frame':70}}})
        self.submit();self.queue.prepare()
        self.assertIs(self.queue.get('alice','hedit_request_123')['request']['follow_root'],True)
        with patch('inference_worker.request_batch',side_effect=self.fake_worker) as generate:self.queue.run()
        self.assertIs(json.loads(Path(generate.call_args.args[1]).read_text())[0]['edit']['follow_root'],True)
        self.assertEqual(self.queue.get('alice','hedit_request_123')['status'],'completed')
        # Same splice asset, different selection: an ordinary repair, no path constraint.
        other=self.queue.submit('alice',self.task['id'],'hedit_request_456',self.parent['motion_id'],{'start_frame':10,'end_frame':40},PROMPT)
        self.assertNotIn('follow_root',other['request'])

    def test_queue_through_candidate_keeps_parent_unchanged(self):
        self.submit();self.queue.prepare()
        self.assertNotIn('follow_root',self.queue.get('alice','hedit_request_123')['request'])
        with patch('inference_worker.request_batch',side_effect=self.fake_worker) as generate:self.queue.run()
        generate.assert_called_once()
        self.assertNotIn('follow_root',json.loads(Path(generate.call_args.args[1]).read_text())[0]['edit'])
        self.assertFalse(generate.call_args.kwargs["retry_unfinished"])
        row=self.queue.get('alice','hedit_request_123');self.assertEqual(row['status'],'completed',row['message'])
        result=self.assets.load('alice',row['result']['asset']['motion_id'])[0]
        for k in ('native_transl','smpl_poses','native_keypoints3d'):
            np.testing.assert_array_equal(result[k][:20],self.arrays[k][:20]);np.testing.assert_array_equal(result[k][70:],self.arrays[k][70:])
        self.assertEqual(self.assets.get('alice',self.parent['motion_id'])['sha256'],self.parent['sha256'])
        self.assertTrue(row['result']['validation']['passed'])

    def test_stable_id_cannot_change_prompt_or_owner(self):
        first=self.submit();self.assertEqual(self.submit()['id'],first['id'])
        with self.assertRaises(ValueError):self.submit(prompt='A person walks backward slowly.')
        with self.assertRaises(KeyError):self.queue.get('bob',first['id'])

    def test_cancelled_work_never_calls_model(self):
        self.submit();self.queue.cancel_task('alice',self.task['id']);self.queue.prepare()
        with patch('inference_worker.request_batch') as generate:self.queue.run()
        generate.assert_not_called()
        row = self.queue.get('alice', 'hedit_request_123')
        self.assertEqual((row['status'], row['message']), ('cancelled', '修复已停止'))

    def test_cancelling_a_task_prevents_an_in_flight_submit_from_creating_a_job(self):
        service = HarnessService(self.root)
        cancel = MotionEdits.cancel_task

        def cancel_and_deliver_late_submission(queue, owner, task_id):
            cancel(queue, owner, task_id)
            try:
                self.submit()
            except ValueError:
                pass

        with patch.object(MotionEdits, 'cancel_task', cancel_and_deliver_late_submission):
            task = service.cancel({'username': 'alice'}, self.task['id'])
        self.assertEqual(task['status'], 'cancelled')
        with closing(self.queue.db()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    def test_worker_honours_task_cancellation_even_if_queue_notification_was_interrupted(self):
        self.submit()
        self.queue.prepare()
        Store(self.root).cancel_task('alice', self.task['id'])
        with patch('inference_worker.request_batch', side_effect=self.fake_worker) as generate:
            self.queue.run()
        generate.assert_not_called()
        self.assertEqual(self.queue.get('alice', 'hedit_request_123')['status'], 'cancelled')

    def test_cancel_after_generation_before_publication_keeps_candidate_out_of_library(self):
        job = self.submit()
        self.queue.prepare()
        service = HarnessService(self.root)
        register = MotionAssets.register

        def cancel_before_registration(assets, owner, arrays, **options):
            if options.get('request_id') == job['id']:
                service.cancel({'username': 'alice'}, self.task['id'])
            return register(assets, owner, arrays, **options)

        with patch.object(MotionAssets, 'register', cancel_before_registration), patch(
                'inference_worker.request_batch', side_effect=self.fake_worker):
            self.queue.run()

        row = self.queue.get('alice', job['id'])
        self.assertEqual(row['status'], 'cancelled')
        self.assertIsNone(self.assets.find_request('alice', job['id']))
        self.assertFalse(list(self.assets.objects.glob('*/.writing-*')))
        self.assertTrue((self.queue.folder / job['id'] / 'candidate.npz').is_file())

    def test_worker_failure_after_cancellation_keeps_cancelled_status(self):
        from inference_worker import WorkerDisconnected

        job = self.submit()
        self.queue.prepare()
        service = HarnessService(self.root)

        def cancel_then_disconnect(*args, **kwargs):
            service.cancel({'username': 'alice'}, self.task['id'])
            raise WorkerDisconnected('fixture disconnected during cancellation')

        with patch('inference_worker.request_batch', side_effect=cancel_then_disconnect):
            self.queue.run()

        self.assertEqual(self.queue.get('alice', job['id'])['status'], 'cancelled')
        self.assertIsNone(self.assets.find_request('alice', job['id']))
        self.assertFalse(list((self.root / 'topics/kimodo_edits/work').glob(f"{job['id']}*")))

    def test_restart_recovers_committed_candidate_when_cancellation_arrives_after_publication(self):
        job = self.submit()
        self.queue.prepare()
        update = self.queue.update

        def interrupt_before_completion(key, **values):
            if values.get('status') == 'completed':
                raise SystemExit('fixture stopped after asset commit')
            update(key, **values)

        with patch.object(self.queue, 'update', side_effect=interrupt_before_completion), patch(
                'inference_worker.request_batch', side_effect=self.fake_worker):
            with self.assertRaises(SystemExit):
                self.queue.run()

        published = self.assets.find_request('alice', job['id'])
        self.assertIsNotNone(published)
        self.assertEqual(self.queue.get('alice', job['id'])['status'], 'running')
        HarnessService(self.root).cancel({'username': 'alice'}, self.task['id'])

        restarted = MotionEdits(self.root)
        with patch('inference_worker.request_batch') as generate:
            restarted.run()

        generate.assert_not_called()
        row = restarted.get('alice', job['id'])
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['result']['asset'], published)
        self.assertTrue(row['result']['validation']['passed'])

    def test_failed_seams_publish_soft_warning_candidate_for_preview(self):
        self.fail_gate=True;self.submit();self.queue.prepare()
        with patch('inference_worker.request_batch',side_effect=self.fake_worker):self.queue.run()
        row=self.queue.get('alice','hedit_request_123')
        self.assertEqual(row['status'],'completed')
        self.assertIn('asset',row['result'])
        self.assertFalse(row['result']['validation']['passed'])
        self.assertIn('过渡不符合要求',row['result']['quality_warning'])
        self.assertIn('接缝平滑度待复核',row['message'])
        self.assertTrue((self.queue.folder/row['id']/'candidate.npz').exists())

    def test_outer_frame_corruption_is_rejected_even_if_report_claims_pass(self):
        self.corrupt_outside=True;self.submit();self.queue.prepare()
        with patch('inference_worker.request_batch',side_effect=self.fake_worker):self.queue.run()
        row=self.queue.get('alice','hedit_request_123');self.assertEqual(row['status'],'failed')
        self.assertNotIn('asset',row['result'])

    def test_english_mode_rejects_chinese_and_missing_model(self):
        with self.assertRaises(ValueError):self.submit(prompt='把选中的片段改成抬左手')
        with self.assertRaises(ValueError):self.submit(prompt='把选中的片段改成抬左手',prompt_mode='translate')

    def test_explicit_translation_is_bounded_and_reuses_saved_response(self):
        response={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'supported':True,'text':PROMPT})}}]}
        with patch('rewrite_models.resolve',return_value={'provider':'fixture'}),patch('rewrite_models.call',return_value=response) as call:
            self.submit(prompt='把选中的片段改成抬左手',prompt_mode='translate',rewrite_model='fixture')
            self.queue.prepare();self.queue.update('hedit_request_123',status='preparing');self.queue.prepare()
            call.assert_called_once()
        self.assertEqual(self.queue.get('alice','hedit_request_123')['prompt'],PROMPT)

    def test_cancel_during_rewrite_setup_prevents_provider_call(self):
        with patch('rewrite_models.resolve', return_value={'provider': 'fixture'}):
            self.submit(prompt='把选中的片段改成抬左手', prompt_mode='translate', rewrite_model='fixture')
        service = HarnessService(self.root)

        def cancel_then_resolve(*args):
            service.cancel({'username': 'alice'}, self.task['id'])
            return {'provider': 'fixture'}

        with patch('rewrite_models.resolve', side_effect=cancel_then_resolve), patch('rewrite_models.call') as call:
            self.queue.prepare()
        call.assert_not_called()
        row = self.queue.get('alice', 'hedit_request_123')
        self.assertEqual((row['status'], row['message']), ('cancelled', '修复已停止'))

    def test_cancellation_is_preserved_if_an_in_flight_rewrite_fails(self):
        service = HarnessService(self.root)

        def cancel_then_fail(*args):
            service.cancel({'username': 'alice'}, self.task['id'])
            raise TimeoutError('fixture provider timeout')

        with patch('rewrite_models.resolve', return_value={'provider': 'fixture'}):
            self.submit(prompt='把选中的片段改成抬左手', prompt_mode='translate', rewrite_model='fixture')
            with patch('rewrite_models.call', side_effect=cancel_then_fail) as call:
                self.queue.prepare()
        call.assert_called_once()
        self.assertEqual(self.queue.get('alice', 'hedit_request_123')['status'], 'cancelled')

    def test_cancellation_during_ready_transition_keeps_job_out_of_gpu_queue(self):
        job = self.submit()
        owner = 'alice'
        task_id = self.task['id']
        task_status = self.queue.ledger.task_status

        def cancel_after_status_read(owner, task_id):
            status = task_status(owner, task_id)
            if status != 'cancelled' and self.queue.get(owner, job['id'])['status'] == 'preparing':
                self.queue.ledger.cancel_task(owner, task_id)
            return status

        with patch.object(self.queue.ledger, 'task_status', side_effect=cancel_after_status_read):
            self.queue.prepare()

        self.assertEqual(task_status(owner, task_id), 'cancelled')
        row = self.queue.get(owner, job['id'])
        self.assertEqual((row['status'], row['message']), ('cancelled', '修复已停止'))
        self.assertIsNone(self.queue._choose(('ready',)))

    def test_uncertain_translation_is_not_reissued(self):
        with patch('rewrite_models.resolve',return_value={'provider':'fixture'}):
            self.submit(prompt='把选中的片段改成抬左手',prompt_mode='translate',rewrite_model='fixture')
        self.queue.update('hedit_request_123',status='preparing')
        with patch('rewrite_models.call') as call:self.queue.prepare()
        call.assert_not_called();self.assertEqual(self.queue.get('alice','hedit_request_123')['status'],'failed')

    def test_incomplete_gpu_operation_is_not_silently_resubmitted(self):
        self.submit();self.queue.prepare();self.queue.update('hedit_request_123',status='running')
        with patch('inference_worker.request_batch') as generate:self.queue.run()
        generate.assert_not_called();self.assertEqual(self.queue.get('alice','hedit_request_123')['status'],'failed')

    def test_uncertain_edit_transport_is_not_automatically_retried(self):
        from inference_worker import request_batch, WorkerDisconnected
        with patch('inference_worker._request_once',side_effect=WorkerDisconnected('lost')) as call:
            with self.assertRaises(WorkerDisconnected):
                request_batch(self.root,self.root/'unused.json',-1,retry_unfinished=False)
            call.assert_called_once()

    def test_revoked_account_stops_before_generation(self):
        self.submit();self.queue.prepare()
        with sqlite3.connect(self.root/'review/accounts.sqlite3') as db:db.execute("UPDATE users SET disabled=1 WHERE username='alice'")
        with patch('inference_worker.request_batch') as generate:self.queue.run()
        generate.assert_not_called();self.assertEqual(self.queue.get('alice','hedit_request_123')['status'],'cancelled')

    def test_account_revocation_updates_edit_queue_table(self):
        from review_access import cancel_owner_jobs
        self.submit();cancel_owner_jobs(self.root,'alice')
        self.assertEqual(self.queue.get('alice','hedit_request_123')['status'],'cancelled')

    def test_per_account_queue_bound_and_readiness(self):
        for i in range(4):self.submit('hedit_request_00'+str(i))
        with self.assertRaises(ValueError):self.submit('hedit_request_005')
        with patch('review_motion_edits.kimodo_edit_readiness',return_value=False):
            self.assertEqual(self.submit('hedit_request_000')['id'],'hedit_request_000')
            with self.assertRaises(ValueError):self.submit('hedit_request_other')

    def test_quota_exceeded_keeps_job_ready_without_charging_motion_seconds(self):
        from g2h import resource_quota

        self.submit()
        self.queue.prepare()
        cap = resource_quota.snapshot(self.root, 'alice', 'user')['resources']['motion_seconds']['limit']
        resource_quota.reserve(self.root, 'prefill-quota', 'alice', {'motion_seconds': cap - 0.5}, 'user')
        resource_quota.settle(self.root, 'prefill-quota', {'motion_seconds': cap - 0.5})
        before = resource_quota.snapshot(self.root, 'alice', 'user')['resources']['motion_seconds']['used']
        with patch('inference_worker.request_batch') as generate:
            self.queue.run()
        generate.assert_not_called()
        row = self.queue.get('alice', 'hedit_request_123')
        self.assertEqual(row['status'], 'ready')
        self.assertIn('配额不足', row['message'])
        after = resource_quota.snapshot(self.root, 'alice', 'user')['resources']['motion_seconds']['used']
        self.assertEqual(after - before, 0.0)

    def test_pre_inference_capacity_unavailable_keeps_ready_and_leaks_zero_quota(self):
        from g2h import resource_quota
        from g2h.capacity import CapacityUnavailable

        self.submit()
        self.queue.prepare()
        before = resource_quota.snapshot(self.root, 'alice', 'user')['resources']['motion_seconds']['used']
        with patch('g2h.capacity.reservation', side_effect=CapacityUnavailable('磁盘空间不足')), \
             patch('inference_worker.request_batch') as generate:
            self.queue.run()
        generate.assert_not_called()
        row = self.queue.get('alice', 'hedit_request_123')
        self.assertEqual(row['status'], 'ready')
        self.assertIn('磁盘空间不足', row['message'])
        after = resource_quota.snapshot(self.root, 'alice', 'user')['resources']['motion_seconds']['used']
        self.assertEqual(after - before, 0.0)
        self.assertFalse(list((self.root / 'topics/kimodo_edits/work').glob(f"{row['id']}*")))


if __name__=='__main__':unittest.main()
