from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import pickle
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from g2h.collection_store import CollectionStore,iter_committed
from g2h.cold_storage import package,archive_path,read_payload,usage,guard
from g2h.farm_index import read_motion


class ColdTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.folder=self.root/'topics/walking';self.folder.mkdir(parents=True)
        (self.root/'review').mkdir();self.key='a'*64
        with closing(sqlite3.connect(self.root/'prompts.sqlite3')) as db, db:
            db.executescript('CREATE TABLE prompts(id TEXT,item TEXT);CREATE TABLE motion_links(number INTEGER,task_id TEXT,text_id TEXT,topic TEXT,seed INTEGER);CREATE TABLE embeddings(text_id TEXT,vector BLOB);CREATE TABLE embedding_config(singleton INTEGER,model TEXT,dimensions INTEGER);')
            db.execute('INSERT INTO prompts VALUES(?,?)',('t'*64,json.dumps({'text':'A person walks forward.','id':'t'*64})))
            db.execute('INSERT INTO motion_links VALUES(1,?,?,?,42)',(self.key,'t'*64,'walking'))
            db.execute('INSERT INTO embeddings VALUES(?,?)',('t'*64,b'abcd'))
            db.execute("INSERT INTO embedding_config VALUES(1,'test',1)")
        with closing(sqlite3.connect(self.root/'review/reviews.sqlite3')) as db, db:
            db.execute('CREATE TABLE latest(motion_id TEXT,status TEXT,body TEXT)')
            db.execute('INSERT INTO latest VALUES(?,?,?)',('m00000001','accepted',json.dumps({'status':'accepted'})))
        with CollectionStore(self.folder) as store:
            with store.db:store.db.execute('INSERT INTO tasks(id,spec) VALUES(?,?)',(self.key,'{}'))
            store.append(dict(id=self.key,motion=b'repeated motion data '*8000))
            self.row=store.db.execute('SELECT shard,offset,bytes,sha256 FROM records').fetchone()
        self.raw=self.folder/'motions_000000.pkl';self.original=self.raw.read_bytes()
        os.utime(self.raw,(1,1))

    def tearDown(self):self.tmp.cleanup()

    def test_lossless_transparent_read_and_new_shard(self):
        result=package(self.root,'walking',0)
        self.assertFalse(self.raw.exists());self.assertLess(result['archive_bytes'],result['raw_bytes'])
        self.assertEqual(read_payload(self.folder,*self.row,self.key),self.original)
        with closing(sqlite3.connect(self.root/'prompts.sqlite3')) as db:
            self.assertEqual(read_motion(db,self.root,'m00000001')['id'],self.key)
        self.assertEqual(list(iter_committed(self.folder))[0]['id'],self.key)
        with CollectionStore(self.folder) as store:
            store.append(dict(id='b'*64,motion='new'))
            self.assertEqual(store.db.execute('SELECT shard FROM records WHERE id=?',('b'*64,)).fetchone()[0],1)
        self.assertTrue(archive_path(self.folder,0).exists())

    def test_export_unreviewed_is_portable_and_does_not_migrate(self):
        with closing(sqlite3.connect(self.root/'review/reviews.sqlite3')) as db, db:db.execute('DELETE FROM latest')
        with self.assertRaises(RuntimeError):package(self.root,'walking',0)
        out=self.root/'exports/test.mf.zip';package(self.root,'walking',0,out)
        self.assertEqual(self.raw.read_bytes(),self.original)
        import zipfile
        with zipfile.ZipFile(out) as archive:
            manifest=json.loads(archive.read('manifest.json'));entry=manifest['records'][0]
            self.assertEqual(entry['motion_id'],'m00000001');self.assertEqual(entry['embedding'],'YWJjZA==')
            self.assertEqual(archive.read(entry['member']),self.original)

    def test_writer_lock_and_failed_publish_preserve_source(self):
        with CollectionStore(self.folder):
            with self.assertRaises(RuntimeError):package(self.root,'walking',0)
        with patch('g2h.cold_storage.os.replace',side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):package(self.root,'walking',0)
        self.assertEqual(self.raw.read_bytes(),self.original)
        self.assertFalse(archive_path(self.folder,0).exists())
        self.assertFalse(Path(str(archive_path(self.folder,0))+'.partial').exists())

    def test_checksum_age_and_quota(self):
        os.utime(self.raw,None)
        with self.assertRaises(RuntimeError):package(self.root,'walking',0)
        with patch('g2h.cold_storage.LIMIT_BYTES',1),patch('g2h.cold_storage.RESERVE_BYTES',0):
            with self.assertRaises(RuntimeError):guard(self.root)
        self.raw.write_bytes(b'X'+self.original[1:])
        with self.assertRaises(RuntimeError):package(self.root,'walking',0,min_age_days=0)
        self.assertTrue(self.raw.exists())

    def test_directory_size_counts_exports_and_rejects_symlinks(self):
        before=usage(self.root)['total'];(self.root/'exports').mkdir();(self.root/'exports/a').write_bytes(b'x'*1000)
        self.assertEqual(usage(self.root)['total'],before+1000)
        (self.root/'linked').symlink_to(self.folder,target_is_directory=True)
        with self.assertRaises(RuntimeError):usage(self.root)

    def test_recovers_published_archive_before_index_commit(self):
        out=archive_path(self.folder,0)
        package(self.root,'walking',0,destination=out)
        self.assertTrue(self.raw.exists())
        with closing(sqlite3.connect(self.folder/'index.sqlite3')) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM cold_shards').fetchone())
        result=package(self.root,'walking',0)
        self.assertTrue(result['already']);self.assertFalse(self.raw.exists())
        self.assertEqual(read_payload(self.folder,*self.row,self.key),self.original)

    def test_recovers_index_commit_before_hot_unlink(self):
        out=archive_path(self.folder,0)
        package(self.root,'walking',0,destination=out)
        with closing(sqlite3.connect(self.folder/'index.sqlite3')) as db, db:
            db.execute('INSERT INTO cold_shards VALUES(?,?,?)',(0,out.stat().st_size,1))
        package(self.root,'walking',0)
        self.assertFalse(self.raw.exists())
        self.assertEqual(read_payload(self.folder,*self.row,self.key),self.original)


if __name__=='__main__':unittest.main()
