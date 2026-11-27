"""Collection deletion uses disposable SQLite only; shared access has HTTP fixtures."""
from contextlib import closing
import sqlite3,tempfile,types,unittest
from pathlib import Path
from review_collections import setup,change,collections


class CollectionDeletion(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'reviews.sqlite3'
        self.store=types.SimpleNamespace(reviews=lambda:sqlite3.connect(self.path))
        with closing(self.store.reviews()) as db:setup(db)
        self.alice={'username':'alice'};self.bob={'username':'bob'}
    def tearDown(self):self.tmp.cleanup()
    def test_delete_releases_quota_and_keeps_other_users(self):
        for n in range(100):change(self.store,self.alice,dict(id='set'+str(n),action='rename'))
        change(self.store,self.bob,dict(id='set0',action='rename'))
        with closing(self.store.reviews()) as db,db:
            db.executemany('INSERT INTO collection_items VALUES(?,?,?,0)',[('alice','set0','m00000001'),('bob','set0','m00000001')])
            db.execute('INSERT INTO collection_events(owner,collection_id,motion_id,action,at) VALUES(?,?,?,?,0)',('alice','set0','m00000001','add'))
        with self.assertRaises(ValueError):change(self.store,self.alice,dict(id='new',action='rename'))
        change(self.store,self.alice,dict(id='set0',action='delete'))
        change(self.store,self.alice,dict(id='new',action='rename'))
        with closing(self.store.reviews()) as db:
            self.assertEqual(db.execute('SELECT owner FROM collection_items').fetchall(),[('bob',)])
            self.assertEqual(db.execute('SELECT owner FROM motion_collections WHERE id=?',('set0',)).fetchall(),[('bob',)])
            self.assertEqual(db.execute('SELECT action FROM collection_events WHERE owner=? ORDER BY id',('alice',)).fetchall(),[('add',),('delete',)])
        self.assertEqual(len(collections(self.store,self.alice,{})['collections']),100)
    def test_delete_replay_and_stale_remove_do_not_recreate(self):
        change(self.store,self.alice,dict(id='favorites',action='rename'))
        for _ in range(2):change(self.store,self.alice,dict(id='favorites',action='delete'))
        change(self.store,self.alice,dict(id='favorites',motion_id='m00000001',action='remove'))
        self.assertEqual(collections(self.store,self.alice,{})['collections'],[])
        with closing(self.store.reviews()) as db:self.assertEqual(db.execute('SELECT count(*) FROM collection_events').fetchone()[0],1)
    def test_delete_cannot_target_another_owner(self):
        change(self.store,self.bob,dict(id='private',action='rename'))
        change(self.store,self.alice,dict(id='private',action='delete'))
        self.assertEqual(len(collections(self.store,self.bob,{})['collections']),1)
        with self.assertRaises(ValueError):change(self.store,self.alice,dict(id='private',action='delete',owner='bob'))


if __name__=='__main__':unittest.main()
