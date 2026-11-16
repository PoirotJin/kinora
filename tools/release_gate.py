"""Fail closed on protected addresses and plaintext credentials in release payloads."""
import argparse
import bz2
import gzip
import ipaddress
import io
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import zipfile

from desktop_assets import asset_entries, chunks

IPV4 = re.compile(rb'(?<!\d)(?<!\d\.)(?:\d{1,3}\.){3}\d{1,3}(?!\d|\.\d)')
CREDENTIAL = re.compile(
    rb'(?<![\w-])(?:sk-[A-Za-z0-9_-]{16,}|mfg_[A-Za-z0-9_-]{32,}|LTAI[A-Za-z0-9]{16,})(?![\w-])'
)
PROVIDER_LITERAL = re.compile(
    rb"""(?ix)
    (?:QWEN|DASHSCOPE|GADGET|MOTIONFARM)[_-]?(?:API[_-]?)?(?:KEY|TOKEN|SECRET)
    ["']?\s*[:=]\s*
    (?:(?P<quote>["'])(?P<quoted>[A-Za-z0-9_./+=-]{12,160})(?P=quote)
      |(?P<bare>[A-Za-z0-9_./+=-]{12,160})(?=[ \t]*(?:$|[;#\r\n])))
    """
)
PLACEHOLDER = re.compile(rb'^(?:example|sample|dummy|fake|your|test|replace|path|changeme|synthetic)(?:[-_]|$)', re.I)
WIDE_LE = re.compile(rb'(?:[\x20-\x7e]\x00){8,}')
WIDE_BE = re.compile(rb'(?:\x00[\x20-\x7e]){8,}')
GITHUB_FEED = b'https://github.com/PoirotJin/kinora/releases/latest/download/latest.json'
APP_NAMES = {'motionfarm-desktop', 'motionfarm-desktop.exe', 'kinora', 'kinora.exe'}
MAX_FILE = 256 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024

class GateError(Exception):
    pass

def protected_addresses():
    configured = os.environ.get('KINORA_BLOCKED_IPS', '')
    try:
        addresses = {str(ipaddress.IPv4Address(value.strip())).encode()
                     for value in configured.split(',')}
    except ipaddress.AddressValueError:
        raise GateError('protected IPv4 configuration is missing or invalid') from None
    if len(addresses) != 2 or len(configured.split(',')) != 2:
        raise GateError('exactly two protected IPv4 addresses must be configured')
    return addresses

def check_text(data, blocked):
    for ip in IPV4.findall(data):
        if ip in blocked:
            raise GateError('protected IPv4 address')
    if CREDENTIAL.search(data):
        raise GateError('plaintext credential pattern')
    for match in PROVIDER_LITERAL.finditer(data):
        value = match['quoted'] or match['bare']
        if value.lower().endswith((b'.txt', b'.key')) or PLACEHOLDER.match(value):
            continue
        raise GateError('literal Qwen or Gadget credential assignment')

def check_bytes(data, blocked):
    check_text(data, blocked)
    for pattern, encoding in ((WIDE_LE, 'utf-16le'), (WIDE_BE, 'utf-16be')):
        for match in pattern.finditer(data):
            check_text(match[0].decode(encoding).encode('ascii'), blocked)

def read_bounded(stream):
    data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise GateError('expanded member exceeds the inspection limit')
    return data

