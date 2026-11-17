"""Gate a macOS-only prerelease without creating a stable updater feed."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

from release_gate import GateError, Scanner, inspect, read_bounded


def prepare(version, candidate, checksum, directory, notes):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+-beta(?:\.[1-9][0-9]*)?', version):
        raise GateError('expected a macOS beta version')
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != checksum:
        raise GateError('candidate checksum differs')
    name = f'kinora_{version}_universal.dmg'
    with zipfile.ZipFile(candidate) as archive:
        if sorted(archive.namelist()) != sorted([name, 'renderer-assets.json']):
            raise GateError('unexpected beta candidate contents')
        with archive.open('renderer-assets.json') as stream:
            renderer = json.loads(read_bounded(stream))
        if (not isinstance(renderer, list) or not all(isinstance(v, str) and v.startswith('/') for v in renderer)
                or len(renderer) != len(set(renderer)) or not {'/index.html', '/app.js'} <= set(renderer)):
            raise GateError('invalid frontend inventory')
        with archive.open(name) as stream:
            payload = read_bounded(stream)
    scanner = Scanner(renderer=set(renderer))
    scanner.scan(payload, name)
    if scanner.desktop_slices != 2:
        raise GateError('expected both universal macOS executable slices')
    inspect(notes)
    directory.mkdir()
    (directory / name).write_bytes(payload)
    (directory / 'INSTALL.txt').write_bytes(notes.read_bytes())
    checksums = ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
                        for p in sorted(directory.iterdir()))
    (directory / 'SHA256SUMS.txt').write_text(checksums)
    print('Universal macOS beta passed both executable and embedded frontend disclosure checks')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('candidate', type=Path)
    parser.add_argument('checksum')
    parser.add_argument('directory', type=Path)
    parser.add_argument('notes', type=Path)
    args = parser.parse_args()
    prepare(args.version, args.candidate, args.checksum, args.directory, args.notes)
