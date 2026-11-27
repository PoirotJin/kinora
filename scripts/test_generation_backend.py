"""Model pinning and admission tests; no model requests or GPU work."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from g2h.generation_backend import (HY_MODEL, KIMODO_MODEL, current_recipe,
                                    manifest_recipe, validate_spec)


class BackendTests(unittest.TestCase):
    def test_old_outbox_keeps_hy_after_default_switch(self):
        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL}):
            self.assertEqual(current_recipe()['model'], KIMODO_MODEL)
            self.assertEqual(manifest_recipe({'prompts': []})['model'], HY_MODEL)

    def test_recipe_is_pinned_and_survives_later_switch(self):
        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL}):
            manifest = json.loads(json.dumps({'generation': current_recipe()}))
        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': HY_MODEL}):
            self.assertEqual(manifest_recipe(manifest)['model'], KIMODO_MODEL)

    def test_unknown_model_never_silently_falls_back(self):
        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': 'typo'}):
            with self.assertRaises(ValueError):
                current_recipe()

    def test_model_changes_task_identity(self):
        from g2h.collection_store import task_id
        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL}):
            kimodo = dict(current_recipe(), text='A person walks.', seed=7, duration_s=8)
        hy = dict(kimodo, model=HY_MODEL)
        self.assertNotEqual(task_id(hy), task_id(kimodo))

    def test_duration_cap_is_specific_to_backend(self):
        hy = dict(model=HY_MODEL, text='A person walks.', seed=7, duration_s=12, cfg_scale=5)
        self.assertEqual(validate_spec(hy), HY_MODEL)
        with patch.dict(os.environ, {'MOTIONFARM_GENERATOR_MODEL': KIMODO_MODEL}):
            kimodo = dict(current_recipe(), text='A person walks.', seed=7, duration_s=121)
        with self.assertRaises(ValueError):
            validate_spec(kimodo)
        kimodo['duration_s'] = 120
        self.assertEqual(validate_spec(kimodo), KIMODO_MODEL)
        kimodo['duration_s'] = float('nan')
        with self.assertRaises(ValueError):
            validate_spec(kimodo)

    def test_missing_recipe_cannot_be_executed_as_kimodo(self):
        spec = dict(model=KIMODO_MODEL, text='A person walks.', seed=7, duration_s=8, cfg_scale=2)
        with self.assertRaises(ValueError):
            validate_spec(spec)

    def test_backend_socket_names_are_separate(self):
        from inference_worker import private_folder
        with tempfile.TemporaryDirectory(dir='/tmp') as tmp:
            self.assertNotEqual(private_folder(tmp, HY_MODEL), private_folder(tmp, KIMODO_MODEL))


if __name__ == '__main__':
    unittest.main()
