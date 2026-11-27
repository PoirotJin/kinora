"""Presence is advisory, bounded, scoped and automatically expires."""
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from review_collaboration import presence

class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.store = SimpleNamespace(detail=lambda mid: {'motion_id':mid})
        self.alice = {'username':'alice'}
        self.bob = {'username':'bob'}
    def touch(self, user, client, active=True, now=1, mid='m00000001'):
        return presence(self.store, mid, user, {'client':client,'active':active},now=now)
    def test_multiple_users_tabs_release_and_expiry(self):
        self.touch(self.alice,'alice_tab_1')
        self.touch(self.alice,'alice_tab_2')
        self.touch(self.bob,'bob_tab_01')
        result=self.touch(self.alice,'alice_tab_1',False)
        self.assertEqual(result['reviewers'],['alice','bob'])
        self.assertEqual(self.touch(self.alice,'alice_tab_2',False)['reviewers'],['bob'])
        self.assertEqual(presence(self.store,'m00000001',self.alice,now=67)['reviewers'],[])
    def test_cannot_release_another_user_or_motion(self):
        self.touch(self.alice,'shared_id')
        self.touch(self.bob,'shared_id',False)
        self.touch(self.bob,'bob_tab_02',mid='m00000002')
        self.assertEqual(presence(self.store,'m00000001',self.bob,now=2)['reviewers'],['alice'])
    def test_concurrent_updates_and_validation(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda n:self.touch({'username':str(n)},f'client_{n:03}'),range(30)))
        self.assertEqual(len(presence(self.store,'m00000001',self.alice,now=2)['reviewers']),30)
        for body in ({'client':'short','active':True},{'client':'validclient','active':'true'},{'client':'validclient','active':True,'username':'alice'}):
            with self.assertRaises(ValueError):presence(self.store,'m00000001',self.bob,body,now=2)
    def test_route_permissions(self):
        from review_access import allowed
        user={'role':'user'}
        for route in ('/api/motions/m00000001/presence','/api/library/saved-searches'):
            self.assertTrue(allowed(user,route,False))
            self.assertTrue(allowed(user,route,True))
            self.assertFalse(allowed({'role':'unknown'},route,True))
        self.assertTrue(allowed(user,'/api/library/export',False))
        self.assertFalse(allowed(user,'/api/library/export',True))
        for route in ('/api/motions/m00000001/preview','/api/motions/m00000001/preview.bin'):
            self.assertTrue(allowed(user,route,False))
            self.assertFalse(allowed(user,route,True))
        self.assertFalse(allowed(user,'/api/motions/m00000001/previewXbin',False))
    def test_missing_removed_motion_checks_apply(self):
        def missing(mid): raise ValueError('not found')
        self.store.detail=missing
        with self.assertRaises(ValueError):self.touch(self.alice,'alice_tab_1')

if __name__=='__main__':unittest.main()
