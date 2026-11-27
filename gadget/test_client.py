import io,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import motionfarm as cli
import motionfarm_mcp as mcp

class JournalIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,{'MOTIONFARM_OUTPUT_DIR':tmp.name});env.start();self.addCleanup(env.stop)

class Fake:
    def __init__(self):self.calls=[]
    def request(self,endpoint,value=None,binary=False):
        if endpoint=='identity':return dict(schema_version='gadget-account-v1',server_id='1'*32,account_id='2'*32,username='alice')
        self.calls.append((endpoint,value,binary))
        if binary:return b'numeric npz fixture'
        if endpoint.startswith('generate?'):return {'items':[dict(motion_id='m00000001',text='A person walks slowly forward with relaxed arms and separate foot tracks throughout the motion.')],'next_after':None}
        if endpoint.startswith('motion?'):return dict(motion_id='m00000001',text='A person walks slowly forward with relaxed arms and separate foot tracks throughout the motion.')
        return {'ok':True}
    def wait(self,*args):return {'job':{'status':'completed'}}
class ClientTests(JournalIsolatedTestCase):
    def test_generate_only_and_explicit_retarget(self):
        for workflow in ('generate','generate_retarget','retarget'):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);item=dict(id='sample',text='A person walks slowly forward with relaxed arms and separate foot tracks throughout the motion.',motion_id='m00000001')
                manifest=dict(workflow=workflow,robot='unitree_g1_23dof',items=[item]);p=root/'manifest.json';p.write_text(json.dumps(manifest));c=Fake()
                cli.run_manifest(c,p,root/'out',10)
                posts=[name for name,v,b in c.calls if v is not None]
                self.assertEqual('generate' in posts,workflow!='retarget');self.assertEqual('retarget' in posts,workflow!='generate')
                cli.run_manifest(c,p,root/'out',10)
                if workflow!='retarget':self.assertEqual([v['request_id'] for name,v,b in c.calls if name=='generate'][0],[v['request_id'] for name,v,b in c.calls if name=='generate'][1])
    def test_manifest_preserves_job_but_does_not_download_unavailable_retarget_output(self):
        client = Fake()
        client.wait = lambda *args: dict(status='done',export_verified=False,export_reason='重定向结果文件缺失')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = root/'manifest.json'
            output = root/'out'
            manifest.write_text(json.dumps(dict(workflow='retarget',robot='unitree_g1_23dof',
                items=[dict(id='sample',motion_id='m00000001')])))
            with self.assertRaisesRegex(RuntimeError,'重定向结果文件缺失'):
                cli.run_manifest(client,manifest,output,1)
            self.assertFalse(any(binary for _,_,binary in client.calls))
            saved = json.loads((output/'workflow-state.json').read_text())
            job = saved['items']['sample']['motions']['m00000001']['retarget_id']
            self.assertNotIn('completed',saved)
            client.wait = lambda *args: dict(status='done',export_verified=True)
            result = cli.run_manifest(client,manifest,output,1)
            self.assertTrue(result['completed'])
            self.assertEqual([body['id'] for endpoint,body,_ in client.calls if endpoint=='retarget'],[job,job])
            self.assertTrue((output/'sample/m00000001/reference.npz').is_file())
    def test_cli_explicit_integer_and_prompt_mode(self):
        c=Fake()
        with patch.object(cli,'Client',return_value=c),patch('sys.argv',['motionfarm.py','generate','--id','stable-id-1','--text','A person walks slowly forward with relaxed arms and separate foot tracks throughout the motion.','--duration','8','--prompt-mode','exact_english']),patch('sys.stdout',new=io.StringIO()):cli.main()
        self.assertIs(type(c.calls[0][1]['duration']),int);self.assertEqual(c.calls[0][1]['prompt_mode'],'exact_english')
    def test_mcp_protocol_and_json_only_stdout(self):
        messages=[dict(jsonrpc='2.0',id=1,method='initialize',params={'protocolVersion':'2025-11-25'}),dict(jsonrpc='2.0',method='notifications/initialized'),dict(jsonrpc='2.0',id=2,method='tools/list'),dict(jsonrpc='2.0',id=3,method='ping')]
        out=io.StringIO();mcp.serve(io.StringIO('\n'.join(map(json.dumps,messages))+'\n'),out)
        responses=[json.loads(x) for x in out.getvalue().splitlines()];self.assertEqual(len(responses),3)
        names=[tool['name'] for tool in responses[1]['result']['tools']]
        self.assertEqual(set(names),{'workflow_recovery','get_prompt_guidance','check_motion_prompt','motionfarm_info',
            'search_motions','list_tags','quality_report','get_motion','generate_motion','import_motion','retarget_motion',
            'get_job','cancel_job','download_artifact','motion_task_capabilities','list_motion_tasks','get_motion_task',
            'create_motion_task','motion_task_step','feedback_motion_task','cancel_motion_task','import_motion_patch'})
        self.assertEqual(len(names),len(set(names)))
    def test_mcp_generate_does_not_retarget(self):
        c=Fake();mcp.invoke('generate_motion',dict(request_id='stable-id-1',text='A person walks slowly forward with relaxed arms and separate foot tracks throughout the motion.',prompt_mode='exact_english'),c)
        self.assertEqual([x[0] for x in c.calls],['generate'])
        with self.assertRaises(ValueError):mcp.invoke('generate_motion',dict(request_id='stable-id-1',text='a',prompt_mode='exact_english',duration=True),c)
    def test_download_conflict_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'MOTIONFARM_OUTPUT_DIR':tmp}):
            c=Fake();result=mcp.invoke('download_artifact',dict(id='m00000001',kind='source'),c)
            self.assertTrue(Path(result['path']).is_file());Path(result['path']).write_bytes(b'other')
            with self.assertRaises(ValueError):mcp.invoke('download_artifact',dict(id='m00000001',kind='source'),c)
            with self.assertRaises(ValueError):mcp.invoke('download_artifact',dict(id='../outside',kind='source'),c)
    @unittest.skipUnless(cli.sys.platform in ('darwin','linux'),'native exclusive rename')
    def test_download_does_not_require_hard_links_on_current_platforms(self):
        import errno

        client = Fake()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'动作.npz'
            with patch('motionfarm.os.link',side_effect=OSError(errno.ENOTSUP,'hard links unavailable')) as link:
                cli.download(client,'m00000001',target,kind='source')
            self.assertEqual(target.read_bytes(),b'numeric npz fixture')
            self.assertEqual(list(target.parent.iterdir()),[target])
            link.assert_not_called()

    @unittest.skipUnless(cli.sys.platform in ('darwin','linux'),'native exclusive rename')
    def test_download_falls_back_to_exclusive_link_when_native_rename_is_unavailable(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'source.npz'
            with (patch('ctypes.CDLL',return_value=SimpleNamespace()),
                  patch('motionfarm.os.link',wraps=os.link) as link):
                cli.download(Fake(),'m00000001',target,kind='source')
            link.assert_called_once()
            self.assertEqual(target.read_bytes(),b'numeric npz fixture')
            self.assertEqual(list(target.parent.iterdir()),[target])

    def test_download_preserves_files_created_while_the_artifact_is_being_saved(self):
        client = Fake()
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder)/'source.npz'
            fsync = os.fsync

            def create_competing_file(descriptor):
                fsync(descriptor)
                target.write_bytes(b'written by another process')

            with patch('motionfarm.os.fsync',side_effect=create_competing_file):
                with self.assertRaisesRegex(ValueError,'different content'):
                    cli.download(client,'m00000001',target,kind='source')
            self.assertEqual(target.read_bytes(),b'written by another process')
            self.assertEqual(list(target.parent.iterdir()),[target])

            target.unlink()
            competing = []

            def create_identical_file(descriptor):
                fsync(descriptor)
                target.write_bytes(b'numeric npz fixture')
                competing.append(target.stat())

            with patch('motionfarm.os.fsync',side_effect=create_identical_file):
                artifact = cli.download(client,'m00000001',target,kind='source')
            self.assertEqual(len(competing),1)
            self.assertEqual(cli.download(client,'m00000001',target,kind='source'),artifact)
            self.assertTrue(os.path.samestat(competing[0],target.stat()))
            self.assertEqual(target.read_bytes(),b'numeric npz fixture')

    def test_source_download_verifies_the_response_before_creating_a_file(self):
        import hashlib
        from types import SimpleNamespace

        complete = b'valid source bytes'
        digest = hashlib.sha256(complete).hexdigest()
        client = cli.Client.__new__(cli.Client)
        client.url = 'https://fixture.invalid'
        client.key = 'fixture-key'

        def respond(data, *, status=200, length=len(complete), sha=digest):
            stream = io.BytesIO(data)
            stream.status = status
            stream.headers = {'Content-Length':str(length),'X-MotionFarm-Content-SHA256':sha}
            client.opener = SimpleNamespace(open=lambda *args,**kwargs: stream)

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'source.npz'
            for data, status, message in (
                (b'x'*len(complete),200,'checksum'),
                (complete[:-2],200,'incomplete'),
                (complete,206,'incomplete'),
            ):
                with self.subTest(status=status,message=message):
                    respond(data,status=status)
                    with self.assertRaisesRegex(ValueError,message):
                        cli.download(client,'mimp'+'7'*32,path,kind='source')
                    self.assertFalse(path.exists())
            respond(complete)
            self.assertEqual(cli.download(client,'mimp'+'7'*32,path,kind='source')['sha256'],digest)
            self.assertEqual(path.read_bytes(),complete)
            respond(b'x'*len(complete))
            with self.assertRaisesRegex(ValueError,'checksum'):
                cli.download(client,'mimp'+'7'*32,path,kind='source')
            self.assertEqual(path.read_bytes(),complete)
    def test_tool_failure_reports_retry_id(self):
        messages=[dict(jsonrpc='2.0',id=1,method='initialize'),dict(jsonrpc='2.0',method='notifications/initialized'),dict(jsonrpc='2.0',id=2,method='tools/call',params={'name':'generate_motion','arguments':{'request_id':'stable-id-1'}})]
        out=io.StringIO();mcp.serve(io.StringIO('\n'.join(map(json.dumps,messages))+'\n'),out)
        response=json.loads(out.getvalue().splitlines()[-1]);self.assertTrue(response['result']['isError']);self.assertIn('stable-id-1',response['result']['content'][0]['text'])

