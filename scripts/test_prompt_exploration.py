"""Regression tests for conflict adjudication, diversity and bounded carryover."""
import contextlib,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from prompt_exploration import exploration_seed,plan,recent_feedback
from agent_context import prepare
from test_qwen_motion_agent import agent,farm,TOPICS,STRAIGHT,prompt,response,review,insert,AgentTests

class RepairTests(unittest.TestCase):
    def test_mirrored_sequences_are_not_hidden_by_same_word_sets(self):
        from prompt_exploration import direction_conflicts
        a='A person performs a crossover step to the left into a squat, then rises and steps backward to the right.'
        b='A person performs a crossover step to the right into a squat, then rises and steps backward to the left.'
        self.assertTrue(direction_conflicts(a,b))
        self.assertFalse(direction_conflicts('A person walks forward swinging arms backward.',STRAIGHT))

    def test_seed_order_and_step_are_significant(self):
        self.assertNotEqual(exploration_seed('ab','walking',1),exploration_seed('ba','walking',1))
        self.assertNotEqual(exploration_seed('run_99','walking',1),exploration_seed('run_100','walking',1))
        plans=[json.dumps(plan('walking',{'counts':{}},exploration_seed(f'job_{i}','walking',1)),sort_keys=True) for i in range(32)]
        self.assertGreater(len(set(plans)),15)

    def test_generator_receives_measured_coverage_and_plan(self):
        history={'walking':{'measured_motion_coverage':{'underrepresented':'slow_travel'},'exploration_plan':{'optional_cues':{'direction':'backward'}}}}
        p,meta,_=prepare('generate','rules',dict(topics=TOPICS,duration_s=8,history_json=history))
        self.assertEqual(set(p['output_template']['prompts'][0]['attributes']),{'direction','speed','style'})
        self.assertEqual(p['measured_motion_coverage']['underrepresented'],'slow_travel')
        self.assertEqual(p['exploration_plan']['optional_cues']['direction'],'backward')
        self.assertLessEqual(meta['input_bytes'],meta['byte_budget'])

    def test_explained_duplicate_stays_rejected_but_conflict_resolves(self):
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(tmp);db=farm.catalog(root);insert(db,STRAIGHT)
            ref=db.execute('select id from prompts').fetchone()[0];db.close()
            candidate='A person walks forward at a steady pace with short steps, swinging the arms forward and backward outside the torso.'
            dup=review(False);dup['decisions'][0].update(verdict='duplicate',evidence_text_id=ref)
            resolved=json.loads(json.dumps(dup));resolved['decisions'][0]['difference_assessment']=dict(evidence_text_id=ref,axes=['direction'],same_motion=True,explanation='Backward refers to the arm swing, while both descriptions have the same forward travel and short steady steps.')
            args=AgentTests().args(root,max_attempts=2);args.work_key='resolved-test'
            with patch.object(farm,'call_qwen',side_effect=[response({'prompts':[prompt(candidate)]}),response(dup),response(resolved)]):
                self.assertFalse(agent.run_agent(args,TOPICS))
            outcome=json.loads((root/'agent_runs/resolved-test/attempt_02/outcome.json').read_text())
            self.assertEqual(outcome['accepted'],0);self.assertNotIn('review_evidence_conflict',outcome)
            db=farm.catalog(root);self.assertEqual(db.execute('select count(*) from prompts').fetchone()[0],1);db.close()

    def test_generic_explanation_cannot_resolve_real_difference_terms(self):
        from prompt_exploration import resolved_difference
        d=dict(verdict='duplicate',difference_assessment=dict(evidence_text_id='id',axes=['direction'],same_motion=True,explanation='Both texts describe exactly the same travel direction.'))
        self.assertFalse(resolved_difference(d,'id',['direction'],'A person walks forward swinging arms backward.','A person walks forward.'))

    def test_missing_or_wrong_axis_cannot_resolve_conflict(self):
        from prompt_exploration import resolved_difference
        d=dict(verdict='duplicate',difference_assessment=dict(evidence_text_id='id',axes=['speed'],same_motion=True,explanation='Both texts describe exactly the same travel direction.'))
        self.assertFalse(resolved_difference(d,'id',['direction']))
        self.assertFalse(resolved_difference(d,'other',['speed']))
        self.assertFalse(resolved_difference(dict(verdict='distinct'),'id',['direction']))

    def test_recent_rejection_is_carried_across_steps(self):
        from g2h.agent_learning import decision
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);decision(root,'old_run:1:0','walking','duplicate')
            p=root/'agent_runs/old_run/attempt_01';p.mkdir(parents=True)
            (p/'outcome.json').write_text(json.dumps({'conflicts':[{'text':STRAIGHT,'reference_text':STRAIGHT,'reason':'Same travel'}]}))
            self.assertEqual(recent_feedback(root,'walking')[0]['text'],STRAIGHT)
            self.assertEqual(recent_feedback(root,'running'),[])

if __name__=='__main__':unittest.main()
