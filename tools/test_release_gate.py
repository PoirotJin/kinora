import gzip
import shutil
import subprocess
import io
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from package_server import build
from release_gate import GateError, inspect


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {'KINORA_BLOCKED_IPS': '192.0.2.77,198.51.100.77'}))

    def zip_payload(self, name, payload):
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name, payload)
        return output.getvalue()

    def test_nested_archives_check_the_decoded_bytes(self):
        address = b'192.0.2.77'
        for encoding in ('ascii', 'utf-16le', 'utf-16be'):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'source.zip'
                text = (address.decode() + '.').encode(encoding)
                inner = self.zip_payload('settings.txt.gz', gzip.compress(text))
                path.write_bytes(self.zip_payload('nested.zip', inner))
                with self.assertRaisesRegex(GateError, 'protected IPv4'):
                    inspect(path)

    @unittest.skipUnless(shutil.which('zstd'), 'requires Zstandard')
    def test_nested_zstandard_inspects_contents_and_refuses_corrupt_stream(self):
        payload = self.zip_payload('settings.env', b'192.0.2.77\nQWEN_API_KEY=sk-' + b'X' * 32)
        archive = subprocess.check_output(['zstd', '-q', '-c'], input=payload)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'source.zip'
            for name in ('settings.zip.zst', 'extensionless'):
                path.write_bytes(self.zip_payload(name, archive))
                with self.assertRaisesRegex(GateError, 'protected IPv4'):
                    inspect(path)
            path.write_bytes(self.zip_payload('payload.zst', archive[:8]))
            with self.assertRaisesRegex(GateError, 'Zstandard'):
                inspect(path)
            path.write_bytes(self.zip_payload('old-backup.rar', b'unsupported archive'))
            with self.assertRaisesRegex(GateError, 'unrecognized package'):
                inspect(path)

    def test_credential_reports_do_not_include_values(self):
        samples = [
            b'sk-' + b'a' * 24,
            b'mfg_' + b'Z9' * 21,
            b'QWEN_API_KEY="someRandomCredentialValue123"',
            ('GADGET_TOKEN=someRandomCredentialValue123').encode('utf-16le'),
        ]
        for payload in samples:
            with self.subTest(length=len(payload)), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'source.zip'
                path.write_bytes(self.zip_payload('config.txt', payload))
                with self.assertRaisesRegex(GateError, 'credential') as error:
                    inspect(path)
                self.assertNotIn(payload.decode('ascii'), str(error.exception))

    def test_examples_and_environment_references_pass(self):
        payload = (b'url=https://your-kinora.example.com\n'
                   b'MOTIONFARM_API_KEY_FILE=/home/me/api-key.txt\n'
                   b'QWEN_API_KEY="your-example-api-key"\n'
                   b'QWEN_API_KEY=os.environ.get("QWEN_API_KEY")\n')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'source.zip'
            path.write_bytes(self.zip_payload('README.md', payload))
            inspect(path)

    def test_blocked_source_does_not_replace_existing_output(self):
        address = b'192.0.2.77'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'source'
            source.mkdir()
            (source / 'VERSION').write_text('1.0.0')
            (source / 'assets.lock.json').write_text('{"files":[]}')
            (source / 'README.md').write_bytes(address)
            output = root / 'kinora-server-1.0.0.zip'
            output.write_bytes(b'previous file')
            with self.assertRaisesRegex(GateError, 'protected IPv4'):
                build(source, output)
            self.assertEqual(output.read_bytes(), b'previous file')

    def test_missing_configuration_blocks_publication(self):
        with patch.dict(os.environ, {'KINORA_BLOCKED_IPS': ''}):
            with self.assertRaisesRegex(GateError, 'configuration is missing'):
                inspect(Path('source.zip'))

    def test_unreadable_installers_do_not_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'kinora_1.0.0_universal.dmg'
            path.write_bytes(b'invalid')
            with self.assertRaisesRegex(GateError, 'invalid DMG'):
                inspect(path)


if __name__ == '__main__':
    unittest.main()