class TagClientTests(JournalIsolatedTestCase):
    def test_tag_tools_and_forwarding(self):
        import motionfarm_mcp as bridge
        from unittest.mock import Mock
        from urllib.parse import urlsplit,parse_qs
        client=Mock();client.request.return_value={'items':[]}
        bridge.invoke('search_motions',dict(tag_group='动作类型',tag='行走',tagged_by='alice'),client)
        query=parse_qs(urlsplit(client.request.call_args.args[0]).query)
        self.assertEqual(query['tag'],['行走']);self.assertEqual(query['tag_group'],['动作类型']);self.assertEqual(query['tagged_by'],['alice'])
        bridge.invoke('list_tags',dict(limit=1,after=2),client)
        self.assertEqual(client.request.call_args.args[0],'tags?limit=1&after=2')

class LocalRewriteTests(JournalIsolatedTestCase):
    text='A person walks slowly forward with relaxed arms and separate foot tracks throughout the motion.'
    def test_default_and_provenance(self):
        c=Fake();mcp.invoke('generate_motion',dict(request_id='stable-id-1',text=self.text,original_description='向前走',rewrite_agent={'name':'Codex','model':'selected-model'}),c)
        self.assertEqual(c.calls[0][1]['prompt_mode'],'exact_english')
        self.assertEqual(c.calls[0][1]['original_description'],'向前走')
        self.assertEqual(c.calls[0][1]['rewrite_agent']['name'],'Codex')
        with patch.object(cli,'Client',return_value=c),patch('sys.argv',['motionfarm.py','generate','--id','stable-id-2','--text',self.text]),patch('sys.stdout',new=io.StringIO()):cli.main()
        self.assertEqual(c.calls[-1][1]['prompt_mode'],'exact_english')
    def test_translate_rejected_before_request(self):
        c=Fake()
        with self.assertRaisesRegex(ValueError,'local-agent rewriting'):mcp.invoke('generate_motion',dict(request_id='stable-id-1',text=self.text,prompt_mode='translate'),c)
        with patch.object(cli,'Client',return_value=c),patch('sys.argv',['motionfarm.py','generate','--id','stable-id-2','--text',self.text,'--prompt-mode','translate']):
            with self.assertRaisesRegex(ValueError,'local-agent rewriting'):cli.main()
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'manifest.json';p.write_text(json.dumps({'prompt_mode':'translate','items':[{'id':'sample','text':self.text}]}))
            with self.assertRaisesRegex(ValueError,'local-agent rewriting'):cli.run_manifest(c,p,Path(tmp)/'out',10)
            self.assertFalse((Path(tmp)/'out').exists())
        self.assertEqual(c.calls,[])
    def test_bad_text_and_bounded_self_report(self):
        for extra in ({'rewrite_agent':{'name':'x'*121}},{'rewrite_agent':{'api_key':'secret'}},{'rewrite_agent':[]},{'rewrite_agent':{}},{'original_description':'x'*2001}):
            with self.subTest(extra=extra):
                c=Fake()
                with self.assertRaises(ValueError):mcp.invoke('generate_motion',dict(request_id='stable-id-1',text=self.text,**extra),c)
                self.assertEqual(c.calls,[])
        for text in ('向前走','A person walks forward.','A person '+'walks '*70):
            with self.assertRaisesRegex(ValueError,'local-agent rewriting'):cli.generation_payload('stable-id-1',text)
    def test_manifest_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'manifest.json';p.write_text(json.dumps({'rewrite_agent':{'name':'Cursor'},'items':[{'id':'sample','text':self.text,'original_description':'向前走'}]}))
            c=Fake();cli.run_manifest(c,p,Path(tmp)/'out',10)
            post=next(v for endpoint,v,_ in c.calls if endpoint=='generate')
            self.assertEqual(post['prompt_mode'],'exact_english');self.assertEqual(post['rewrite_agent'],{'name':'Cursor'})
            saved=json.loads((Path(tmp)/'out/sample/m00000001/metadata.json').read_text())
            self.assertEqual(saved['original_description'],'向前走')