def check_member(name, blocked):
    path = PurePosixPath(name.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or re.match(r'^[A-Za-z]:', str(path)):
        raise GateError('archive contains an unsafe member path')
    check_text(name.encode(), blocked)
    return path.name

def ar_members(data):
    offset = 8
    while offset < len(data):
        header = data[offset:offset + 60]
        if len(header) != 60 or header[-2:] != b'`\n':
            raise GateError('invalid Debian package header')
        size = int(header[48:58])
        name = header[:16].decode('ascii').strip().rstrip('/')
        start = offset + 60
        if size < 0 or start + size > len(data):
            raise GateError('invalid Debian package member size')
        yield name, data[start:start + size]
        offset = start + size + size % 2
    if offset != len(data):
        raise GateError('invalid Debian package ending')

def squashfs(data):
    candidates = []
    for match in re.finditer(b'hsqs', data):
        offset = match.start()
        if offset + 96 > len(data):
            continue
        fields = struct.unpack_from('<5I6H8Q', data, offset)
        block, major, minor, size = fields[3], fields[9], fields[10], fields[12]
        if major == 4 and minor == 0 and block in (1 << n for n in range(12, 21)) and 96 < size <= len(data) - offset:
            candidates.append((offset, size))
    if len(candidates) != 1:
        raise GateError('could not identify the AppImage SquashFS payload')
    offset, size = candidates[0]
    return data[offset:offset + size]

def nsis_payload(data):
    start = data.index(b'NullsoftInst') - 8
    _, _, _, _, _, header_size, total_size = struct.unpack_from('<7I', data, start)
    properties = data[start + 28]
    dictionary, = struct.unpack_from('<I', data, start + 29)
    if dictionary > MAX_FILE:
        raise GateError('NSIS dictionary exceeds inspection limit')
    filters = [{
        'id': lzma.FILTER_LZMA1, 'dict_size': dictionary,
        'lc': properties % 9, 'lp': properties // 9 % 5, 'pb': properties // 45,
    }]
    decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=filters)
    payload = decoder.decompress(data[start + 33:start + total_size - 4], max_length=MAX_FILE)
    if len(payload) >= MAX_FILE or struct.unpack_from('<I', payload)[0] != header_size:
        raise GateError('could not fully decode the NSIS installer stream')
    return payload

def is_zstandard(data):
    return data.startswith(b'\x28\xb5\x2f\xfd') or (
        len(data) >= 4 and 0x50 <= data[0] <= 0x5f and data[1:4] == b'\x2a\x4d\x18'
    )


