from contextlib import closing
import fcntl
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parent)]
import numpy as np
from test_review_server import ReviewTests
from g2h import direction_index as index
from g2h.farm_index import short_id,register_motion
from g2h.collection_store import CollectionStore
from gadget_library import search
from backfill_direction import batch,busy


class DirectionTests(unittest.TestCase):
    setUp=ReviewTests.setUp
    tearDown=ReviewTests.tearDown

    def save(self,number,status,version=None):
        report=dict(version=version or index.VERSION,status=status,reasons=[],checks=[])
        index.save(self.root,short_id(number),'fixture',report)

    def test_four_states_and_manual_review_are_independent(self):
        import sqlite3
        with sqlite3.connect(self.root/'prompts.sqlite3') as db:
            for n in range(2,6):db.execute('INSERT INTO motion_links(number,task_id,text_id,topic,seed) VALUES(?,?,?,?,?)',(n,'task'+str(n),'a'*64,'walking',n))
        for n,s in [(1,'pass'),(2,'fail'),(3,'review')]:self.save(n,s)
        with closing(index.connect(self.root)) as db,db:
            db.execute('INSERT INTO results VALUES(?,?,?,?,?,?)',(4,'old','pass','fixture','{}',0))
        expected={'direction_pass':[1],'direction_fail':[2],'direction_review':[3],'direction_not_checked':[4,5]}
        with patch('review_server.motion_info',side_effect=lambda root,row:dict(motion_id=short_id(row[0]),state='done')):
            for selected,nums in expected.items():
                result=self.store.listing(status=selected)
                self.assertEqual([x['motion_id'] for x in result['items']],[short_id(n) for n in nums])
        self.store.save(self.mid,self.value)
        self.assertEqual(self.store.listing(status='accepted')['items'][0]['direction_status'],'pass')
        self.assertEqual(index.summary(self.root)['checked'],3)
        self.save(1,'fail');self.save(1,'fail')
        self.assertEqual(index.summary(self.root)['counts']['fail'],2)
        self.assertEqual(index.summary(self.root)['checked'],3)

    def test_desktop_filter_is_applied_before_window_and_pages(self):
        import sqlite3
        with sqlite3.connect(self.root/'prompts.sqlite3') as db:
            for n in range(2,525):db.execute('INSERT INTO motion_links(number,task_id,text_id,topic,seed) VALUES(?,?,?,?,?)',(n,'task'+str(n),'a'*64,'walking',n))
        for n in (510,520,521):self.save(n,'pass')
        info=lambda root,row:dict(motion_id=short_id(row[0]),state='done',location=True)
        with patch('gadget_library.motion_info',side_effect=info):
            result=search(self.store,dict(username='alice',role='user'),dict(status=['direction_pass'],limit=['2']))
            self.assertEqual([i['motion_id'] for i in result['items']],[short_id(510),short_id(520)])
            next_page=search(self.store,dict(username='alice',role='user'),dict(status=['direction_pass'],limit=['2'],after=[str(result['next_after'])]))
            self.assertEqual([i['motion_id'] for i in next_page['items']],[short_id(521)])
            self.assertIsNone(next_page['next_after'])

    def test_backfill_resumes_and_yields_to_generation(self):
        from types import SimpleNamespace

        with patch('backfill_direction.os.getloadavg', return_value=(0, 0, 0)), \
                patch('backfill_direction.shutil.disk_usage', return_value=SimpleNamespace(free=20_000_000_000)):
            self.assertEqual(batch(self.root,'walking',1,mib_per_second=16),1)
            before=index.summary(self.root)
            self.assertEqual(batch(self.root,'walking',1,mib_per_second=16),0)
            self.assertEqual(index.summary(self.root)['scanned'],before['scanned'])
            with (self.root/'gpu_worker.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                self.assertIn('正在使用 GPU', busy(self.root))
                self.assertEqual(batch(self.root,'walking',1,mib_per_second=16),0)

    def test_real_motion_backfill(self):
        import MotionFarm as farm
        with np.load(Path(__file__).parent/'fixtures/m000096vn-semantic.npz',allow_pickle=False) as z:
            data={k:z[k].copy() for k in z.files};text=str(z['text'])
        with closing(farm.catalog(self.root)) as db:
            item=dict(id='b'*64,text=text,topic_id='walking',attributes={},source={})
            with db:db.execute('INSERT INTO prompts VALUES(?,?,?,?)',(item['id'],'walking',text,json.dumps(item)))
            mid=register_motion(db,item['id'],'walking','regression',10)
        with CollectionStore(self.root/'topics/walking') as store:
            with store.db:store.db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)',('regression','{}'))
            store.append(dict(id='regression',text=text,motion=data))
        with patch('backfill_direction.busy',return_value=''):
            self.assertEqual(batch(self.root,'walking',16,mib_per_second=16),2)
        self.assertEqual(index.statuses(self.root,[mid])[mid],'fail')


if __name__=='__main__':unittest.main()
