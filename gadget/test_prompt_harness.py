import io
import json
from pathlib import Path
import re
import unittest
from unittest.mock import patch

import motionfarm
import motionfarm_mcp as mcp
from prompt_harness import check_motion_prompt, prompt_guidance

TEXT = 'A person walks slowly forward on separate foot tracks with relaxed arms clear of the torso.'

class PromptHarnessTests(unittest.TestCase):
    def test_offline_tools_do_not_construct_client(self):
        with patch.object(mcp, 'Client', side_effect=AssertionError('No network credentials needed')):
            guide=mcp.invoke('get_prompt_guidance', {})
            self.assertIn('intentional crossing', guide['guide'])
            self.assertEqual(len(guide['guide_sha256']),64)
            result=mcp.invoke('check_motion_prompt',dict(text=TEXT))
            self.assertTrue(result['format_valid'])
            self.assertTrue(result['semantic_review_required'])
            self.assertEqual(result['motion_quality'],'unverified')

    def test_new_host_receives_full_policy_at_initialize(self):
        out=io.StringIO()
        with patch.object(mcp, 'Client', side_effect=AssertionError('No network')):
            mcp.serve(io.StringIO(json.dumps(dict(jsonrpc='2.0',id=1,method='initialize'))+'\n'),out)
        response=json.loads(out.getvalue())['result']
        self.assertIn(prompt_guidance()['guide'],response['instructions'])
        self.assertEqual(response['serverInfo']['version'],'0.9.1')

    def test_invalid_formats_cannot_submit(self):
        for text in ('A person walks.',TEXT+'\n',TEXT+'中文',TEXT+' русский',TEXT+' 日本語','A person '+'word '*69,'A person '+('x'*100+' ')*12):
            with self.subTest(text=text):
                self.assertFalse(check_motion_prompt(text)['format_valid'])
                with self.assertRaises(ValueError):motionfarm.generation_payload('stable-id-001',text)
        for duration in (True,0,121,1.5):
            self.assertFalse(check_motion_prompt(TEXT,duration)['format_valid'])
        for duration in (1,13,120):
            self.assertTrue(check_motion_prompt(TEXT,duration)['format_valid'])
        self.assertTrue(check_motion_prompt(TEXT.replace('arms','“arms”'))['format_valid'])
        for name, field in (('create_motion_task', 'goal'), ('feedback_motion_task', 'message')):
            tool_def = next(t for t in mcp.TOOLS if t['name'] == name)
            self.assertEqual(tool_def['inputSchema']['properties'][field]['maxLength'], 4000)
            req = {'request_id': 'stable-id-001'} if name == 'create_motion_task' else {'task_id': 'task_001'}
            mcp.validate(tool_def['inputSchema'], {**req, field: 'x' * 4000})
            with self.assertRaises(ValueError):
                mcp.validate(tool_def['inputSchema'], {**req, field: 'x' * 4001})

    def test_cli_works_without_api_key(self):
        with patch.object(motionfarm,'Client',side_effect=AssertionError('No network')),patch('sys.stdout',new=io.StringIO()) as out:
            with patch('sys.argv',['motionfarm.py','prompt-guide']):motionfarm.main()
            self.assertIn('guide_sha256',out.getvalue())
            with patch('sys.argv',['motionfarm.py','check-prompt','--text','bad']):
                with self.assertRaises(SystemExit) as result:motionfarm.main()
            self.assertEqual(result.exception.code,2)

    def test_bundled_examples_are_valid(self):
        examples=re.findall(r'^  Prompt: (.+)$',prompt_guidance()['guide'],re.M)
        self.assertEqual(len(examples),5)
        for text in examples:self.assertTrue(check_motion_prompt(text)['format_valid'],text)

    def test_policy_snapshot_matches_repository(self):
        source=Path(__file__).resolve().parents[1]/'AGENTS.md'
        if not source.exists() or '<!-- MOTION_GENERATION_POLICY_BEGIN -->' not in source.read_text():
            self.skipTest('Source repository is not part of the standalone download')
        policy=source.read_text().split('<!-- MOTION_GENERATION_POLICY_BEGIN -->')[1].split('<!-- MOTION_GENERATION_POLICY_END -->')[0].strip()
        self.assertIn(policy,prompt_guidance()['guide'])

if __name__=='__main__':unittest.main()
