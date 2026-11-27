"""Tier migration checks use tiny temporary fixtures, never production data."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_cold_storage import ColdTests
from g2h.cold_storage import archive_path, package, read_payload
from g2h.capacity import ensure, Reconciler, status
from g2h.storage_tiers import cold_root


class SeparateColdTests(ColdTests):
    def setUp(self):
        super().setUp()
        self.cold_tmp=tempfile.TemporaryDirectory()
        self.cold=Path(self.cold_tmp.name).resolve()
        self.config=self.root/'storage_tiers.json'
        self.config.write_text(json.dumps({'cold_root':str(self.cold),'separate_devices':False}))
        ensure(self.cold)

    def tearDown(self):
        self.cold_tmp.cleanup()
        super().tearDown()

    def test_archive_accounting_is_separate_and_downloadable(self):
        result=package(self.root,'walking',0)
        path=Path(result['path'])
        self.assertTrue(path.is_relative_to(self.cold))
        self.assertEqual(read_payload(self.folder,*self.row,self.key),self.original)
        for root in (self.root,self.cold):
            scan=Reconciler(root)
            for _ in range(100):
                if scan.step():break
            else:self.fail('small tier reconciliation did not finish')
        self.assertEqual(status(self.root)['cold'],0)
        self.assertEqual(status(self.cold)['cold'],path.stat().st_size)
        from review_storage import Storage
        storage=Storage(self.root,self.root/'unused-topics.json')
        with closing(storage.db()) as db, db:
            db.execute('INSERT INTO jobs(id,status,result) VALUES(?,?,?)',('test','completed',json.dumps(result)))
        self.assertEqual(storage.download('test'),path)

    def test_cold_quota_refusal_keeps_hot_source(self):
        with closing(sqlite3.connect(self.cold/'review/storage/capacity.sqlite3')) as db, db:
            db.execute('UPDATE capacity SET total=800000000000 WHERE id=1')
        with self.assertRaises(RuntimeError):package(self.root,'walking',0)
        self.assertEqual(self.raw.read_bytes(),self.original)
        self.assertFalse(archive_path(self.folder,0).exists())

    def test_missing_mount_refuses_cold_write(self):
        self.config.write_text(json.dumps({'cold_root':str(self.cold),'cold_mount':str(self.cold),'separate_devices':False}))
        with patch('g2h.storage_tiers.os.path.ismount',return_value=False):
            with self.assertRaises(RuntimeError):package(self.root,'walking',0)
        self.assertEqual(self.raw.read_bytes(),self.original)

    def test_same_device_rejected_in_production_mode(self):
        self.config.write_text(json.dumps({'cold_root':str(self.cold)}))
        with self.assertRaises(RuntimeError):cold_root(self.root)

    def test_legacy_archive_is_still_readable(self):
        self.config.unlink()
        result=package(self.root,'walking',0)
        self.config.write_text(json.dumps({'cold_root':str(self.cold),'separate_devices':False}))
        self.assertEqual(archive_path(self.folder,0),Path(result['path']))
        self.assertEqual(read_payload(self.folder,*self.row,self.key),self.original)


if __name__=='__main__':unittest.main()
