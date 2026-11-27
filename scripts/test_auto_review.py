import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from g2h import auto_review as ar


class AutoReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'review').mkdir()
        self.h = sqlite3.connect(self.root/'review/reviews.sqlite3')
        self.h.execute('CREATE TABLE latest(motion_id TEXT PRIMARY KEY,body TEXT)')
        self.addCleanup(self.h.close)

    def add(self, i, score=.9, status=None, topic='walking', text_id=None, flags=None):
        mid=f'm{i:08d}'
        ar.save(self.root,dict(motion_id=mid,topic=topic,text_id=text_id or 't'+str(i)),
                'hash',dict(score=score,version='v1'),flags or [])
        if status:
            self.h.execute('INSERT OR REPLACE INTO latest VALUES(?,?)',
                           (mid,json.dumps(dict(status=status,reviewer='tester',revision=1,motion_sha256='hash',updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat()))))
            self.h.commit()
        return mid

    def test_missing_and_high_similarity_never_auto_pass(self):
        self.assertEqual(ar.detail(self.root,'missing')['state'],'pending')
        mid=self.add(1,1.,'accepted')
        self.assertEqual(ar.detail(self.root,mid)['state'],'needs_review')
        ar.calibrate(self.root,'v1')
        with ar.connect(self.root) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM calibration').fetchone()[0],0)

    def train(self):
        for i in range(30):
            self.add(i,.5 if i<5 else .9,'rejected' if i<5 else 'accepted')
        ar.calibrate(self.root,'v1')

    def test_frozen_threshold_independent_groups_and_revocation(self):
        self.train()
        with ar.connect(self.root) as db:
            p=dict(db.execute('SELECT * FROM calibration').fetchone())
        self.assertEqual(p['state'],'validating')
        ids=[]
        i=100
        while len(ids)<75:
            if ar.sampled('t'+str(i)):
                ids.append(self.add(i,.95,'accepted'))
            i+=1
        ar.calibrate(self.root,'v1')
        self.assertEqual(ar.detail(self.root,ids[0])['state'],'auto_passed')
        for mid in ids[:10]:
            self.h.execute('UPDATE latest SET body=? WHERE motion_id=?',
                           (json.dumps(dict(status='rejected',revision=2,reviewer='tester',motion_sha256='hash',updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat())),mid))
        self.h.commit()
        ar.calibrate(self.root,'v1')
        self.assertEqual(ar.detail(self.root,ids[-1])['state'],'needs_review')
        with ar.connect(self.root) as db:
            self.assertEqual(db.execute('SELECT threshold FROM calibration').fetchone()[0],p['threshold'])

    def test_variants_cannot_inflate_calibration(self):
        for i in range(100):
            self.add(i,.9,'accepted',text_id='same')
        ar.calibrate(self.root,'v1')
        with ar.connect(self.root) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM calibration').fetchone()[0],0)

    def test_geometry_veto_and_missing_scores(self):
        self.train()
        with ar.connect(self.root) as db:
            db.execute("UPDATE calibration SET state='eligible'")
        flagged=self.add(200,1.,flags=['possible_foot_slide'])
        missing=self.add(201,None)
        other=self.add(202,1.,topic='crawling')
        for mid in (flagged,missing,other):
            self.assertEqual(ar.detail(self.root,mid)['state'],'needs_review')

    def test_version_isolation_and_immutable_scores(self):
        mid=self.add(1,.7)
        self.add(1,.99)
        self.assertEqual(ar.detail(self.root,mid)['score'],.7)
        with self.assertRaises(ValueError):
            self.add(2,float('nan'))
        self.assertGreater(ar.wilson_upper(0,10),.05)
        self.assertLess(ar.wilson_upper(0,75),.05)

    def test_invalid_data_is_quarantined_without_human_changes(self):
        ar.save(self.root,dict(motion_id='bad',topic='walking',text_id='tbad'),'hash',
                dict(version='v1',score=None,integrity='invalid'),['invalid_motion'])
        self.assertEqual(ar.detail(self.root,'bad')['state'],'quarantined')
        self.assertEqual(self.h.execute('SELECT count(*) FROM latest').fetchone()[0],0)

    def test_old_labels_or_wrong_source_cannot_validate(self):
        self.train()
        ids=[]
        for i in range(100,700):
            if ar.sampled('t'+str(i)) and len(ids)<75:
                ids.append(self.add(i,.95,'accepted'))
        self.h.execute('UPDATE latest SET body=? WHERE motion_id=?',
                       (json.dumps(dict(status='accepted',motion_sha256='different',updated_at='2099-01-01T00:00:00+00:00')),ids[0]))
        self.h.commit()
        ar.calibrate(self.root,'v1')
        self.assertEqual(ar.detail(self.root,ids[-1])['state'],'needs_review')


if __name__=='__main__':
    unittest.main()
