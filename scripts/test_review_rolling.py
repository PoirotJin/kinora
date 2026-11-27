"""Rolling boundaries, persistence, unknowns and real HTTP role isolation."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
from types import SimpleNamespace
from review_rolling import RollingMonitor, WINDOW, RETENTION
from g2h.observability import initialize
from g2h.api_usage import connect

class RollingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.now=200000.
        initialize(self.root)
        with sqlite3.connect(self.root/'review/telemetry.sqlite3') as db:db.execute("UPDATE meta SET value='1' WHERE key='started_at'")
        with closing(connect(self.root/'review/api_usage.sqlite3')):pass
        with sqlite3.connect(self.root/'review/reviews.sqlite3') as db:db.execute('CREATE TABLE events(event_id INTEGER PRIMARY KEY,motion_id TEXT,revision INTEGER,body TEXT)')
        with sqlite3.connect(self.root/'review/agent.sqlite3') as db:db.execute('CREATE TABLE decisions(ts REAL,verdict TEXT)')
        self.monitor=RollingMonitor(self.root)
    def observe(self,now=None,ready=True):
        now=self.now if now is None else now
        with patch('time.time',return_value=now):
            self.monitor.observe(dict(ready=ready,sampled_at=now,cpu=20,memory={'percent':30},gpu={'items':[{'load':40,'used_mb':50}]},network=[{'rx_bps':100,'tx_bps':200}],topics=[],alerts=[]))
        return self.monitor.get()
    def motion(self,identity,ts):
        with sqlite3.connect(self.root/'review/telemetry.sqlite3') as db:db.execute('INSERT INTO motions VALUES(?,?,?,?,?,?,?,?,?)',(identity,ts,2,'walking','production',100,60,30,'job'))
    def test_exact_event_window_hourly_and_advancing(self):
        start=self.now-WINDOW
        for i,ts in enumerate([start-1,start,start+1,self.now-1,self.now,self.now+1]):self.motion(str(i),ts)
        a=self.observe();self.assertEqual(a['production']['motions'],3)
        self.assertEqual(sum(h['motions'] for h in a['production']['hourly']),3)
        self.assertEqual(len(a['production']['hourly']),24);self.assertEqual(a['production']['hourly'][0]['motions'],2)
        self.assertEqual(a['campaign']['ends_at']-a['campaign']['started_at'],WINDOW)
        b=self.observe(self.now+60);self.assertEqual(b['production']['motions'],3)
        self.assertEqual(b['campaign']['started_at'],start+60);self.assertEqual(b['status'],'rolling');self.assertIsNone(b['finalized_at'])
    def test_cache_restart_gap_and_no_synthetic_history(self):
        a=self.observe();self.assertEqual(a['coverage']['samples'],1);self.assertEqual(a['coverage']['covered_seconds'],0)
        self.assertIsNone(a['resources']['disk']['growth_bytes']);self.assertIs(self.observe(self.now+10),a)
        self.observe(self.now+60);restarted=RollingMonitor(self.root);self.assertEqual(restarted.get(),self.monitor.get());self.monitor=restarted
        b=self.observe(self.now+600);self.assertEqual(b['coverage']['samples'],3);self.assertEqual(b['coverage']['covered_seconds'],120)
        self.assertGreaterEqual(b['coverage']['gap_seconds'],WINDOW-120);self.assertEqual(len(b['resources']['points']),3)
        self.assertGreater(b['coverage']['max_gap_s'],180)
        with patch.object(self.monitor,'_connect',side_effect=AssertionError('HTTP reader must not query')):self.assertEqual(self.monitor.get(),b)
    def test_stale_and_unready_samples_are_unknown(self):
        a=self.observe(ready=False);self.assertEqual(a['coverage']['samples'],0);self.assertIsNone(a['resources']['cpu_percent'])
        self.assertEqual(a['coverage']['gap_seconds'],WINDOW)
        with patch('time.time',return_value=self.now+60):self.monitor.observe({'ready':True,'sampled_at':self.now-1000,'cpu':0})
        self.assertEqual(self.monitor.get()['coverage']['samples'],0)
    def test_window_clips_coverage_and_prunes_storage(self):
        self.observe()
        with closing(self.monitor._connect()) as db,db:
            raw=db.execute('SELECT body FROM host_samples LIMIT 1').fetchone()[0]
            for ts in [self.now-RETENTION-1,self.now-WINDOW+30,self.now-WINDOW-1]:
                body=json.loads(raw);body['ts']=ts
                db.execute('INSERT INTO host_samples VALUES(?,?,?)',(ts,60,json.dumps(body)))
        result=self.observe(self.now+60);self.assertEqual(result['coverage']['samples'],2);self.assertEqual(result['coverage']['covered_seconds'],60)
        with closing(self.monitor._connect()) as db:self.assertIsNone(db.execute('SELECT ts FROM host_samples WHERE ts<?',(self.now+60-RETENTION,)).fetchone())
        self.monitor.updated=None
        with closing(self.monitor._connect()) as db,db:
            ts=self.now+60-WINDOW+30;body=json.loads(raw);body['ts']=ts
            db.execute('INSERT INTO host_samples VALUES(?,?,?)',(ts,60,json.dumps(body)))
        self.assertEqual(self.observe(self.now+60)['coverage']['covered_seconds'],90)
    def test_failed_publish_preserves_previous_snapshot(self):
        before=self.observe()
        with patch.object(self.monitor,'_build',side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):self.observe(self.now+60)
        self.assertIs(self.monitor.get(),before);self.assertEqual(self.observe(self.now+70)['sampled_at'],self.now+70)
    def test_late_events_recount_old_campaign_untouched(self):
        historical=self.root/'review/acceptance.sqlite3';historical.write_bytes(b'untouched historical report')
        self.observe();self.motion('late',self.now-10)
        self.assertEqual(self.observe(self.now+60)['production']['motions'],1)
        self.assertEqual(historical.read_bytes(),b'untouched historical report');self.assertFalse((self.root/'review/acceptance_reports').exists())
        self.assertIn('最近 24 小时监控',self.monitor.export_markdown());self.assertNotIn('冻结报告',self.monitor.export_markdown())
    def test_costs_all_calls_and_currencies(self):
        with closing(connect(self.root/'review/api_usage.sqlite3')) as db,db:
            db.executemany('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?)',[(str(i),self.now-1,'model','chat','ok',1,2,3,1) for i in range(20001)])
        a=self.observe()['api'];self.assertEqual(a['calls'],20001);self.assertEqual(a['total_tokens'],60003)
        self.assertEqual(a['unknown_cost_calls'],20001);self.assertIsNone(a['cost_cny']);self.assertEqual(a['currencies'],{})
        with closing(connect(self.root/'review/api_usage.sqlite3')) as db,db:
            cols=[r[1] for r in db.execute('PRAGMA table_info(cost_events)')];defaults={k:'' for k in cols}
            for identity,currency,amount in [('0','CNY',1.2),('1','USD',.3)]:
                values=dict(defaults,event_id=identity,currency=currency,amount=amount,state='configured_estimate',cached=0,details='{}')
                db.execute('INSERT INTO cost_events('+','.join(cols)+') VALUES('+','.join('?' for _ in cols)+')',tuple(values[k] for k in cols))
        b=self.observe(self.now+60)['api'];self.assertEqual(b['currencies'],{'CNY':1.2,'USD':.3});self.assertEqual(b['unknown_cost_calls'],19999);self.assertIsNone(b['cost_cny'])
    def test_missing_event_database_keeps_unknown(self):
        (self.root/'review/telemetry.sqlite3').unlink();result=self.observe()
        self.assertFalse(result['production']['available']);self.assertIsNone(result['production']['motions']);self.assertIsNone(result['reviews']['pass_rate'])
    def test_review_cohort_and_cutoff(self):
        self.motion('inside',self.now-1);self.motion('outside',self.now-WINDOW-1)
        from review_acceptance import iso
        with sqlite3.connect(self.root/'review/reviews.sqlite3') as db:
            for i,mid,status,ts in [(1,'inside','accepted',self.now-.5),(2,'inside','rejected',self.now+1),(3,'outside','accepted',self.now-1)]:
                db.execute('INSERT INTO events VALUES(?,?,?,?)',(i,mid,i,json.dumps(dict(status=status,updated_at=iso(ts)))))
        result=self.observe()['reviews'];self.assertEqual(result['cohort'],1);self.assertEqual(result['accepted'],1);self.assertEqual(result['pass_rate'],1)

class RollingHTTPTests(unittest.TestCase):
    def test_roles_snapshot_and_report(self):
        from review_server import Server,Handler
        from review_auth import Auth
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);auth=Auth(root/'review');tokens={}
            for name,role in [('admin','admin'),('reader','user')]:auth.create(name,'local-test-password-123',role=role)
            with closing(auth.connect()) as db,db:db.execute('UPDATE users SET must_change=0')
            for name in ('admin','reader'):tokens[name]=auth.login(name,'local-test-password-123','local-test')[0]
            rolling=RollingMonitor(root)
            with patch('time.time',return_value=200000):rolling.observe({})
            server=Server(('127.0.0.1',0),Handler);server.auth=auth
            server.monitor=SimpleNamespace(rolling=rolling,get=lambda:{'ready':True,'rolling_24h':rolling.get()})
            server.store=SimpleNamespace(root=root);server.origins=set()
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                for path in ['/api/monitor','/api/monitor/24h','/api/monitor/24h/report.md']:
                    for name,expected in [(None,401),('reader',403),('admin',200)]:
                        req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+path,headers={'Cookie':'mf_session='+tokens[name]} if name else {})
                        try:
                            with urllib.request.urlopen(req) as response:code=response.status;body=response.read()
                        except urllib.error.HTTPError as exc:code=exc.code;body=exc.read()
                        self.assertEqual(code,expected,(path,name,body))
                        if name=='admin':self.assertIn('最近 24 小时'.encode() if path.endswith('.md') else b'rolling_24h',body)
            finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
