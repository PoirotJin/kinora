"""Compare GitHub's draft assets with the exact files approved by the release gate."""
import argparse
import hashlib
import json
from pathlib import Path


def verify(directory, remote_assets):
    expected = {}
    for asset in directory.iterdir():
        if not asset.is_file() or asset.is_symlink():
            raise ValueError('Unexpected entry in approved release artifacts')
        with asset.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        expected[asset.name] = (asset.stat().st_size, f'sha256:{digest}')
    if not expected or not isinstance(remote_assets, list):
        raise ValueError('Expected approved files and a list of GitHub assets')
    observed = {}
    for asset in remote_assets:
        name = asset['name']
        if name in observed or asset['state'] != 'uploaded':
            raise ValueError('Duplicate or incomplete asset in the GitHub draft')
        observed[name] = (asset['size'], asset['digest'])
    if observed != expected:
        raise ValueError('GitHub draft assets differ from the verified release artifacts')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('remote_assets', type=Path)
    args = parser.parse_args()
    try:
        verify(args.directory, json.loads(args.remote_assets.read_text()))
    except (KeyError, OSError, TypeError, ValueError):
        raise SystemExit('Publication blocked: GitHub draft files do not match the approved asset list') from None
    print('All GitHub draft asset names, sizes and SHA-256 digests match the approved release files')


if __name__ == '__main__':
    main()
