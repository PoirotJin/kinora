"""Bounded rechecks preserve evidence, budget and restart semantics."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_qwen_motion_agent import agent, farm, TOPICS, STRAIGHT, BACKWARD, prompt, response, review, insert, AgentTests
from agent_context import prepare


class RecheckTests(unittest.TestCase):
    def setup_case(self, root, limit=2):
        db=farm.catalog(root);insert(db,STRAIGHT)
        historical=db.execute('SELECT id FROM prompts').fetchone()[0];db.close()
        args=AgentTests().args(root,max_attempts=limit);args.work_key='recheck-test'
        duplicate=review(False)
        duplicate['decisions'][0].update(verdict='duplicate',evidence_text_id=historical)
        return args,response(duplicate)

    def test_same_candidate_rechecked_without_new_generation(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);args,dup=self.setup_case(root)
            with patch.object(farm,'call_qwen',side_effect=[response({'prompts':[prompt(BACKWARD)]}),dup,response(review(True))]) as api:
                self.assertTrue(agent.run_agent(args,TOPICS))
                self.assertEqual(api.call_count,3)
                payload=json.loads(api.call_args_list[2].args[2][1]['content'])
            self.assertEqual(payload['candidates'][0]['text'],BACKWARD)
            self.assertEqual(payload['review_conflicts'][0]['reference_text'],STRAIGHT)
            self.assertIn('direction',payload['review_conflicts'][0]['difference_axes'])
            folder=root/'agent_runs/recheck-test'
            self.assertFalse((folder/'attempt_02/generate_request.json').exists())
            outcome=json.loads((folder/'attempt_01/outcome.json').read_text())
            self.assertTrue(outcome['review_evidence_conflict'])
            self.assertNotIn('review_format_rejected',outcome)
            with patch.object(farm,'call_qwen') as api:
                self.assertTrue(agent.run_agent(args,TOPICS));api.assert_not_called()

    def test_repeat_conflict_and_last_slot_never_auto_accept(self):
        for limit in [1,2]:
            with self.subTest(limit=limit),tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
                root=Path(tmp);args,dup=self.setup_case(root,limit)
                replies=[response({'prompts':[prompt(BACKWARD)]}),dup]+([dup] if limit==2 else [])
                with patch.object(farm,'call_qwen',side_effect=replies) as api:
                    self.assertFalse(agent.run_agent(args,TOPICS));self.assertEqual(api.call_count,len(replies))
                db=farm.catalog(root);self.assertEqual(db.execute('SELECT count(*) FROM prompts').fetchone()[0],1);db.close()
                with patch.object(farm,'call_qwen') as api:
                    self.assertFalse(agent.run_agent(args,TOPICS));api.assert_not_called()

    def test_restart_reuses_generation_and_initial_review(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);args,dup=self.setup_case(root)
            with patch.object(farm,'call_qwen',side_effect=[response({'prompts':[prompt(BACKWARD)]}),dup,farm.TemporaryAPIError('timeout')]):
                with self.assertRaises(farm.TemporaryAPIError):agent.run_agent(args,TOPICS)
            with patch.object(farm,'call_qwen',return_value=response(review(True))) as api:
                self.assertTrue(agent.run_agent(args,TOPICS));self.assertEqual(api.call_count,1)
                payload=json.loads(api.call_args.args[2][1]['content'])
                self.assertEqual(payload['review_conflicts'][0]['text'],BACKWARD)

    def test_recheck_does_not_make_uncertain_acceptable(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);args,dup=self.setup_case(root)
            unsure=review(False);unsure['decisions'][0]['verdict']='uncertain'
            with patch.object(farm,'call_qwen',side_effect=[response({'prompts':[prompt(BACKWARD)]}),dup,response(unsure)]):
                self.assertFalse(agent.run_agent(args,TOPICS))

    def test_recheck_reuses_vectors_and_manifest_recovers_without_api(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);args,dup=self.setup_case(root)
            args.semantic_retrieval=True;args.embedding_model='fake';args.dimensions=64;args.neighbors=5
            reference=json.loads(dup['choices'][0]['message']['content'])['decisions'][0]['evidence_text_id']
            with patch.object(agent.semantic_index,'require_complete'),patch.object(agent.semantic_index,'faiss_module'), \
                 patch.object(agent.semantic_index,'embed',return_value=[np.asarray([1]+[0]*63,dtype=float)]) as embed, \
                 patch.object(agent.semantic_index,'nearest',return_value=[[dict(text_id=reference,text=STRAIGHT,similarity=.9)]]) as nearest, \
                 patch.object(farm,'call_qwen',side_effect=[response({'prompts':[prompt(BACKWARD)]}),dup,response(review(True))]) as api:
                self.assertTrue(agent.run_agent(args,TOPICS))
                self.assertTrue(agent.run_agent(args,TOPICS))
                self.assertEqual(api.call_count,3);embed.assert_called_once();nearest.assert_called_once()
            folder=root/'agent_runs/recheck-test'
            self.assertEqual((folder/'attempt_01/vectors.json').read_text(),(folder/'attempt_02/vectors.json').read_text())

    def test_protected_evidence_budget_and_feedback(self):
        payload=dict(topics=TOPICS,duration_s=8,candidates=[prompt(BACKWARD)],history_json={},
            candidate_neighbors=[dict(candidate_index=0,matches=[dict(text_id='ref',text=STRAIGHT)])],
            review_conflicts=[dict(text=BACKWARD,reference_text=STRAIGHT,evidence_text_id='ref',difference_axes=['direction'])])
        result,meta,evidence=prepare('review','rules',payload)
        self.assertEqual(evidence['ref'],STRAIGHT)
        with self.assertRaises(ValueError):prepare('review','S'*12000,payload)
        longtext='A person walks forward. '*30
        payload['rejected_feedback']=[dict(text=longtext,reference_text=longtext)]
        result,_,_=prepare('generate','rules',payload)
        self.assertEqual(result['rejected_feedback'][0]['text'],longtext)
        self.assertEqual(result['rejected_feedback'][0]['reference_text'],longtext)


if __name__=='__main__':unittest.main()
