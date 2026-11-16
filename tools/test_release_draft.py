import hashlib
from pathlib import Path
import tempfile
import unittest

from verify_release_draft import verify


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        data = b'approved archive bytes'
        (self.directory / 'kinora.zip').write_bytes(data)
        self.asset = {
            'name': 'kinora.zip',
            'size': len(data),
            'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
            'state': 'uploaded',
        }

    def test_exact_draft(self):
        verify(self.directory, [self.asset])

    def test_missing_extra_duplicate_modified_and_incomplete_assets(self):
        for assets in (
            [],
            [self.asset, {**self.asset, 'name': 'manual.zip'}],
            [self.asset, self.asset],
            [{**self.asset, 'digest': 'sha256:' + '0' * 64}],
            [{**self.asset, 'state': 'starter'}],
        ):
            with self.subTest(assets=assets), self.assertRaises(ValueError):
                verify(self.directory, assets)
