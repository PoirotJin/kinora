"""Prepare and verify the GitHub Releases updater manifest and its exact assets."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

from release_gate import GateError, inspect, read_bounded

GITHUB = 'https://github.com/PoirotJin/kinora/releases/download/'

def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()

def expected_packages(version):
    prefix = f'kinora_{version}'
    return {
        'dmg': f'{prefix}_universal.dmg',
        'exe': f'{prefix}_x64-setup.exe',
        'deb': f'{prefix}_amd64.deb',
        'mac': f'kinora-{version}-macos-universal.app.tar.gz',
        'image': f'kinora-{version}-linux-x86_64.AppImage',
    }

def copy_candidate(candidate, checksum, directory, packages):
    if digest(candidate) != checksum:
        raise GateError('candidate ZIP does not match the build digest')
    expected = {'renderer-assets.json'} | set(packages.values()) | {f'{name}.sig' for kind, name in packages.items() if kind != 'dmg'}
    with zipfile.ZipFile(candidate) as archive:
        entries = archive.namelist()
        if set(entries) != expected or len(entries) != len(expected):
            raise GateError('unexpected or missing files in the build artifact')
        with archive.open('renderer-assets.json') as stream:
            assets = json.loads(read_bounded(stream))
        if not isinstance(assets, list) or not all(
            isinstance(name, str) and name.startswith('/') and len(name) <= 500 for name in assets
        ) or len(assets) != len(set(assets)) or not {'/index.html', '/app.js'} <= set(assets):
            raise GateError('invalid renderer inventory')
        for name in sorted(expected - {'renderer-assets.json'}):
            with archive.open(name) as stream:
                (directory / name).write_bytes(read_bounded(stream))
        return set(assets)

def verify_signature(path, version):
    signature = path.with_name(path.name + '.sig').read_text().strip()
    with tempfile.TemporaryDirectory(prefix='kinora-signature-') as directory:
        decoded = Path(directory) / 'signature'
        decoded.write_bytes(base64.b64decode(signature, validate=True))
        key = Path(__file__).with_name('updater.pub')
        proc = subprocess.run(
            ['minisign', '-V', '-p', str(key), '-m', str(path), '-x', str(decoded)],
            capture_output=True, text=True, timeout=90,
        )
        if proc.returncode or f'version:{version}' not in (proc.stdout + proc.stderr).split():
            raise GateError('an updater signature is invalid or for a different version')
    return signature

def prepare(version, candidate, checksum, sources, directory):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise GateError('expected a three-part desktop release version')
    packages = expected_packages(version)
    directory.mkdir()
    renderer = copy_candidate(candidate, checksum, directory, packages)
    for source in sources:
        if not re.fullmatch(r'kinora-(?:server|gadget)-[0-9]+\.[0-9]+\.[0-9]+\.zip', source.name):
            raise GateError('unexpected source archive name')
        target = directory / source.name
        if target.exists():
            raise GateError('duplicate source archive')
        shutil.copyfile(source, target)
    for prefix in ('kinora-server-', 'kinora-gadget-'):
        if len(list(directory.glob(prefix + '*.zip'))) != 1:
            raise GateError('expected one server source archive and one Gadget source archive')

    # Scan exact bytes before creating the update feed. An inspection error is a failed release.
    for index, path in enumerate(sorted(directory.iterdir()), 1):
        try:
            inspect(path, renderer=renderer if path.name in packages.values() else None)
        except GateError as error:
            raise GateError(f'artifact {index}: {error}') from None
        print(f'Artifact {index} passed unpacking and disclosure checks', flush=True)
    base = f'{GITHUB}kinora-v{version}/'
    signatures = {kind: verify_signature(directory / name, version)
                  for kind, name in packages.items() if kind != 'dmg'}
    platforms = {}
    for target, kind in (
        ('darwin-aarch64', 'mac'), ('darwin-x86_64', 'mac'),
        ('windows-x86_64', 'exe'), ('windows-x86_64-nsis', 'exe'),
        ('linux-x86_64', 'image'), ('linux-x86_64-appimage', 'image'),
        ('linux-x86_64-deb', 'deb'),
    ):
        platforms[target] = {'url': base + packages[kind], 'signature': signatures[kind]}
    feed = directory / 'latest.json'
    feed.write_text(json.dumps({'version': version, 'platforms': platforms}, indent=2) + '\n')
    inspect(feed)
    lines = [f'{digest(path)}  {path.name}\n' for path in sorted(directory.iterdir())]
    (directory / 'SHA256SUMS.txt').write_text(''.join(lines))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('candidate', type=Path)
    parser.add_argument('checksum')
    parser.add_argument('directory', type=Path)
    parser.add_argument('sources', nargs='+', type=Path)
    args = parser.parse_args()
    try:
        prepare(args.version, args.candidate, args.checksum, args.sources, args.directory)
    except GateError as error:
        raise SystemExit(f'Publication blocked: {error}') from None
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError):
        raise SystemExit('Publication blocked: unable to safely prepare release files') from None

if __name__ == '__main__':
    main()
