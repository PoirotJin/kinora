import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import review_calibration as cal


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'review').mkdir()
        self.sha='source-v1'
        test=self
        class Store:
            root=test.root
            def detail(self,mid):return dict(topic='walking',location={'sha256':test.sha})
            def reviews(self):return sqlite3.connect(test.root/'review/reviews.sqlite3')
        self.store=Store()
        self.access=patch.dict(sys.modules,{'gadget_library':types.SimpleNamespace(detail=lambda *_:{})});self.access.start()
    def tearDown(self):self.access.stop();self.tmp.cleanup()
    def save(self,reviewer='alice',verdict='normal',revision=0,**extra):
        return cal.label(self.store,dict(username=reviewer),dict(motion_id='m00000001',check='penetration',verdict=verdict,revision=revision,source_sha256=self.sha,notes='',**extra))
    def test_per_person_latest_and_dispute(self):
        alice=self.save();bob=self.save('bob','defect')
        state=bob['current'][0];self.assertEqual(state['verdict'],'disputed');self.assertFalse(state['trusted']);self.assertEqual(len(state['opinions']),2)
        result=self.save('bob','normal',bob['event']['id']);self.assertEqual(result['current'][0]['verdict'],'normal')
        self.assertEqual(len(cal.evidence(self.root,'m00000001')['labels']),3)
    def test_stale_same_person_revision_rejected(self):
        first=self.save()
        with self.assertRaises(cal.CalibrationConflict) as caught:self.save(verdict='defect')
        self.assertEqual(caught.exception.current['id'],first['event']['id'])
        self.assertEqual(len(cal.evidence(self.root,'m00000001')['labels']),1)
    def test_source_is_bound(self):
        self.save();self.sha='source-v2'
        with self.assertRaises(cal.CalibrationConflict):
            cal.label(self.store,dict(username='alice'),dict(motion_id='m00000001',check='penetration',verdict='normal',revision=0,source_sha256='source-v1'))
        current=cal.history(self.store,dict(username='alice'),{'motion_id':'m00000001'})
        self.assertEqual(current['current'],[]);self.assertEqual(len(current['labels']),1)
    def test_specialty_history_pagination_retains_all(self):
        revision=0
        for i in range(25):revision=self.save(revision=revision)['event']['id']
        page=cal.history(self.store,{'username':'alice'},{'motion_id':['m00000001']});self.assertEqual(len(page['labels']),20)
        more=cal.history(self.store,{'username':'alice'},{'motion_id':'m00000001','after':page['next_after']});self.assertEqual(len(more['labels']),5);self.assertIsNone(more['next_after'])
        self.assertEqual(len({x['id'] for x in page['labels']+more['labels']}),25)
    def test_disputed_excluded_from_trusted_calibration(self):
        self.save();self.save('bob','defect')
        with sqlite3.connect(self.root/'review/auto_review.sqlite3') as db:
            db.executescript('CREATE TABLE runtime(key TEXT,value TEXT);CREATE TABLE scores(motion_id TEXT,version TEXT,source_sha256 TEXT,flags TEXT);')
            db.execute('INSERT INTO runtime VALUES(?,?)',('version','v1'));db.execute('INSERT INTO scores VALUES(?,?,?,?)',('m00000001','v1',self.sha,'["possible_self_penetration"]'))
        group=cal.calibration_summary(self.root)['groups'][0]
        self.assertEqual(group['verdict'],'disputed');self.assertEqual(group['scored'],1);self.assertEqual(group['trusted'],0);self.assertEqual(group['machine_flagged'],0)
    def test_normal_review_history_pagination_preserves_structured_fields(self):
        with self.store.reviews() as db:
            db.execute('CREATE TABLE events(motion_id TEXT,revision INTEGER,body TEXT)')
            for i in range(1,26):db.execute('INSERT INTO events VALUES(?,?,?)',('m00000001',i,json.dumps(dict(revision=i,issues=['foot_slide'],corrected_text='A person walks.'))))
        page=cal.review_history(self.store,'m00000001',user={'username':'alice'});more=cal.review_history(self.store,'m00000001',after=page['next_after'])
        self.assertEqual(len(page['items'])+len(more['items']),25);self.assertEqual(page['items'][0]['issues'],['foot_slide']);self.assertIsNone(more['next_after'])


if __name__=='__main__':unittest.main()
