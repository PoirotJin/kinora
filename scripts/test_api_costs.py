from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from g2h.api_usage import billed_to, attributed_call, connect, record
from g2h.api_costs import estimate, load_prices, summarize

class CostsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'api_usage.sqlite3'
        self.prices=self.path.with_name('api_prices.json')
    def price(self,rate=2,version='test-v1'):
        self.prices.write_text(json.dumps({'version':version,'prices':[{'provider':'test','model':'model','kind':'chat','currency':'CNY','input_per_million':rate,'cached_input_per_million':1,'output_per_million':4}]}))
    def test_frozen_attribution_and_unknown(self):
        self.price()
        with billed_to('alice',self.path,purpose='rewrite',job_id='job1',provider='test'):
            record('model','chat',{'id':'one','usage':{'prompt_tokens':1000,'completion_tokens':100,'prompt_tokens_details':{'cached_tokens':200}}})
            record('model','chat',{'id':'unknown','usage':{'prompt_tokens':1000,'completion_tokens':100}})
        self.price(100,'test-v2')
        with closing(connect(self.path)) as db,db:
            result=summarize(db,self.prices)
            self.assertAlmostEqual(result['currencies']['CNY'],.0022)
            self.assertEqual(result['unknown'],1)
            self.assertEqual(result['unknown_reasons'],{'unknown_cached_tokens':1})
            self.assertAlmostEqual(result['components']['CNY']['cached_input'],.0002)
            self.assertEqual(result['groups'][0]['username'],'alice')
            self.assertEqual(result['groups'][0]['job_id'],'job1')
            self.assertEqual(db.execute('SELECT price_version FROM cost_events WHERE amount IS NOT NULL').fetchone()[0],'test-v1')
    def test_no_price_never_zero_and_dedup(self):
        record('model','chat',{'id':'same','usage':{'input_tokens':10,'output_tokens':2}},path=self.path)
        record('model','chat',{'id':'same','usage':{'input_tokens':10,'output_tokens':2}},path=self.path)
        with closing(connect(self.path)) as db,db:
            result=summarize(db,self.prices)
            self.assertEqual(result['calls'],1);self.assertEqual(result['unknown'],1);self.assertEqual(result['currencies'],{})
            self.assertEqual(db.execute('SELECT calls FROM totals').fetchone()[0],1)
    def test_legacy_bounded_and_bad_config(self):
        self.prices.write_text('{broken')
        self.assertEqual(load_prices(self.prices)['state'],'invalid')
        with closing(connect(self.path)) as db,db:
            db.executemany('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)',[(str(i),i,'old','chat','ok',1,2,3,1) for i in range(3)])
            value=summarize(db,self.prices,limit=2)
            self.assertTrue(value['truncated']);self.assertEqual(value['unknown'],2)
    def test_historical_estimate_and_currencies(self):
        prices={'version':'manual','prices':[dict(provider='',model='old',kind='embedding',currency=currency,input_per_million=rate,cached_input_per_million=rate,output_per_million=0) for currency,rate in [('CNY',2)]]}
        prices['prices'].append(dict(provider='',model='new',kind='embedding',currency='USD',input_per_million=1,cached_input_per_million=1,output_per_million=0))
        self.prices.write_text(json.dumps(prices))
        with closing(connect(self.path)) as db,db:
            db.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)',('old',1,'old','embedding','ok',1000,0,1000,1))
        record('new','embedding',{'id':'usd','usage':{'input_tokens':1000}},path=self.path)
        with closing(connect(self.path)) as db,db:
            value=summarize(db,self.prices)
            self.assertEqual(value['historical_calls'],1)
            self.assertEqual(value['currencies'],{'USD':.001,'CNY':.002})
            self.assertEqual(value['unknown'],0)
            self.assertEqual(value['lifetime']['currencies']['USD'],{'minimum':.001,'maximum':.001})
    def test_invalid_prices_do_not_lose_usage(self):
        self.prices.write_text('{invalid')
        self.assertTrue(record('model','chat',{'usage':{'input_tokens':2,'output_tokens':1}},path=self.path))
        with closing(connect(self.path)) as db,db:
            self.assertEqual(summarize(db,self.prices)['unknown'],1)
            self.assertEqual(db.execute('SELECT total FROM totals').fetchone()[0],3)
    def test_automatic_attribution_preserves_user_context(self):
        with billed_to('alice',self.path,purpose='rewrite',job_id='job'):
            with attributed_call('prompt_generation','dashscope'):
                record('model','chat',{'id':'attribution','usage':{'input_tokens':2,'output_tokens':1}})
        with closing(connect(self.path)) as db,db:
            row=db.execute('SELECT username,provider,purpose,job_id FROM cost_events').fetchone()
            self.assertEqual(row,('alice','dashscope','rewrite','job'))
    def test_lifetime_cache_reference_range(self):
        self.price()
        value=json.loads(self.prices.read_text());value['prices'][0]['provider']='';self.prices.write_text(json.dumps(value))
        record('model','chat',{'id':'known','usage':{'input_tokens':1000,'output_tokens':100}},path=self.path)
        record('model','chat',None,status='network_error',path=self.path)
        with closing(connect(self.path)) as db,db:
            total=summarize(db,self.prices)['lifetime']
            self.assertAlmostEqual(total['currencies']['CNY']['minimum'],.0014)
            self.assertAlmostEqual(total['currencies']['CNY']['maximum'],.0024)
            self.assertEqual(total['unknown_usage'],1)
    def test_total_only_unknown(self):
        self.price()
        with billed_to('',self.path,provider='test'):
            record('model','chat',{'id':'total','usage':{'total_tokens':100,'cached_tokens':0}})
        with closing(connect(self.path)) as db,db:self.assertEqual(summarize(db,self.prices)['unknown'],1)

if __name__=='__main__':unittest.main()
