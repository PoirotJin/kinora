"""Tag integration tests against temporary SQLite and real local HTTP."""
from contextlib import closing
import json
import unittest
import urllib.request
import urllib.error
from urllib.parse import urlencode
import test_gadget_v2 as fixtures
from review_tags import change,read,filter_sql
from review_access import claim_owners,remove
from g2h.farm_index import motion_number


class TagTests(unittest.TestCase):
    setUp=fixtures.GadgetTests.setUp
    tearDown=fixtures.GadgetTests.tearDown
    call=fixtures.GadgetTests.call

    def browser(self,path,value=None,username='alice'):
        token,_=self.auth.login(username,'test-password-123','tags-test')
        self.server.origins={'http://localhost'}
        request=urllib.request.Request(self.base.split('/api/gadget/')[0]+path,
            data=json.dumps(value).encode() if value is not None else None,
            headers={'Cookie':'mf_session='+token,'Origin':'http://localhost','X-MotionFarm-Request':'1','Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=4) as response:return json.load(response)

    def add(self,mid=None,user='alice',group='动作类型',tag='行走'):
        return change(self.store,mid or self.mid,{'username':user},{'group':group,'tag':tag})

    def test_shared_assignments_idempotency_and_audit(self):
        self.add();self.add();self.add(user='bob')
        result=read(self.store,self.mid)
        self.assertEqual(len(result['tags']),2);self.assertEqual(len(result['events']),2)
        self.assertEqual({x['username'] for x in result['tags']},{'alice','bob'})
        self.assertTrue(all(x['created_at']>0 for x in result['tags']))
        change(self.store,self.mid,{'username':'alice'},{'group':'动作类型','tag':'行走','action':'remove'})
        result=read(self.store,self.mid);self.assertEqual([x['username'] for x in result['tags']],['bob'])
        self.assertEqual([e['action'] for e in result['events']],['add','add','remove'])
        self.add();self.assertEqual(len(read(self.store,self.mid)['events']),4)

    def test_browser_identity_is_server_assigned(self):
        body={'group':'用途','tag':'测试'}
        result=self.browser('/api/motions/'+self.mid+'/tags',body)
        self.assertEqual(result['tags'][0]['username'],'alice')
        self.browser('/api/motions/'+self.mid+'/tags',body,username='bob')
        result=self.browser('/api/motions/'+self.mid+'/tags',{**body,'action':'remove'})
        self.assertEqual([x['username'] for x in result['tags']],['bob'])
        with self.assertRaises(urllib.error.HTTPError) as e:self.browser('/api/motions/'+self.mid+'/tags',{**body,'username':'bob'})
        self.assertEqual(e.exception.code,400)
        self.assertEqual(len(self.browser('/api/motions/'+self.mid+'/tags')['events']),3)

    def test_indexed_filters_and_same_assignment(self):
        self.add();self.add(user='bob',tag='转身');self.add(self.valid,group='用途',tag='行走')
        query=urlencode({'tag_group':'动作类型','tag':'行走','tagged_by':'alice'})
        page=self.call('search?'+query);self.assertEqual([x['motion_id'] for x in page['items']],[self.mid])
        self.assertEqual(page['scanned'],1);self.assertEqual(page['items'][0]['tags'][0]['username'],'alice')
        self.assertEqual(self.call('search?'+query.replace('alice','bob'))['items'],[])
        self.assertEqual(len(self.call('search?'+urlencode({'tag':'行走'}))['items']),2)
        page=self.browser('/api/motions?'+query);self.assertEqual([x['motion_id'] for x in page['items']],[self.mid])
        self.assertEqual(self.call('motion?id='+self.mid)['tags'][0]['tag'],'行走')
        with closing(self.store.catalog()) as db:
            db.execute('ATTACH DATABASE ? AS reviewdb',(str(self.store.path),))
            sql,args=filter_sql({'tag':['行走']})
            plan=' '.join(str(r) for r in db.execute('EXPLAIN QUERY PLAN SELECT m.number FROM motion_links m WHERE 1=1'+sql,args))
            self.assertIn('tags_name',plan);self.assertIn('motion_tags_tag',plan)

    def test_mcp_shared_authorship_removed_and_read_only(self):
        self.add(self.valid,user='bob');claim_owners(self.root,[self.valid],'bob')
        self.assertEqual([r['motion_id'] for r in self.call('search?'+urlencode({'tag':'行走'}))['items']],[self.valid])
        self.assertEqual(self.call('motion?id='+self.valid)['owner'],'bob')
        vocabulary=self.call('tags');self.assertNotIn('motion_id',json.dumps(vocabulary))
        self.add();remove(self.store,self.mid,{'role':'admin','username':'test'},'test')
        remove(self.store,self.valid,{'role':'admin','username':'test'},'test')
        self.assertEqual(self.call('search?'+urlencode({'tag':'行走'}))['items'],[])
        with self.assertRaises(ValueError):self.add()
        with self.assertRaises(urllib.error.HTTPError) as e:self.call('tags',{'group':'x','tag':'y'})
        self.assertEqual(e.exception.code,405)

    def test_normalization_pagination_and_limits(self):
        self.add(group=' Quality ',tag='ＷＡＬＫ');self.add(group='quality',tag='walk')
        self.assertEqual(len(read(self.store,self.mid)['tags']),1)
        self.add(self.valid,group='Quality',tag='WALK')
        page=self.call('search?tag=walk&limit=1');self.assertIsNotNone(page['next_after'])
        page2=self.call('search?tag=walk&after='+str(page['next_after']))
        self.assertEqual([x['motion_id'] for x in page2['items']],[self.valid])
        self.add(tag='二');first=self.call('tags?limit=1');second=self.call('tags?limit=1&after='+str(first['next_after']))
        self.assertNotEqual(first['items'][0]['tag_id'],second['items'][0]['tag_id'])
        for tag in ('','x'*65,'bad\nlabel'):
            with self.assertRaises(ValueError):self.add(tag=tag)
        for i in range(48):self.add(tag='label'+str(i))
        with self.assertRaises(ValueError):self.add(tag='overflow')
        self.assertEqual(len(read(self.store,self.mid)['tags']),50)

    def test_history_pagination_and_no_motion_mutation(self):
        path=self.root/'topics/walking/motions_000000.pkl';before=path.read_bytes()
        for i in range(51):
            self.add()
            change(self.store,self.mid,{'username':'alice'},{'group':'动作类型','tag':'行走','action':'remove'})
        page=read(self.store,self.mid);self.assertEqual(len(page['events']),100)
        self.assertEqual(len(read(self.store,self.mid,page['next_after'])['events']),2)
        self.assertEqual(path.read_bytes(),before)

if __name__=='__main__':unittest.main()
