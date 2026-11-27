"""Preset lifecycle on real temporary SQLite; no HTTP, API or GPU work."""
from contextlib import closing
from pathlib import Path
import sqlite3,tempfile,unittest
from concurrent.futures import ThreadPoolExecutor
import review_personal as personal
from review_access import Forbidden
from retarget_parameters import resolve

class Store:
    def __init__(self,root):
        self.root=root;self.path=root/'review/reviews.sqlite3';self.path.parent.mkdir(parents=True)
    def reviews(self):return sqlite3.connect(self.path,timeout=3)

class PersonalLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name));self.alice={'username':'alice','role':'user'};self.bob={'username':'bob','role':'user'}
        with closing(self.store.reviews()) as db:personal.setup(db)
    def tearDown(self):self.tmp.cleanup()
    def body(self,n=1,**changes):return dict(dict(id=f'{n:032x}',version=0,name=f'Scheme {n}',robot='unitree_g1_23dof',notes='',parameters={'smooth_weight':.4}),**changes)
    def save(self,n=1,**changes):return personal.save(self.store,self.alice,self.body(n,**changes))
    def action(self,p,action,user=None):return personal.mutate(self.store,user or self.alice,{key:p[key] for key in ('id','version','archive_revision')}|{'action':action})
    def page(self,archived=False,after=''):return personal.listing(self.store,self.alice,{'archived':['1' if archived else '0'],'after':[after]})
    def test_archive_restore_keeps_versions_and_exact_execution_snapshot(self):
        p=self.save();self.save(version=1,parameters={'smooth_weight':.9});recipe={'preset_id':p['id'],'preset_version':1};before=resolve(self.store.root,self.alice,p['robot'],recipe)
        with closing(self.store.reviews()) as db:raw=db.execute('SELECT version,body FROM personal_preset_versions ORDER BY version').fetchall()
        p=self.page()['items'][0];archived=self.action(p,'archive')
        self.assertEqual((archived['archived'],archived['version'],archived['archive_revision']),(1,2,1));self.assertEqual(self.page()['items'],[]);self.assertEqual(self.page(True)['items'][0]['id'],p['id'])
        self.assertEqual((self.page()['active_count'],self.page()['archived_count']),(0,1));self.assertEqual(resolve(self.store.root,self.alice,p['robot'],recipe),before)
        history=personal.history(self.store,self.alice,{'id':[p['id']]});self.assertEqual([v['version'] for v in history['versions']],[1,2]);self.assertTrue(history['preset']['archived'])
        restored=self.action(archived,'restore');self.assertEqual((restored['archived'],restored['version'],restored['archive_revision']),(0,2,2))
        with closing(self.store.reviews()) as db:self.assertEqual(db.execute('SELECT version,body FROM personal_preset_versions ORDER BY version').fetchall(),raw)
    def test_ownership_remains_private_even_for_other_admin(self):
        p=self.save()
        for user in (self.bob,dict(self.bob,role='admin')):
            with self.subTest(user=user),self.assertRaises(Forbidden):self.action(p,'archive',user)
            with self.assertRaises(Forbidden):personal.history(self.store,user,{'id':[p['id']]})
            self.assertEqual(personal.listing(self.store,user,{'archived':['1']})['items'],[])
        self.assertFalse(self.page()['items'][0]['archived'])
    def test_archived_scheme_cannot_be_edited_or_restored_by_save(self):
        self.action(self.save(),'archive');from review_server import Conflict
        with self.assertRaises(Conflict):self.save(version=1,parameters={'smooth_weight':.8})
        with self.assertRaises(Conflict):self.save(version=1)
        self.assertEqual(self.page()['active_count'],0);self.assertEqual(self.page(True)['items'][0]['version'],1)
    def test_archive_frees_capacity_and_restore_checks_limit(self):
        for n in range(1,501):self.save(n)
        with self.assertRaisesRegex(ValueError,'归档'):self.save(501)
        first=self.action(self.page()['items'][0],'archive');new=self.save(501);self.assertEqual(self.page()['active_count'],500)
        with self.assertRaisesRegex(ValueError,'先归档'):self.action(first,'restore')
        self.assertEqual(self.page(True)['items'][0]['archive_revision'],1)
        self.action(new,'archive');self.assertEqual(self.action(first,'restore')['archived'],0);self.assertEqual(self.page()['active_count'],500)
    def test_concurrent_restore_cannot_overfill_last_slot(self):
        first=self.action(self.save(1),'archive');second=self.action(self.save(2),'archive')
        for n in range(3,502):self.save(n)
        def restore(p):
            try:return self.action(p,'restore')['id']
            except ValueError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(restore,[first,second]))
        self.assertEqual(sum(x is not None for x in results),1);self.assertEqual(self.page()['active_count'],500)
    def test_archive_replay_is_safe_and_old_cycle_cannot_override_restore(self):
        p=self.save();archived=self.action(p,'archive');self.assertEqual(self.action(p,'archive'),archived)
        restored=self.action(archived,'restore');self.assertEqual(self.action(archived,'restore'),restored)
        from review_server import Conflict
        with self.assertRaises(Conflict):self.action(p,'archive')
        self.assertEqual(self.page()['items'][0]['archive_revision'],2)
    def test_stale_parameter_version_blocks_archive(self):
        p=self.save();self.save(version=1,name='New revision');from review_server import Conflict
        with self.assertRaises(Conflict):self.action(p,'archive')
        self.assertEqual(self.page()['items'][0]['version'],2)
    def test_active_and_archived_pagination_is_stable_disjoint_and_owned(self):
        for n in range(1,105):
            p=self.save(n)
            if n%2==0:self.action(p,'archive')
        personal.save(self.store,self.bob,self.body(999))
        for archived in (False,True):
            first=self.page(archived);second=self.page(archived,first['next_after']);ids=[p['id'] for p in first['items']+second['items']]
            self.assertEqual(len(ids),52);self.assertEqual(ids,sorted(set(ids)));self.assertIsNone(second['next_after']);self.assertTrue(all(int(x,16)%2==(0 if archived else 1) for x in ids))
    def test_version_pagination_survives_archive(self):
        for version in range(52):p=self.save(version=version,notes=str(version))
        self.action(p,'archive');first=personal.history(self.store,self.alice,{'id':[p['id']]});second=personal.history(self.store,self.alice,{'id':[p['id']],'after':[str(first['next_after'])]})
        self.assertEqual([v['version'] for v in first['versions']+second['versions']],list(range(1,53)))
    def test_strict_action_validation_and_save_dispatch(self):
        p=personal.mutate(self.store,self.alice,self.body());valid={key:p[key] for key in ('id','version','archive_revision')}|{'action':'archive'}
        for changes in ({'action':'delete'},{'archive_revision':True},{'version':True},{'owner':'bob'},{'id':'../bad'}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):personal.mutate(self.store,self.alice,valid|changes)
        with self.assertRaises(ValueError):personal.listing(self.store,self.alice,{'archived':['both']})
    def test_legacy_migration_preserves_parameter_rows(self):
        with closing(sqlite3.connect(self.store.root/'legacy.sqlite3')) as db:
            db.execute('CREATE TABLE personal_presets(id TEXT PRIMARY KEY,owner TEXT,name TEXT,robot TEXT,notes TEXT,parameters TEXT,version INTEGER,created REAL,updated REAL)')
            db.execute('INSERT INTO personal_presets VALUES(?,?,?,?,?,?,?,?,?)',('f'*32,'alice','Legacy','unitree_g1_23dof','','{}',1,1.,1.));db.commit();personal.setup(db);personal.setup(db)
            self.assertEqual(db.execute('SELECT name,version,archived,archive_revision FROM personal_presets').fetchone(),('Legacy',1,0,0))

if __name__=='__main__':unittest.main()
