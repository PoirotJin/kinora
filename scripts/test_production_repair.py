import io,json,tempfile,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from g2h.api_errors import http_failure,ContentRejectedError
import qwen_motion_agent as agent
import review_payload_backup as payload

class RepairTests(unittest.TestCase):
 def test_only_known_content_rejection_is_skippable(self):
  for status,code,expected in [(400,'data_inspection_failed',True),(400,'InvalidParameter',False),(401,'data_inspection_failed',False),(400,'secret',False)]:
   error=urllib.error.HTTPError('https://example.invalid',status,'error',{},io.BytesIO(json.dumps({'error':{'code':code,'message':'private'}}).encode()))
   result=http_failure(error,'Qwen');self.assertEqual(isinstance(result,ContentRejectedError),expected);self.assertNotIn('private',str(result));self.assertNotIn('secret',str(result))
 def test_rejected_checkpoint_never_calls_provider(self):
  with tempfile.TemporaryDirectory() as d:
   folder=Path(d);(folder/'generate_rejected.json').write_text('{}')
   with patch.object(agent.farm,'call_qwen',side_effect=AssertionError('must not retry')):
    with self.assertRaises(ContentRejectedError):agent.request(SimpleNamespace(),folder,'generate','',{},0.9)
 def test_rotated_status_reports_keep_group_read(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'latest.json'
   payload.atomic_json(p,{'complete':True});payload.atomic_json(p,{'complete':False})
   self.assertEqual(p.stat().st_mode&0o777,0o640)
   q=Path(d)/'private.json';payload.atomic_json(q,{});self.assertEqual(q.stat().st_mode&0o777,0o600)
if __name__=='__main__':unittest.main()
