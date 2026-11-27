"""Collect the tested desktop artifacts consumed by the public Kinora publisher."""
import argparse
import json
from pathlib import Path
import re
import shutil


def find_one(directory, pattern):
    files = [path for path in directory.rglob(pattern) if path.is_file()]
    if len(files) != 1:
        raise ValueError(f'Expected one {pattern} in {directory.name}')
    return files[0]


def assemble(version, source, renderer, output):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('Invalid desktop version')
    prefix = f'kinora-{version}-'
    output.mkdir()
    renderer_assets = sorted('/' + path.relative_to(renderer).as_posix()
                             for path in renderer.rglob('*') if path.is_file())
    if not {'/index.html', '/app.js'} <= set(renderer_assets):
        raise ValueError('Renderer inventory is missing required entry points')
    (output / 'renderer-assets.json').write_text(json.dumps(renderer_assets) + '\n')
    for platform, pattern, name, signed in (
        ('macos-universal', '*.dmg', f'kinora_{version}_universal.dmg', False),
        ('updates-macos-universal', '*.app.tar.gz', f'kinora-{version}-macos-universal.app.tar.gz', True),
        ('updates-windows-x86_64', '*-setup.exe', f'kinora_{version}_x64-setup.exe', True),
        ('updates-ubuntu-x86_64', '*.deb', f'kinora_{version}_amd64.deb', True),
        ('updates-ubuntu-x86_64', '*.AppImage', f'kinora-{version}-linux-x86_64.AppImage', True),
    ):
        artifact = find_one(source / (prefix + platform), pattern)
        if signed and name.endswith(('.exe', '.deb')):
            installer_platform = platform.removeprefix('updates-')
            installer = find_one(source / (prefix + installer_platform), pattern)
            if artifact.read_bytes() != installer.read_bytes():
                raise ValueError('The signed binary differs from the installer tested in CI')
        shutil.copyfile(artifact, output / name)
        if signed:
            shutil.copyfile(artifact.with_name(artifact.name + '.sig'), output / (name + '.sig'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version')
    parser.add_argument('source', type=Path)
    parser.add_argument('renderer', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    assemble(args.version, args.source, args.renderer, args.output)
