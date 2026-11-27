"""Preview integrity, cache authorization, and trusted-proxy boundaries."""
from contextlib import closing
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
from test_review_server import ReviewTests
from review_server import Handler, read_motion
from review_access import Removed
from review_preview import PreviewCache, world_coordinates


class PreviewTests(unittest.TestCase):
    setUp=ReviewTests.setUp
    tearDown=ReviewTests.tearDown

    def test_thumbnail_and_binary_share_conversion(self):
        with patch('review_server.read_motion',wraps=read_motion) as load:
            thumbnail=self.store.preview(self.mid,'thumbnail')
            wire=self.store.preview(self.mid,'binary')
            legacy=self.store.preview(self.mid)
            self.assertEqual(load.call_count,1)
        self.assertEqual(wire[:8],b'MFPRV001')
        size=struct.unpack('<I',wire[8:12])[0]
        meta=json.loads(wire[12:12+size])
        world=np.frombuffer(wire[12+size:],dtype='<f4').reshape(meta['frames'],meta['joint_count'],3)
        np.testing.assert_allclose(world,legacy['joints'],atol=1e-6)
        np.testing.assert_allclose(world[-1],thumbnail['joints'],atol=1e-6)
        np.testing.assert_allclose(world[0,0],[0,1.2,0],atol=1e-6)

    def test_cached_motion_still_checks_removal(self):
        self.store.preview(self.mid,'binary')
        with closing(self.store.reviews()) as db,db:
            db.execute('INSERT INTO removed VALUES(?,?,?,?)',(self.mid,0,'admin','test'))
        with self.assertRaises(Removed):self.store.preview(self.mid,'thumbnail')
        with self.assertRaises(Removed):self.store.preview(self.mid,'binary')

    def test_changed_digest_does_not_serve_stale_cache(self):
        self.store.preview(self.mid,'binary')
        import sqlite3
        with closing(sqlite3.connect(self.root/'topics/walking/index.sqlite3')) as db,db:
            db.execute('UPDATE records SET sha256=?',('0'*64,))
        with self.assertRaises(ValueError):self.store.preview(self.mid,'binary')

    def test_cache_budget_does_not_grow_without_bound(self):
        self.store.preview_cache=PreviewCache(self.assets,max_bytes=1)
        self.store.preview(self.mid)
        self.assertEqual(self.store.preview_cache.bytes,0)
        self.assertEqual(len(self.store.preview_cache.entries),0)

    def test_finite_and_dimension_guards(self):
        with self.assertRaises(ValueError):world_coordinates(np.full((1,2,3),np.nan),np.zeros((1,3)),np.zeros(3))
        with self.assertRaises(ValueError):world_coordinates(np.zeros((3601,2,3)),np.zeros((3601,3)),np.zeros(3))

    def test_forwarded_ip_only_trusted_from_configured_loopback(self):
        handler=object.__new__(Handler)
        handler.headers={'X-Real-IP':'203.0.113.8'}
        handler.server=SimpleNamespace(behind_proxy=True)
        handler.client_address=('127.0.0.1',1234)
        self.assertEqual(handler.peer(),'203.0.113.8')
        handler.client_address=('192.0.2.1',1234)
        self.assertEqual(handler.peer(),'192.0.2.1')
        handler.client_address=('127.0.0.1',1234);handler.server.behind_proxy=False
        self.assertEqual(handler.peer(),'127.0.0.1')


if __name__=='__main__':unittest.main()
