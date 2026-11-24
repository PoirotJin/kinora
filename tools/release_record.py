"""Create and verify the protected record for a scanned Kinora release."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

MANIFEST = 'KINORA-RELEASE-GATE.json'
SIGNATURE = MANIFEST + '.sig'
VERIFY_KEY = Path(__file__).with_name('release_record.pub')
RECORD_FILES = {MANIFEST, SIGNATURE}


def inventory(directory):
    approved = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.is_symlink() or path.name in RECORD_FILES:
            raise ValueError('Unexpected entry in already verified artifacts')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        approved[path.name] = {'size': path.stat().st_size, 'digest': f'sha256:{digest}'}
    if not approved:
        raise ValueError('Expected verified artifacts')
    return approved


def sign(directory, repo, version, release_id, commit, private_key, public_key=VERIFY_KEY, *, platform=None):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-beta(?:\.[1-9][0-9]*)?)?', version) or not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('Invalid version or commit')
    if platform is not None and (platform not in {'ubuntu', 'server'} or '-beta' not in version):
        raise ValueError('Invalid platform for the release version')
    tag = 'kinora-v' + version + (f'-{platform}' if platform else '')
    record = {
        'schema': 1,
        'repository': repo,
        'release_id': release_id,
        'tag': tag,
        'commit': commit,
        'assets': inventory(directory),
    }
    manifest = directory / MANIFEST
    signature = directory / SIGNATURE
    manifest.write_text(json.dumps(record, sort_keys=True) + '\n')
    with tempfile.TemporaryDirectory(prefix='kinora-signing-') as temp:
        key = Path(temp) / 'key.pem'
        key.write_text(private_key, encoding='ascii')
        result = subprocess.run(
            ['openssl', 'pkeyutl', '-sign', '-rawin', '-inkey', str(key),
             '-in', str(manifest), '-out', str(signature)],
            capture_output=True, timeout=30,
        )
    if result.returncode or not signature_valid(manifest.read_bytes(), signature.read_bytes(), public_key):
        raise ValueError('Release record signature did not match the pinned verification key')


def signature_valid(record, signature, public_key=VERIFY_KEY):
    if len(signature) != 64:
        return False
    with tempfile.TemporaryDirectory(prefix='kinora-record-') as temp:
        manifest = Path(temp) / MANIFEST
        signed = Path(temp) / SIGNATURE
        manifest.write_bytes(record)
        signed.write_bytes(signature)
        result = subprocess.run(
            ['openssl', 'pkeyutl', '-verify', '-rawin', '-pubin', '-inkey', str(public_key),
             '-in', str(manifest), '-sigfile', str(signed)],
            capture_output=True, timeout=30,
        )
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('repo')
    parser.add_argument('version')
    parser.add_argument('release_id', type=int)
    parser.add_argument('commit')
    parser.add_argument('--platform', choices=['ubuntu', 'server'])
    args = parser.parse_args()
    try:
        sign(args.directory, args.repo, args.version, args.release_id, args.commit,
             os.environ['KINORA_RELEASE_RECORD_KEY'], platform=args.platform)
    except (KeyError, OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit('Publication blocked: could not sign the gate record') from None


if __name__ == '__main__':
    main()
