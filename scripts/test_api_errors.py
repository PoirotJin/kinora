import io
import json
import sys
from pathlib import Path
import unittest
from urllib.error import HTTPError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from g2h.api_errors import TemporaryAPIError, http_failure


class APIFailureTests(unittest.TestCase):
    def failure(self,status,code=''):
        return http_failure(HTTPError('https://example.invalid',status,'error',{},
            io.BytesIO(json.dumps({'error':{'code':code}}).encode())), 'Qwen')

    def test_rate_and_service_failures_retry(self):
        for status in (408,429,500,502,503,504):
            self.assertIsInstance(self.failure(status),TemporaryAPIError)

    def test_auth_balance_and_config_fail_closed(self):
        for status,code in ((401,''),(403,''),(400,''),(404,''),
                (429,'insufficient_quota'),(429,'Arrearage'),(429,'AllocationQuota.Exceeded')):
            self.assertNotIsInstance(self.failure(status,code),TemporaryAPIError)

    def test_provider_body_not_exposed(self):
        self.assertNotIn('secret',str(self.failure(401,'secret')))