def decode_zstandard(data):
    with tempfile.TemporaryDirectory(prefix='kinora-zstd-') as directory:
        source = Path(directory) / 'payload.zst'
        source.write_bytes(data)
        with subprocess.Popen(
            ['zstd', '--decompress', '--stdout', '--quiet', '-M128MB', str(source)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        ) as process:
            try:
                decoded = read_bounded(process.stdout)
            except GateError:
                process.kill()
                raise
            if process.wait():
                raise GateError('could not fully decode a Zstandard archive')
    return decoded


class Scanner:
    def __init__(self, seven_zip=None, renderer=None):
        self.seven_zip = seven_zip or shutil.which('7zz') or shutil.which('7z')
        self.renderer = renderer
        self.blocked = protected_addresses()
        self.total = 0
        self.desktop_slices = 0

    def scan_7zip(self, data, suffix, depth):
        if not self.seven_zip:
            raise GateError('7-Zip is required to inspect desktop packages')
        with tempfile.TemporaryDirectory(prefix='kinora-scan-') as tmp:
            base = Path(tmp)
            package = base / ('package' + suffix)
            package.write_bytes(data)
            extracted = base / 'contents'
            command = [self.seven_zip, 'x', '-y', '-sns-', '-snl-', '-snh-',
                       f'-o{extracted}', str(package)]
            result = subprocess.run(command, capture_output=True, timeout=180)
            if result.returncode or not extracted.is_dir():
                raise GateError('7-Zip could not fully extract a desktop package')
            for item in sorted(extracted.rglob('*')):
                name = check_member(item.relative_to(extracted).as_posix(), self.blocked)
                if item.is_symlink():
                    check_text(item.readlink().as_posix().encode(), self.blocked)
                    continue
                if item.is_file():
                    with item.open('rb') as stream:
                        self.scan(read_bounded(stream), name, depth + 1)

    def scan(self, data, name, depth=0):
        self.total += len(data)
        if depth > 12 or self.total > MAX_TOTAL or len(data) > MAX_FILE:
            raise GateError('archive exceeds the inspection limits')
        check_bytes(data, self.blocked)
        if data.startswith((b'PK\x03\x04', b'PK\x05\x06')):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for member in archive.infolist():
                    child = check_member(member.filename, self.blocked)
                    if not member.is_dir():
                        with archive.open(member) as stream:
                            self.scan(read_bounded(stream), child, depth + 1)
        elif data.startswith(b'!<arch>\n') and name.endswith('.deb'):
            for member, contents in ar_members(data):
                self.scan(contents, check_member(member, self.blocked), depth + 1)
        elif is_zstandard(data):
            self.scan(decode_zstandard(data), name.rsplit('.', 1)[0], depth + 1)
        elif data.startswith((b'\x1f\x8b', b'\xfd7zXZ\0', b'BZh')):
            source = io.BytesIO(data)
            if data.startswith(b'\x1f\x8b'):
                decoded = gzip.GzipFile(fileobj=source)
            elif data.startswith(b'\xfd7zXZ\0'):
                decoded = lzma.LZMAFile(source)
            else:
                decoded = bz2.BZ2File(source)
            with decoded as stream:
                self.scan(read_bounded(stream), name.rsplit('.', 1)[0], depth + 1)
        elif name.endswith('.tar') or data[257:262] == b'ustar':
            with tarfile.open(fileobj=io.BytesIO(data), mode='r:') as archive:
                for member in archive:
                    child = check_member(member.name, self.blocked)
                    if member.issym() or member.islnk():
                        check_text(member.linkname.encode(), self.blocked)
                    if member.isfile():
                        with archive.extractfile(member) as stream:
                            self.scan(read_bounded(stream), child, depth + 1)
        elif name.endswith('.dmg'):
            if data[-512:-508] != b'koly':
                raise GateError('invalid DMG')
            self.scan_7zip(data, '.dmg', depth)
        elif name.endswith('.AppImage'):
            if not data.startswith(b'\x7fELF') or data[8:11] != b'AI\x02':
                raise GateError('invalid AppImage')
            self.scan_7zip(squashfs(data), '.squashfs', depth)
        elif name.endswith('-setup.exe'):
            check_bytes(nsis_payload(data), self.blocked)
            self.scan_7zip(data, '.exe', depth)
        elif name in APP_NAMES and data.startswith((b'MZ', b'\x7fELF', b'\xca\xfe\xba\xbe', b'\xcf\xfa\xed\xfe')):
            self.scan_desktop(data)
        elif name.lower().endswith((
            '.zip', '.deb', '.tar.gz', '.tar.xz', '.tar.bz2', '.tgz', '.txz', '.tbz2',
            '.gz', '.xz', '.bz2', '.zst', '.tzst', '.zstd', '.7z', '.rar', '.cab',
            '.iso', '.cpio', '.msi',
        )) or data.startswith((b'7z\xbc\xaf\x27\x1c', b'Rar!\x1a\x07', b'MSCF\0\0\0\0')):
            raise GateError('unrecognized package format')

    def scan_desktop(self, contents):
        for _, binary in chunks(contents):
            if self.renderer is not None and GITHUB_FEED not in binary:
                raise GateError('desktop updater does not point to the GitHub release feed')
            _, assets = asset_entries(binary)
            if b'/index.html' not in assets or b'/app.js' not in assets:
                raise GateError('could not recover the embedded Tauri frontend')
            if self.renderer is not None and self.renderer != {key.decode('ascii') for key in assets}:
                raise GateError('embedded frontend differs from the release build inventory')
            self.desktop_slices += 1
            for name, (contents, _) in assets.items():
                self.scan(contents, name.decode('ascii'), 1)

def inspect(path, seven_zip=None, renderer=None):
    scanner = Scanner(seven_zip, renderer)
    name = check_member(path.name, scanner.blocked)
    with path.open('rb') as stream:
        scanner.scan(read_bounded(stream), name)
    if name.endswith(('.dmg', '-setup.exe', '.deb', '.AppImage', '.app.tar.gz', '-update-server.tar.gz')):
        if not scanner.desktop_slices:
            raise GateError('installer did not yield a Kinora executable')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path)
    parser.add_argument('--seven-zip')
    args = parser.parse_args()
    for number, path in enumerate(args.files, 1):
        try:
            inspect(path, args.seven_zip)
        except GateError as error:
            raise SystemExit(f'Publication blocked at artifact {number}: {error}') from None
        except (OSError, ValueError, struct.error, tarfile.TarError, zipfile.BadZipFile,
                lzma.LZMAError, subprocess.SubprocessError):
            raise SystemExit(f'Publication blocked at artifact {number}: artifact inspection failed') from None
        print(f'Artifact {number} passed unpacking and disclosure checks', flush=True)

if __name__ == '__main__':
    main()
