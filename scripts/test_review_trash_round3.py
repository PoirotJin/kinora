"""Bounded recovery list tests using real temporary review tables; no production I/O."""
from contextlib import closing
import unittest
from urllib.parse import parse_qs,urlencode
import test_review_server as fixtures
import test_review_access as access
from review_access import Forbidden,remove
from review_trash import listing

ADMIN={'username':'jfc','role':'admin'}
class TrashTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.ReviewTests();self.f.setUp();self.store=self.f.store
        with closing(self.store.reviews()) as db,db:
            for i in range(1,122):db.execute('INSERT INTO removed VALUES(?,?,?,?)',(f'm{i:08d}',1000,'jfc','test tombstone'))
    def tearDown(self):self.f.tearDown()
    def page(self,**query):return listing(self.store,ADMIN,parse_qs(urlencode(query)))
    def test_equal_timestamp_order_complete_without_duplicates(self):
        ids=[];cursor=''
        while True:
            page=self.page(limit=50,after=cursor);self.assertLessEqual(len(page['items']),50)
            ids.extend(x['motion_id'] for x in page['items']);cursor=page['next_after']
            if not cursor:break
        self.assertEqual(ids,[f'm{i:08d}' for i in range(121,0,-1)]);self.assertEqual(len(set(ids)),121)
    def test_exact_id_finds_oldest_beyond_first_page(self):
        self.assertNotIn('m00000001',[x['motion_id'] for x in self.page()['items']])
        found=self.page(q='m00000001');self.assertEqual([x['motion_id'] for x in found['items']],['m00000001']);self.assertIsNone(found['next_after'])
        self.assertEqual(self.page(q='m99999999')['items'],[])
    def test_restore_and_new_removal_do_not_shift_next_page(self):
        first=self.page(limit=50);before=[x['motion_id'] for x in first['items']]
        with closing(self.store.reviews()) as db,db:
            db.execute('DELETE FROM removed WHERE motion_id=?',(before[2],))
            db.execute('INSERT INTO removed VALUES(?,?,?,?)',('m00000122',1001,'jfc','new removal'))
        second=self.page(limit=100,after=first['next_after']);ids=before+[x['motion_id'] for x in second['items']]
        self.assertEqual(len(set(ids)),121);self.assertNotIn('m00000122',ids)
        self.assertEqual(self.page()['items'][0]['motion_id'],'m00000122')
    def test_validation_and_admin_only(self):
        for query in [{'limit':0},{'limit':101},{'q':'walking'},{'after':'invalid'}, {'q':'m00000001','after':self.page()['next_after']}]:
            with self.assertRaises(ValueError):self.page(**query)
        with self.assertRaises(Forbidden):listing(self.store,{'username':'alice','role':'user'},{})
    def test_real_restore_preserves_payload_and_audit(self):
        with closing(self.store.reviews()) as db,db:db.execute('DELETE FROM removed WHERE motion_id=?',(self.f.mid,))
        before=(self.f.root/'topics/walking/motions_000000.pkl').read_bytes()
        remove(self.store,self.f.mid,ADMIN,'fixture remove')
        self.assertEqual(self.page(q=self.f.mid)['items'][0]['motion_id'],self.f.mid)
        remove(self.store,self.f.mid,ADMIN,'fixture restore',True)
        self.assertEqual(self.page(q=self.f.mid)['items'],[])
        self.assertEqual(before,(self.f.root/'topics/walking/motions_000000.pkl').read_bytes())
        with closing(self.store.reviews()) as db:self.assertEqual([r[0] for r in db.execute('SELECT action FROM removal_events WHERE motion_id=? ORDER BY id',(self.f.mid,))],['remove','restore'])

class TrashHTTPTests(unittest.TestCase):
    def setUp(self):self.f=access.AccessTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_route_cursor_search_roles_and_actual_restore(self):
        with closing(self.f.store.reviews()) as db,db:
            for i in range(1,102):db.execute('INSERT INTO removed VALUES(?,?,?,?)',(f'm{i:08d}',1000,'jfc','test'))
        self.assertEqual(self.f.call('/api/removed')[0],403)
        code,first=self.f.call('/api/removed?limit=100','jfc');self.assertEqual(code,200);self.assertEqual(len(first['items']),100)
        code,last=self.f.call('/api/removed?'+urlencode({'after':first['next_after']}),'jfc');self.assertEqual(code,200);self.assertEqual([r['motion_id'] for r in last['items']],['m00000001'])
        code,found=self.f.call('/api/removed?q=m00000001','jfc');self.assertEqual(code,200);self.assertEqual(found['items'][0]['motion_id'],'m00000001')
        self.assertEqual(self.f.call('/api/removed?after=garbage','jfc')[0],400)
        self.assertEqual(self.f.call('/api/motions/m00000001/restore','jfc',{})[0],200)
        self.assertEqual(self.f.call('/api/removed?q=m00000001','jfc')[1]['items'],[])

if __name__=='__main__':unittest.main()
