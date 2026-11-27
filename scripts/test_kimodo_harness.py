"""Kimodo queue admission and model capability integration, without GPU calls."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from harness_service import HarnessService
from g2h.generation_backend import KIMODO_MAX_SECONDS, KIMODO_MODEL, kimodo_readiness
from g2h.motion_backends import MotionBackends

class KimodoHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = HarnessService(Path(self.tmp.name), legacy_store=object())
        self.user = {'username': 'alice'}
        self.env = patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL,
            'MOTIONFARM_HARNESS_ENABLE_GENERATION': '1'}, clear=True)
        self.env.start(); self.addCleanup(self.env.stop)

    def test_missing_install_receipt_does_not_admit_generation(self):
        self.assertFalse(kimodo_readiness(self.tmp.name)['available'])
        with patch('harness_service.kimodo_readiness', return_value={'available': False}):
            caps = self.service.capabilities(self.user)
        self.assertFalse(caps['generation_enabled'])
        models = {x['id']:x for x in caps['backends']}
        self.assertFalse(models['kimodo']['available'])
        self.assertFalse(models['hy_motion']['available'])

    def test_validated_worker_exposes_the_supported_duration_limit(self):
        with patch('harness_service.kimodo_readiness', return_value={'available': True}):
            caps = self.service.capabilities(self.user)
            self.assertTrue(caps['generation_enabled'])
            models = {x['id']:x for x in caps['backends']}
            self.assertTrue(models['kimodo']['available'])
            self.assertFalse(models['hy_motion']['available'])
            self.assertFalse(models['local_editor']['available'])
            tool = self.service._registry(self.user).get('generate_motion')
            self.assertEqual(tool.max_cost_usd, 0)
            schema = next(x for x in caps['tools'] if x['name']=='generate_motion')
            self.assertEqual(schema['parameters']['properties']['duration']['maximum'], KIMODO_MAX_SECONDS)

    def test_disabled_bridge_stays_disabled_after_assets_arrive(self):
        with patch('harness_service.kimodo_readiness', return_value={'available': True}), patch.dict(os.environ, {'MOTIONFARM_HARNESS_ENABLE_GENERATION':'0'}):
            self.assertFalse(self.service._generation_enabled())

    def test_model_callback_is_explicit_and_never_falls_back(self):
        called=[]
        backends=MotionBackends(kimodo_generator=lambda **kw:called.append(kw) or {'model':KIMODO_MODEL})
        self.assertEqual(backends.invoke('kimodo',text='A person walks.')['model'],KIMODO_MODEL)
        from g2h.motion_backends import BackendUnavailable
        with self.assertRaises(BackendUnavailable):backends.invoke('hy_motion',text='A person walks.')
        self.assertEqual(len(called),1)

if __name__=='__main__':unittest.main()
