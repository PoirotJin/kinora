"""Generated splice source identity does not imply human acceptance."""
import unittest
from types import SimpleNamespace
from harness_service import HarnessService


class ResultStatusTests(unittest.TestCase):
    def test_only_explicit_acceptance_marks_a_result_accepted(self):
        task={'id':'task_fixture','goal':'Splice','status':'ready','current_motion_id':'ma_fixture'}
        candidate={'motion_id':'ma_fixture','status':'pending_review','created':1}
        detail=dict(task,candidates=[candidate])
        service=object.__new__(HarnessService)
        service.store=SimpleNamespace(list_tasks=lambda *a,**kw:[task],get_task=lambda *a:detail)
        self.assertFalse(service.results({'username':'alice'})['items'][0]['accepted'])
        detail['status']='accepted'
        self.assertTrue(service.results({'username':'alice'})['items'][0]['accepted'])


if __name__=='__main__': unittest.main()
