"""Explicit browser edit decisions and read-only reconciliation, no agent API."""
import json
import os
import unittest
from unittest.mock import patch
from harness_service import HarnessService
from review_access import allowed
from test_motion_edits import QueueFixture, PROMPT


class HarnessRepairTests(QueueFixture, unittest.TestCase):
    def test_repair_decision_replay_and_poll_do_not_resubmit(self):
        service=HarnessService(self.root);user={'username':'alice','role':'user'}
        body={'request_id':'repair_decision_123','prompt':PROMPT,'prompt_mode':'exact_english'}
        with patch.dict(os.environ,{'MOTIONFARM_HARNESS_ENABLE_EDITING':'1'}),patch('harness_service.kimodo_edit_readiness',return_value=True):
            first=service.repair(user,self.task['id'],body)
            self.assertTrue(first['decision_receipt']['action_applied'])
            self.assertEqual(first['operations'][0]['tool'],'edit_motion')
            service.repair(user,self.task['id'],body)
            for _ in range(3):service.repair_status(user,self.task['id'])
            with self.queue.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
            self.queue.prepare()
            with patch('inference_worker.request_batch',side_effect=self.fake_worker):self.queue.run()
            done=service.repair_status(user,self.task['id'])
            self.assertEqual(len(done['candidates']),1)
            self.assertEqual(done['current_motion_id'],self.parent['motion_id'])
            repeated=service.repair_status(user,self.task['id'])
            self.assertEqual(len(repeated['operations']),1)
            accepted=service.accept(user,self.task['id'],{'candidate_id':done['candidates'][0]['id']})
            self.assertNotEqual(accepted['current_motion_id'],self.parent['motion_id'])
            self.assertEqual(accepted['source_motion_id'],self.parent['motion_id'])

    def test_cancel_propagates_to_local_edit_queue(self):
        self.submit();service=HarnessService(self.root)
        service.cancel({'username':'alice'},self.task['id'])
        self.assertEqual(self.queue.get('alice','hedit_request_123')['status'],'cancelled')

    def test_new_routes_preserve_role_boundary(self):
        for action in ('repair','repair-status'):
            path='/api/harness/tasks/task_123/'+action
            self.assertTrue(allowed({'role':'user'},path,True))
            self.assertFalse(allowed({'role':'user'},path,False))
            self.assertFalse(allowed({'role':'viewer'},path,True))

    def test_repair_routes_dispatch_and_enforce_ownership(self):
        from urllib.parse import urlsplit
        from review_harness import handle
        from review_access import Forbidden
        from test_review_harness import Handler
        service=HarnessService(self.root);user={'username':'alice','role':'user'}
        base='/api/harness/tasks/'+self.task['id']
        body={'request_id':'route_repair_123','prompt':PROMPT,'prompt_mode':'exact_english'}
        with patch.dict(os.environ,{'MOTIONFARM_HARNESS_ENABLE_EDITING':'1'}),patch('harness_service.kimodo_edit_readiness',return_value=True):
            status,result=handle(Handler(service,body),urlsplit(base+'/repair'),True,user)
            self.assertEqual(status,200);self.assertTrue(result['decision_receipt']['action_applied'])
            self.assertEqual(handle(Handler(service,{}),urlsplit(base+'/repair-status'),True,user)[0],200)
            with self.assertRaises(ValueError):
                handle(Handler(service,{'owner':'bob'}),urlsplit(base+'/repair-status'),True,user)
            with self.assertRaises((Forbidden,KeyError)):
                handle(Handler(service,{}),urlsplit(base+'/repair-status'),True,{'username':'bob'})

    def test_zero_budget_does_not_enable_unconfigured_editor(self):
        service=HarnessService(self.root)
        with patch.dict(os.environ,{'MOTIONFARM_HARNESS_ENABLE_EDITING':'1'}),patch('harness_service.kimodo_edit_readiness',return_value=False):
            self.assertFalse(service.capabilities({'username':'alice'})['editing_enabled'])


if __name__=='__main__':unittest.main()
