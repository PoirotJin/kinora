"""Recover Tauri's Brotli frontend assets from Mach-O, PE and ELF bundles."""
import ctypes
from ctypes.util import find_library
import re
import struct

LIBRARY = None
PATH_PATTERN = re.compile(rb'/[A-Za-z0-9_@./+-]+')
ANCHORS = (b'/index.html', b'/app.js', b'/gadget.js')
MACH_POINTER_MASK = (1 << 36) - 1
MAX_ASSET_SIZE = 4 * 1024 * 1024


def chunks(data):
    if data[:4] != b'\xca\xfe\xba\xbe':
        yield '', data
        return
    count, = struct.unpack_from('>I', data, 4)
    for index in range(count):
        cpu, _, offset, size, _ = struct.unpack_from('>5I', data, 8 + 20 * index)
        yield hex(cpu), data[offset:offset + size]


def segments(data):
    """Return file offsets, load addresses and sizes for file-backed segments."""
    mappings = []
    base = 0
    if data.startswith(b'\x7fELF'):
        program_offset, = struct.unpack_from('<Q', data, 32)
        entry_size, count = struct.unpack_from('<HH', data, 54)
        for index in range(count):
            header = program_offset + index * entry_size
            kind, _, offset, address, _, size, _, _ = struct.unpack_from('<II6Q', data, header)
            if kind == 1 and size:
                mappings.append((offset, address, size))
        return 'ELF', base, mappings

    if data.startswith(b'MZ'):
        header, = struct.unpack_from('<I', data, 60)
        count, = struct.unpack_from('<H', data, header + 6)
        optional_size, = struct.unpack_from('<H', data, header + 20)
        optional = header + 24
        magic, = struct.unpack_from('<H', data, optional)
        if data[header:header + 4] != b'PE\0\0' or magic != 0x20B:
            raise ValueError('unsupported PE format')
        base, = struct.unpack_from('<Q', data, optional + 24)
        for index in range(count):
            section = optional + optional_size + 40 * index
            _, address, size, offset = struct.unpack_from('<4I', data, section + 8)
            if size:
                mappings.append((offset, base + address, size))
        return 'PE', base, mappings

    if data.startswith(b'\xcf\xfa\xed\xfe'):
        count, = struct.unpack_from('<I', data, 16)
        header = 32
        for _ in range(count):
            command, length = struct.unpack_from('<II', data, header)
            if command == 0x19:
                name = data[header + 8:header + 24].rstrip(b'\0')
                address, _, offset, size = struct.unpack_from('<4Q', data, header + 24)
                if name == b'__TEXT':
                    base = address
                if size:
                    mappings.append((offset, address, size))
            header += length
        return 'MACH', base, mappings

    raise ValueError('unsupported desktop executable')


def apply_elf_relocations(data, file_offset):
    # PIE executables store pointer values as R_X86_64_RELATIVE addends.
    patched = bytearray(data)
    table, = struct.unpack_from('<Q', data, 40)
    entry_size, count = struct.unpack_from('<HH', data, 58)
    for index in range(count):
        header = table + index * entry_size
        kind, = struct.unpack_from('<I', data, header + 4)
        if kind != 4:  # SHT_RELA
            continue
        start, size = struct.unpack_from('<QQ', data, header + 24)
        for entry in range(start, start + size, 24):
            address, info, target = struct.unpack_from('<3Q', data, entry)
            if info & 0xFFFFFFFF == 8:
                offset = file_offset(address)
                if offset is not None:
                    struct.pack_into('<Q', patched, offset, target)
    return bytes(patched)


def brotli(data):
    global LIBRARY
    if LIBRARY is None:
        LIBRARY = ctypes.CDLL(find_library('brotlidec') or 'libbrotlidec.so.1')
        LIBRARY.BrotliDecoderDecompress.argtypes = [
            ctypes.c_size_t, ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p,
        ]
        LIBRARY.BrotliDecoderDecompress.restype = ctypes.c_int
    output = ctypes.create_string_buffer(MAX_ASSET_SIZE)
    length = ctypes.c_size_t(len(output))
    result = LIBRARY.BrotliDecoderDecompress(len(data), data, ctypes.byref(length), output)
    return output.raw[:length.value] if result == 1 else None


def asset_entries(data):
    kind, base, mappings = segments(data)

    def address_of(offset):
        return next((address + offset - start for start, address, size in mappings
                     if start <= offset < start + size), None)

    def file_offset(address):
        return next((start + address - target for start, target, size in mappings
                     if target <= address < target + size), None)

    def pointer_encodings(address):
        yield struct.pack('<Q', address)
        if kind == 'MACH':
            # The two chained fixup formats retain the low target bits.
            yield struct.pack('<Q', address)[:4]
            if address >= base:
                yield struct.pack('<Q', address - base)[:4]

    def pointer_targets(value):
        yield value
        if kind == 'MACH':
            yield value & MACH_POINTER_MASK
            yield base + (value & MACH_POINTER_MASK)

    if kind == 'ELF':
        data = apply_elf_relocations(data, file_offset)

    def read_entry(offset):
        if not 0 <= offset <= len(data) - 32:
            return None
        path_pointer, path_size, data_pointer, data_size = struct.unpack_from('<4Q', data, offset)
        if not (0 < path_size < 500 and 0 < data_size < MAX_ASSET_SIZE):
            return None
        for target in pointer_targets(path_pointer):
            start = file_offset(target)
            if start is None:
                continue
            path = data[start:start + path_size]
            if not PATH_PATTERN.fullmatch(path):
                continue
            for payload in pointer_targets(data_pointer):
                start = file_offset(payload)
                if start is not None and start + data_size <= len(data):
                    asset = brotli(data[start:start + data_size])
                    if asset is not None:
                        return path, asset
        return None

    # The PHF map stores consecutive tuples of Rust string and byte-slice fat pointers.
    for anchor in ANCHORS:
        for match in re.finditer(re.escape(anchor), data):
            address = address_of(match.start())
            if address is None:
                continue
            for pointer in pointer_encodings(address):
                offset = data.find(pointer)
                while offset >= 0:
                    entry = read_entry(offset) if offset % 8 == 0 else None
                    if entry and entry[0] == anchor:
                        assets = {}
                        for step in (32, -32):
                            current = offset
                            while record := read_entry(current):
                                assets[record[0]] = (record[1], current)
                                current += step
                        if b'/index.html' in assets and b'/app.js' in assets:
                            return kind, assets
                    offset = data.find(pointer, offset + 1)
    return kind, {}
