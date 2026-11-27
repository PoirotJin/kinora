"""Semantic-agent control-flow tests; no network or GPU work."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import qwen_motion_agent as agent
import MotionFarm as farm

TOPICS = [{"id": "walking", "name": "走路", "definition": "日常步行及转身"}]
STRAIGHT = "A person walks forward at a steady pace with short steps and relaxed arm swings."
BACKWARD = "A person walks backward slowly with short careful steps and their arms held loosely at their sides."


def prompt(text):
    return {"topic_id": "walking", "text": text,
            "attributes": {"direction": "backward" if text == BACKWARD else "forward",
                           "speed": "slow", "style": "ordinary"}}


def response(content):
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
            "model": "qwen3.8-flash", "id": "test", "usage": {"total_tokens": 1}}


def review(accept):
    return {"decisions": [{"index": 0, "accept": accept, "family": "backward walking" if accept else "straight walking",
                           "verdict":"distinct" if accept else "invalid","evidence_text_id":None,
                           "reason": "Adds a different travel direction." if accept else "Repeats the dominant straight walking pattern."}],
            "next_hint": "Explore backward walking instead of another straight walking paraphrase."}


def insert(db, text, family=None):
    item = {**prompt(text), "id": farm.task_id({"text": text}), "source": {}}
    if family:
        item["source"]["semantic_review"] = {"family": family}
    with db:
        db.execute("INSERT INTO prompts VALUES (?,?,?,?)",
                   (item["id"], "walking", farm.normalized(text), json.dumps(item)))


class AgentTests(unittest.TestCase):
    def args(self, root, **kwargs):
        return SimpleNamespace(root=root, rounds=kwargs.get("rounds", 1), per_topic=1,
            max_attempts=kwargs.get("max_attempts", 2), history_limit=12, duration=8,
            model="qwen3.8-flash", key_file=root / "unused", enqueue=False, seeds=[42])

    def test_history_preserves_full_small_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = farm.catalog(Path(tmp))
            for i in range(8):
                insert(db, STRAIGHT + " Variant %d." % i, "straight walking")
            history = agent.history_snapshot(db, TOPICS, 12)["walking"]
            db.close()
            self.assertEqual(len(history["examples"]), 8)
            self.assertFalse(history["history_is_sampled"])

    def test_large_history_keeps_older_variation_and_full_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = farm.catalog(Path(tmp))
            insert(db, BACKWARD, "backward walking")
            for i in range(19):
                insert(db, STRAIGHT + " Variant %d." % i, "straight walking")
            history = agent.history_snapshot(db, TOPICS, 12)["walking"]
            db.close()
            self.assertEqual(history["family_counts"]["straight walking"], 19)
            self.assertEqual(history["total_saved_prompts"], 20)
            self.assertTrue(history["history_is_sampled"])
            self.assertIn(BACKWARD, [e["text"] for e in history["examples"]])
            self.assertLessEqual(len(history["examples"]), 12)

    def test_rejection_feedback_rewrites_without_saving_rejected_prompt(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            replies = [response({"prompts": [prompt(STRAIGHT)]}), response(review(False)),
                       response({"prompts": [prompt(BACKWARD)]}), response(review(True))]
            with patch.object(farm, "call_qwen", side_effect=replies) as call:
                self.assertTrue(agent.run_agent(self.args(root), TOPICS))
            payload = json.loads(call.call_args_list[2].args[2][1]["content"])
            self.assertIn("dominant straight walking", str(payload["rejected_feedback"]))
            db = farm.catalog(root)
            saved = [json.loads(r[0])["text"] for r in db.execute("SELECT item FROM prompts")]
            db.close()
            self.assertEqual(saved, [BACKWARD])
            self.assertFalse((root / "topics").exists())

    def test_retry_cap_stops_further_rounds(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            replies = [response({"prompts": [prompt(STRAIGHT)]}), response(review(False))]
            with patch.object(farm, "call_qwen", side_effect=replies) as call:
                self.assertFalse(agent.run_agent(self.args(Path(tmp), rounds=5, max_attempts=1), TOPICS))
                self.assertEqual(call.call_count, 2)

    def test_exact_duplicate_cannot_be_overridden_by_reviewer(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            db = farm.catalog(root)
            insert(db, STRAIGHT)
            db.close()
            with patch.object(farm, "call_qwen", side_effect=[response({"prompts": [prompt(STRAIGHT)]}), response(review(True))]):
                self.assertFalse(agent.run_agent(self.args(root, max_attempts=1), TOPICS))
            db = farm.catalog(root)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0], 1)
            db.close()

    def test_invalid_review_fails_closed(self):
        bad = review(True)
        bad["decisions"][0]["accept"] = "true"
        with self.assertRaises(ValueError):
            agent.reviewed(json.dumps(bad), 1)
        bad = review(True)
        bad["decisions"] *= 2
        with self.assertRaises(ValueError):
            agent.reviewed(json.dumps(bad), 2)

    def test_retrieval_is_given_to_review_and_vectors_saved(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            args = self.args(root)
            args.semantic_retrieval, args.embedding_model, args.dimensions, args.neighbors = True, 'fake', 64, 5
            matches = [[{'text_id':'historical','text':STRAIGHT,'similarity':0.7}]]
            with patch.object(farm,'call_qwen',side_effect=[response({'prompts':[prompt(BACKWARD)]}),response(review(True))]) as call, \
                 patch.object(agent.semantic_index,'embed',return_value=[[1]+[0]*63]), \
                 patch.object(agent.semantic_index,'nearest',return_value=matches), \
                 patch.object(agent.semantic_index,'faiss_module'):
                self.assertTrue(agent.run_agent(args,TOPICS))
            payload = json.loads(call.call_args_list[1].args[2][1]['content'])
            observed=payload['candidate_neighbors'][0]['matches'][0]
            self.assertEqual(observed['text_id'],matches[0][0]['text_id'])
            self.assertIn('direction',observed['explicit_difference_axes'])
            db = farm.catalog(root)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM embeddings').fetchone()[0],1)
            db.close()

    def test_missing_vectors_stop_before_spending_on_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = farm.catalog(root)
            insert(db,STRAIGHT)
            db.close()
            args = self.args(root)
            args.semantic_retrieval, args.embedding_model, args.dimensions = True, 'fake',64
            with patch.object(farm,'call_qwen') as call:
                with self.assertRaises(RuntimeError):
                    agent.run_agent(args,TOPICS)
                call.assert_not_called()

    def test_multiple_topics_are_separate_single_prompt_requests(self):
        other={'id':'running','name':'跑步','definition':'bipedal running'}
        run_prompt={**prompt(BACKWARD),'topic_id':'running','text':'A person runs backward slowly with short careful steps and steady controlled arm swings.'}
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            replies=[response({'prompts':[prompt(BACKWARD)]}),response(review(True)),response({'prompts':[run_prompt]}),response(review(True))]
            with patch.object(farm,'call_qwen',side_effect=replies) as call:
                self.assertTrue(agent.run_agent(self.args(Path(tmp)),TOPICS+[other]))
            payloads=[json.loads(call.call_args_list[i].args[2][1]['content']) for i in (0,2)]
            self.assertEqual([p['topics'][0]['id'] for p in payloads],['walking','running'])
            self.assertTrue(all(len(p['topics'])==1 and p['count_per_topic']==1 for p in payloads))

    def test_rejected_topic_does_not_prevent_next_topic(self):
        other={'id':'running','name':'跑步','definition':'bipedal running'}
        with tempfile.TemporaryDirectory() as tmp, patch.object(agent,'_run_single',side_effect=[False,True]) as run:
            self.assertFalse(agent.run_agent(self.args(Path(tmp)),TOPICS+[other]))
            self.assertEqual(run.call_count,2)
            self.assertEqual(run.call_args_list[1].args[1],[other])

    def test_long_explanation_preserves_decision_and_bounds_metadata(self):
        value=review(False);value['decisions'][0]['reason']='Long English rationale. '*100
        value['next_hint']='Try a different motion. '*200
        decisions,hint=agent.reviewed(json.dumps(value),1)
        self.assertFalse(decisions[0]['accept']);self.assertLessEqual(len(decisions[0]['reason']),600)
        self.assertLessEqual(len(hint),2000)
        self.assertLessEqual(len(decisions[0]['reason'].split()),20);self.assertLessEqual(len(hint.split()),20)

    def test_invalid_review_retries_without_accepting_and_then_succeeds(self):
        bad=review(True);bad['decisions'][0]['accept']='true'
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp)
            replies=[response({'prompts':[prompt(STRAIGHT)]}),response(bad),response({'prompts':[prompt(BACKWARD)]}),response(review(True))]
            with patch.object(farm,'call_qwen',side_effect=replies):self.assertTrue(agent.run_agent(self.args(root),TOPICS))
            db=farm.catalog(root)
            self.assertEqual([json.loads(row[0])['text'] for row in db.execute('SELECT item FROM prompts')],[BACKWARD])
            db.close()

    def test_invalid_reviews_exhaust_into_recoverable_skip(self):
        bad=review(True);bad['decisions'][0]['accept']='true'
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp)
            with patch.object(farm,'call_qwen',side_effect=[response({'prompts':[prompt(STRAIGHT)]}),response(bad)]):
                self.assertFalse(agent.run_agent(self.args(root,max_attempts=1),TOPICS))
            db=farm.catalog(root);self.assertEqual(db.execute('SELECT count(*) FROM prompts').fetchone()[0],0);db.close()

    def test_duplicate_requires_evidence_actually_sent(self):
        value=review(False);value['decisions'][0].update(verdict='duplicate',evidence_text_id='real')
        with self.assertRaises(ValueError):agent.reviewed(json.dumps(value),1,{'other':'A different motion.'})
        decisions,_=agent.reviewed(json.dumps(value),1,{'real':STRAIGHT})
        self.assertFalse(decisions[0]['accept'])
        value['decisions'][0]['accept']=True
        with self.assertRaises(ValueError):agent.reviewed(json.dumps(value),1,{'real':STRAIGHT})

    def test_output_token_cap_and_fresh_messages(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            with patch.object(farm,'call_qwen',side_effect=[response({'prompts':[prompt(BACKWARD)]}),response(review(True))]) as call:
                self.assertTrue(agent.run_agent(self.args(Path(tmp)),TOPICS))
            for request in call.call_args_list:
                self.assertEqual(len(request.args[2]),2)
                self.assertEqual(request.kwargs['max_tokens'],384)


if __name__ == "__main__":
    unittest.main()
