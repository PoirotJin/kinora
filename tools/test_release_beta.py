import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from prepare_macos_beta import prepare
from release_gate import GateError
from release_record import sign, signature_valid, MANIFEST, SIGNATURE


class BetaTests(unittest.TestCase):
    def test_beta_candidate_whitelist_and_version_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / 'candidate.zip'
            with zipfile.ZipFile(candidate, 'w') as archive:
                archive.writestr('unexpected.txt', 'extra source material')
            checksum = hashlib.sha256(candidate.read_bytes()).hexdigest()
            for version, digest, error in (
                ('0.11.0', checksum, 'beta version'),
                ('0.11.0-beta', '0' * 64, 'checksum'),
                ('0.11.0-beta', checksum, 'contents'),
            ):
                with self.subTest(error=error), self.assertRaisesRegex(GateError, error):
                    prepare(version, candidate, digest, root / 'dist', root / 'notes')
            self.assertFalse((root / 'dist').exists())

    def test_both_slices_required_and_beta_has_no_update_feed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / 'candidate.zip'
            notes = root / 'notes'
            notes.write_text('Enter your own team server address.')
            dmg = 'kinora_0.11.0-beta_universal.dmg'
            with zipfile.ZipFile(candidate, 'w') as archive:
                archive.writestr(dmg, b'test payload')
                archive.writestr('renderer-assets.json', json.dumps(['/index.html', '/app.js']))
            checksum = hashlib.sha256(candidate.read_bytes()).hexdigest()
            with patch('prepare_macos_beta.Scanner') as scanner, patch('prepare_macos_beta.inspect'):
                scanner.return_value.desktop_slices = 1
                with self.assertRaisesRegex(GateError, 'both universal'):
                    prepare('0.11.0-beta', candidate, checksum, root / 'dist', notes)
                scanner.return_value.desktop_slices = 2
                prepare('0.11.0-beta', candidate, checksum, root / 'dist', notes)
            self.assertEqual({p.name for p in (root / 'dist').iterdir()},
                             {dmg, 'INSTALL.txt', 'SHA256SUMS.txt'})

    def test_beta_gate_record_retains_signature_and_version_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private, public = root / 'private.pem', root / 'public.pem'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(private)], check=True)
            subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(public)], check=True)
            dist = root / 'dist'
            dist.mkdir()
            (dist / 'installer.dmg').write_bytes(b'test installer')
            sign(dist, 'PoirotJin/kinora', '0.11.0-beta', 42, 'a' * 40, private.read_text(), public)
            record, signature = (dist / MANIFEST).read_bytes(), (dist / SIGNATURE).read_bytes()
            self.assertEqual(json.loads(record)['tag'], 'kinora-v0.11.0-beta')
            self.assertTrue(signature_valid(record, signature, public))
            self.assertFalse(signature_valid(record.replace(b'0.11.0-beta', b'0.12.0-beta'), signature, public))