class RecoveryV04Tests(JournalIsolatedTestCase):
    def test_manifest_replay_parameters_and_quality_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'manifest.json'
            item=dict(id='sample',text=LocalRewriteTests.text,seed=0,parent_request_id='parent-01',parameters={'smooth_weight':1},parent_job_id='d'*32)
            p.write_text(json.dumps(dict(workflow='generate_retarget',robot='unitree_g1_23dof',items=[item])))
            c=Fake();cli.run_manifest(c,p,Path(tmp)/'out',10)
            generate=next(v for e,v,_ in c.calls if e=='generate');retarget=next(v for e,v,_ in c.calls if e=='retarget')
            self.assertEqual(generate['seed'],0);self.assertEqual(generate['parent_request_id'],'parent-01')
            self.assertEqual(retarget['parameters'],{'smooth_weight':1});self.assertEqual(retarget['parent_job_id'],'d'*32)
            self.assertTrue((Path(tmp)/'out/sample/m00000001/quality.json').is_file())

    def test_quality_and_combined_filters(self):
        from urllib.parse import urlsplit,parse_qs
        c=Fake();mcp.invoke('quality_report',dict(kind='retarget',id='a'*32),c)
        self.assertEqual(c.calls[-1][0],'retarget_report?id='+'a'*32)
        filters=dict(author='alice',robot='unitree_g1_23dof',duration_min=1,duration_max=3,quality='incomplete',collection='favorites',tag='walk')
        mcp.invoke('search_motions',filters,c);q=parse_qs(urlsplit(c.calls[-1][0]).query)
        for k,v in filters.items():self.assertEqual(q[k],[str(v)])
    def test_replay_and_version_forwarding(self):
        c=Fake();mcp.invoke('generate_motion',dict(request_id='replay-001',text='A person stands still with both feet flat on the ground and relaxed arms.',prompt_mode='exact_english',seed=0,parent_request_id='parent-01'),c)
        self.assertEqual(c.calls[-1][1]['seed'],0);self.assertEqual(c.calls[-1][1]['parent_request_id'],'parent-01')
        args=dict(id='b'*32,motion_id='m00000001',robot='unitree_g1_23dof',preset_id='c'*32,preset_version=2,parent_job_id='d'*32)
        mcp.invoke('retarget_motion',args,c);self.assertEqual(c.calls[-1][1],args)
        with self.assertRaises(ValueError):mcp.invoke('retarget_motion',dict(args,preset_version=True),c)

if __name__=='__main__':unittest.main()
