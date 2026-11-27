"""Same-batch denominators and conservative unknown cost handling."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from g2h.api_costs import topic_efficiency

class TopicCostsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.topic='ground_get_up';self.key='production_abc_484_'+self.topic
        (self.root/'review').mkdir();(self.root/'topics'/self.topic).mkdir(parents=True)
        with sqlite3.connect(self.root/'prompts.sqlite3') as db:
            db.executescript('CREATE TABLE topic_counts(topic TEXT PRIMARY KEY,total INTEGER);CREATE TABLE prompts(id TEXT PRIMARY KEY,topic TEXT,item TEXT);CREATE TABLE motion_links(number INTEGER,task_id TEXT,text_id TEXT);CREATE INDEX text_links ON motion_links(text_id);')
            db.execute('INSERT INTO topic_counts VALUES(?,10000)',(self.topic,))
            db.execute('INSERT INTO prompts VALUES(?,?,?)',('p',self.topic,json.dumps({'source':{'run_id':self.key}})))
            db.execute("INSERT INTO motion_links VALUES(1,'task','p')")
        with sqlite3.connect(self.root/'topics'/self.topic/'index.sqlite3') as db:db.executescript("CREATE TABLE records(id TEXT PRIMARY KEY);INSERT INTO records VALUES('task');")
        with sqlite3.connect(self.root/'review/reviews.sqlite3') as db:db.executescript("CREATE TABLE latest(motion_id TEXT PRIMARY KEY,status TEXT);INSERT INTO latest VALUES('m00000001','accepted');")
        folder=self.root/'agent_runs'/self.key/'attempt_01';folder.mkdir(parents=True)
        (folder/'manifest.json').write_text(json.dumps({'prompts':[{'id':'p','topic_id':self.topic,'source':{'run_id':self.key}}]}))
    def row(self,amount=2,currency='CNY',status='ok',job=None):
        return (1,'m','chat',1,1,True,2,'','provider','prompt_generation',job or 'auto:'+self.key,0,currency,amount,'v','configured_estimate','id','{}',status)
    def test_same_cohort_not_lifetime_and_underscored_topic(self):
        result=topic_efficiency([self.row(),self.row(3,'USD','error')],self.root)
        self.assertTrue(result['available']);row=result['topics'][0]
        self.assertEqual((row['texts'],row['motions'],row['accepted']),(1,1,1))
        self.assertEqual(row['per_unit']['texts'],{'CNY':2,'USD':3})
        self.assertEqual(row['failed_currencies'],{'USD':3})
    def test_unknown_or_truncated_never_zero(self):
        for rows,truncated in [([self.row(None)],False),([self.row()],True)]:
            row=topic_efficiency(rows,self.root,truncated)['topics'][0]
            self.assertIsNone(row['per_unit']['accepted'])
    def test_missing_manifest_and_unassigned_call(self):
        (self.root/'agent_runs'/self.key/'attempt_01/manifest.json').unlink()
        result=topic_efficiency([self.row(),self.row(job='custom:abc')],self.root)
        self.assertEqual(result['unattributed_calls'],1)
        self.assertTrue(result['topics'][0]['output_unknown'])
        self.assertIsNone(result['topics'][0]['per_unit']['texts'])
    def test_missing_store_does_not_break_usage(self):
        (self.root/'review/reviews.sqlite3').unlink()
        self.assertFalse(topic_efficiency([self.row()],self.root)['available'])

if __name__=='__main__':unittest.main()
