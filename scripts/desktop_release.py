"""Assemble an immutable server release directory from signed Tauri artifacts.

Run on downloaded CI artifacts, then copy the files to the update server and
replace latest.json LAST. Never includes signing keys; no network operations.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlsplit


def assemble(version, origin, artifacts, destination, notes=''):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Release version must be x.y.z')
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError('Expected an HTTPS origin')
    platforms, prepared = {}, []
    for platform, path in artifacts:
        if platform not in ('darwin-aarch64', 'darwin-x86_64', 'windows-x86_64', 'windows-aarch64', 'linux-x86_64', 'linux-aarch64') or platform in platforms:
            raise ValueError('Invalid or repeated platform: ' + platform)
        path = Path(path)
        suffix = '.app.tar.gz' if platform.startswith('darwin-') else '.exe' if platform.startswith('windows-') else '.AppImage'
        if not path.name.endswith(suffix) or not path.is_file() or path.stat().st_size == 0:
            raise ValueError('Wrong or empty update artifact: ' + str(path))
        signature = Path(str(path) + '.sig').read_text().strip()
        decoded = base64.b64decode(signature, validate=True)
        if not decoded.startswith(b'untrusted comment:') or len(decoded.splitlines()) < 4:
            raise ValueError('Invalid signature file')
        name = f'kinora-{version}-{platform}{suffix}'
        platforms[platform] = {'url': origin + '/desktop/releases/' + name, 'signature': signature}
        prepared.append((path, name))
    if not prepared: raise ValueError('At least one signed artifact is required')
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    for path, name in prepared:
        target = destination / name
        if target.exists():
            if hashlib.file_digest(target.open('rb'), 'sha256').digest() != hashlib.file_digest(path.open('rb'), 'sha256').digest():
                raise ValueError('Refusing to overwrite a released artifact: ' + name)
        else:
            shutil.copyfile(path, target)
    manifest = {'version': version, 'notes': notes, 'platforms': platforms}
    temporary = destination / 'latest.json.tmp'
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(destination / 'latest.json')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--origin', default='https://your-kinora.example.com:8765')
    parser.add_argument('--artifact', action='append', required=True, help='platform=/path/to/signed/artifact')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--notes', default='')
    args = parser.parse_args()
    result = assemble(args.version, args.origin, [x.split('=', 1) for x in args.artifact], args.output, args.notes)
    print('Prepared ' + args.version + ': ' + ', '.join(result['platforms']))
