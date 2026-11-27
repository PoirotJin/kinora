import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import motionfarm_mcp as mcp
from workflow_journal import Journal

TEXT='A person walks slowly forward with relaxed arms and separate foot tracks throughout the motion.'
class Client:
    url='https://example.invalid'
    key='private-api-key'
    def __init__(self,fail=False):self.fail=fail;self.calls=[]
    def request(self,endpoint,value=None,binary=False):
        if endpoint=='identity':return dict(schema_version='gadget-account-v1',server_id='1'*32,account_id='2'*32,username='alice')
        self.calls.append(endpoint)
        if self.fail:raise TimeoutError('secret server failure')
        if binary:return b'artifact'
        return {'job':{'status':'completed'},'checks':{'balance':'unknown'}}

class JournalTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        env=patch.dict(os.environ,{'MOTIONFARM_OUTPUT_DIR':tmp.name});env.start();self.addCleanup(env.stop)
    def test_uncertain_post_is_durable_and_recovery_never_posts(self):
        c=Client(True);args=dict(request_id='stable-001',text=TEXT)
        with self.assertRaises(TimeoutError):mcp.invoke('generate_motion',args,c)
        recovered=mcp.invoke('workflow_recovery',{},Client())['records'][0]
        self.assertEqual(recovered['submission'],'uncertain')
        self.assertEqual(recovered['id'],'stable-001')
        raw=next(Journal(c).root.glob('*.json')).read_text()
        for secret in (TEXT,c.key,'secret server failure'):self.assertNotIn(secret,raw)
        c=Client();mcp.invoke('get_job',dict(kind='generate',id='stable-001'),c)
        mcp.invoke('generate_motion',args,c)
        self.assertEqual(c.calls,['generate?id=stable-001&after=0','generate'])
        with self.assertRaisesRegex(ValueError,'different arguments'):mcp.invoke('generate_motion',dict(args,n=2),c)
    def test_files_quality_and_status_survive_new_client(self):
        c=Client();args=dict(kind='retarget',id='a'*32)
        mcp.invoke('get_job',args,c);mcp.invoke('download_artifact',args,c);mcp.invoke('quality_report',args,c)
        record=mcp.invoke('workflow_recovery',args,Client())['record']
        self.assertEqual(record['server_status'],'completed');self.assertTrue(record['artifact']['sha256'])
        self.assertTrue(record['quality_report_fetched']);self.assertIn('unverified',record['quality_state'])
    def test_account_isolation_and_cursor(self):
        c=Client();j=Journal(c)
        for i in range(3):j.update('generate',str(i),server_status='unknown')
        page=j.recover(limit=2);other=j.recover(limit=2,after=page['next_after'])
        self.assertEqual(len(page['records'])+len(other['records']),3)
        c.key='another-private-key';self.assertEqual(Journal(c).recover()['records'],[])
    def test_symlinks_and_oversize_fail_closed(self):
        j=Journal(Client());target=self.root/'outside';target.write_text('original')
        path=j.path('generate','x');path.symlink_to(target)
        with self.assertRaises(ValueError):j.update('generate','x')
        self.assertEqual(target.read_text(),'original');path.unlink();path.write_text('x'*65537)
        with self.assertRaises(ValueError):j.read('generate','x')
    def test_submit_written_before_send_and_locked(self):
        j=Journal(Client())
        def send():
            self.assertEqual(j.read('generate','x')['submission'],'uncertain')
            with self.assertRaises((BlockingIOError,OSError)):
                with j.lock('generate','x'):pass
            return {'ok':True}
        j.submit('generate','x',{'n':1},send)
        self.assertEqual(j.read('generate','x')['submission'],'acknowledged')

if __name__=='__main__':unittest.main()
