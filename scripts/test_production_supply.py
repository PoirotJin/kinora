from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from production_supply import asynchronous, finish, policy, reserve, setup


class SupplyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'review/production').mkdir(parents=True)
        self.db=sqlite3.connect(self.root/'state.sqlite3');self.db.row_factory=sqlite3.Row
        setup(self.db);self.settings=dict(k=8,duration=8,continuous=True)

    def tearDown(self):self.db.close();self.temp.cleanup()

    def test_opt_in_only_continuous(self):
        self.assertFalse(asynchronous(self.root,self.settings))
        (self.root/'review/production/supply_policy.json').write_text('{"enabled":true}')
        self.assertTrue(asynchronous(self.root,self.settings))
        self.assertFalse(asynchronous(self.root,dict(continuous=False)))

    def test_crash_replays_identical_key_and_seeds_without_charge(self):
        a=reserve(self.db,'job','walking',self.settings,1)
        self.db.close();self.db=sqlite3.connect(self.root/'state.sqlite3');self.db.row_factory=sqlite3.Row
        b=reserve(self.db,'job','walking',self.settings,1)
        self.assertEqual(a,b)
        self.assertEqual(self.db.execute('SELECT steps FROM daily').fetchone()[0],1)
        self.assertEqual(len(set(json.loads(a['seeds']))),8)

    def test_daily_limit_survives_restart_and_success(self):
        a=reserve(self.db,'job','walking',self.settings,1);finish(self.db,a,0)
        finish(self.db,a,0)  # A duplicate completion must not inflate supply.
        self.assertIsNone(reserve(self.db,'job','running',self.settings,1))
        self.assertEqual(self.db.execute('SELECT accepted FROM daily').fetchone()[0],1)

    def test_rejection_cools_topic_but_new_step_can_resume(self):
        a=reserve(self.db,'job','walking',self.settings,3);finish(self.db,a,3,now=100)
        row=self.db.execute('SELECT * FROM topics').fetchone()
        self.assertEqual(row['wait_until'],130)
        b=reserve(self.db,'job','running',self.settings,3);self.assertNotEqual(a['work_key'],b['work_key'])
        finish(self.db,b,0)
        self.assertEqual(self.db.execute('SELECT failures FROM topics WHERE topic="running"').fetchone()[0],0)

    def test_new_job_does_not_reset_spending_budget(self):
        reserve(self.db,'job','walking',self.settings,1)
        self.assertIsNone(reserve(self.db,'next-job','running',self.settings,1))

    def test_invalid_policy_fails_closed(self):
        (self.root/'review/production/supply_policy.json').write_text('{"max_attempts":99}')
        with self.assertRaises(ValueError):policy(self.root)

    def test_composition_bounded_two_actions_and_topic_specific(self):
        import random
        from prompt_exploration import composition,COMPOSITIONS
        for topic in COMPOSITIONS:
            p=composition(topic,random.Random(17))
            self.assertIn((p['first'],p['then']),COMPOSITIONS[topic])
            self.assertLess(len(json.dumps(p)),900)

    def test_exhausted_history_waits_without_calling_api_or_completing(self):
        from review_production import Production

        (self.root/'review/production/supply_policy.json').write_text('{"enabled":true}')
        config = self.root / 'topics.json'
        config.write_text(json.dumps({'topics': [{'id': 'walking', 'name': '走路'}]}))
        production = Production(self.root, config)
        job = production.launch(dict(self.settings, topics=['walking'], per_topic=1), 'tester')['job_id']
        with patch.object(production, 'command') as command, \
                patch('production_flow.time.sleep', side_effect=lambda _: production.cancel()):
            production.execute(job)
        command.assert_not_called()
        with closing(production.db()) as db:
            self.assertEqual(db.execute('SELECT status FROM steps WHERE job=?', (job,)).fetchone()[0],
                             'waiting_background_supply')
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (job,)).fetchone()[0], 'stopped')

    def test_stale_capacity_waits_for_real_reconciliation(self):
        from production_flow import ready_capacity
        from unittest.mock import Mock
        updates=[];p=SimpleNamespace(root=self.root,cancelled=lambda _:False,
                                    update=lambda job,**kw:updates.append(kw))
        reader=Mock(side_effect=[dict(ready=True,stale=True),dict(ready=True,stale=False,total=123)])
        with patch('production_flow.time.sleep'):
            value=ready_capacity(p,'job',reader)
        self.assertEqual(value['total'],123)
        self.assertEqual(reader.call_count,2)
        self.assertEqual(updates[0]['stage'],'storage')

    def test_capacity_wait_remains_cancellable_and_does_not_hide_errors(self):
        from production_flow import ready_capacity
        from unittest.mock import Mock
        stop=Mock(side_effect=[False,True])
        p=SimpleNamespace(root=self.root,cancelled=stop,update=lambda *a,**k:None)
        self.assertIsNone(ready_capacity(p,'job',lambda _:dict(stale=True)))
        p.cancelled=lambda _:False
        with self.assertRaises(RuntimeError):
            ready_capacity(p,'job',Mock(side_effect=RuntimeError('disk unavailable')))


if __name__=='__main__':unittest.main()
