import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import reconcile_releases as guard
from release_record import MANIFEST, SIGNATURE, sign, signature_valid


def asset(identifier, name, payload):
    return {'id': identifier, 'name': name, 'state': 'uploaded', 'size': len(payload),
            'digest': 'sha256:' + hashlib.sha256(payload).hexdigest()}


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.github = Mock()
        self.github.repo = 'PoirotJin/kinora'
        self.github.notify.return_value = (37, True)
        self.release = {'id': 42, 'tag_name': 'kinora-v1.0.0', 'target_commitish': 'a' * 40,
                        'immutable': False, 'draft': False}
        self.github.api.side_effect = (
            lambda path, *args, **kwargs: self.release if not args else None
        )
        self.approved = asset(1, 'kinora.zip', b'approved bytes')
        self.baseline = {'42': {'tag': self.release['tag_name'],
                                'assets': {'kinora.zip': guard.metadata(self.approved)}}}

    def test_extra_and_replaced_legacy_assets_remove_only_unapproved_ids(self):
        changed = asset(2, 'kinora.zip', b'unknown replacement')
        extra = asset(3, 'extra.zip', b'manual upload')
        for current, expected_removed in (
            ([self.approved, extra], [3]),
            ([changed, extra], [2, 3]),
        ):
            with self.subTest(current=current):
                self.github.reset_mock()
                self.github.assets.return_value = current
                self.assertTrue(guard.reconcile(self.github, self.release, self.baseline, 'PoirotJin', False))
                deletions = [call.args[0] for call in self.github.api.call_args_list if '-X' in call.args and 'DELETE' in call.args]
                self.assertEqual(deletions, [f'releases/assets/{i}' for i in expected_removed])
                self.github.notify.assert_called_once()

    def test_unsigned_new_release_is_withdrawn_whole(self):
        self.release['immutable'] = True
        self.github.assets.return_value = [self.approved]
        self.assertTrue(guard.reconcile(self.github, self.release, {}, 'PoirotJin', False))
        self.github.api.assert_any_call('releases/42', '-X', 'DELETE')
        self.github.notify.assert_called_once()

    def test_changed_snapshot_stops_removal(self):
        extra = asset(3, 'extra.zip', b'manual upload')
        self.github.assets.side_effect = [[self.approved, extra], [self.approved]]
        with self.assertRaisesRegex(guard.ReconcileError, 'changed'):
            guard.reconcile(self.github, self.release, self.baseline, 'PoirotJin', False)
        self.github.api.assert_called_once_with('releases/42')
        self.github.notify.assert_not_called()

    def test_notice_failure_does_not_block_withdrawal(self):
        self.github.notify.side_effect = guard.ReconcileError('GitHub issue request failed')
        self.github.assets.return_value = [self.approved]
        self.assertTrue(guard.reconcile(self.github, self.release, {}, 'PoirotJin', False))
        self.github.api.assert_any_call('releases/42', '-X', 'DELETE')

    def test_valid_record_cannot_be_replayed_for_another_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / 'test.pem'
            public = root / 'test.pub'
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out', str(private)], check=True)
            subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(public)], check=True)
            release_dir = root / 'dist'
            release_dir.mkdir()
            (release_dir / 'kinora.zip').write_bytes(b'approved bytes')
            sign(release_dir, self.github.repo, '1.0.0', 42, 'a' * 40, private.read_text(), public)
            manifest = (release_dir / MANIFEST).read_bytes()
            signature = (release_dir / SIGNATURE).read_bytes()
            self.assertFalse(signature_valid(manifest + b' ', signature, public))
            record_assets = [asset(4, MANIFEST, manifest), asset(5, SIGNATURE, signature)]
            self.github.read_record_asset.side_effect = lambda entry: {4: manifest, 5: signature}[entry['id']]
            with patch.object(guard, 'signature_valid', lambda m, s: signature_valid(m, s, public)):
                approved = guard.trusted_assets(self.github, self.release, [self.approved, *record_assets], {})
                self.assertEqual(set(approved), {'kinora.zip', MANIFEST, SIGNATURE})
                self.assertIsNone(guard.trusted_assets(
                    self.github, {**self.release, 'id': 43}, [self.approved, *record_assets], {},
                ))
