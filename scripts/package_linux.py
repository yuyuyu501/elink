"""Build a complete Linux x86_64 .deb on Linux; never installs drivers or the app."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

from elink import __version__
from scripts.package_metadata import source_identity, copy_python_licenses

ROOT = Path(__file__).resolve().parents[1]


def package(output=None, manifest=None):
    if sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise RuntimeError('Build the Linux x64 package on Linux x86_64')
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    identity = source_identity(manifest)
    if identity['source_dirty']:
        raise RuntimeError('Linux Preview builds require committed, synchronized source')
    output = (output or ROOT / 'dist' / 'linux' / timestamp).resolve()
    output.mkdir(parents=True, exist_ok=False)
    stage = output / 'stage'
    distribution = output / 'bundle'
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
               '--name', 'elink', '--distpath', str(distribution),
               '--workpath', str(output / 'build'), '--specpath', str(output),
               '--add-data', 'elink/resources:elink/resources',
               '--collect-all', 'av', '--collect-all', 'aiortc', '--collect-all', 'pylibsrtp',
               '--collect-all', 'soundcard', '--collect-all', 'sounddevice',
               '--hidden-import', 'mss.linux', '--hidden-import', 'Xlib.ext.xtest',
               '--hidden-import', 'dbus_next.aio', '--exclude-module', 'pytest', 'main.py']
    subprocess.run(command, cwd=ROOT, check=True)
    bundle = distribution / 'elink'
    # A Linux package must not ship the Windows virtual controller library.
    for library in bundle.rglob('ViGEmClient.dll'):
        library.unlink()
    test_root = output / 'self-test'
    subprocess.run([str(bundle / 'elink'), '--self-test-desktop', str(test_root)],
                   cwd=ROOT, check=True, timeout=100)
    verification = json.loads((test_root / 'result.json').read_text(encoding='utf-8'))
    if not verification.get('ok') or not verification.get('runtime_stopped'):
        raise RuntimeError('Bundled desktop and shutdown checks failed')
    install = stage / 'usr' / 'lib' / 'elink'
    shutil.copytree(bundle, install)
    for name in ('README.md', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT / name, install / name)
    shutil.copytree(ROOT / 'licenses', install / 'licenses')
    copy_python_licenses(install / 'licenses' / 'python-packages')
    launch = stage / 'usr' / 'bin' / 'elink'
    launch.parent.mkdir(parents=True)
    launch.write_text('#!/bin/sh\nexec /usr/lib/elink/elink "$@"\n', encoding='ascii')
    launch.chmod(0o755)
    applications = stage / 'usr' / 'share' / 'applications'
    applications.mkdir(parents=True)
    applications.joinpath('elink.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=Elink\nExec=elink\nIcon=elink\nTerminal=false\nCategories=Network;RemoteAccess;\n', encoding='ascii')
    icons = stage / 'usr' / 'share' / 'icons' / 'hicolor' / 'scalable' / 'apps'
    icons.mkdir(parents=True)
    shutil.copy2(ROOT / 'elink' / 'resources' / 'monitor.svg', icons / 'elink.svg')
    control = stage / 'DEBIAN'
    control.mkdir()
    control.joinpath('control').write_text(
        f'Package: elink\nVersion: {__version__}\nSection: net\nPriority: optional\nArchitecture: amd64\n'
        'Maintainer: Elink <noreply@github.com>\n'
        'Depends: libc6 (>= 2.38), libglib2.0-0, libx11-6, libxext6, libxfixes3, libxtst6, libxcb-cursor0, libxcb-xinerama0, libxkbcommon-x11-0, libgl1, libpulse0, libportaudio2\n'
        'Recommends: libsdl2-2.0-0, xclip\n'
        'Description: Elink desktop controller and host preview\n'
        ' Built and checked on Deepin 25 x86_64 with X11. No graphics driver is bundled.\n', encoding='utf-8')
    archive = output / f'Elink-{__version__}-linux-x64.deb'
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(archive)], check=True)
    subprocess.run(['dpkg-deb', '--info', str(archive)], check=True)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / 'Linux.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
    receipt = dict(version=__version__, platform='linux-x64', package=archive.name,
                   sha256=digest, bytes=archive.stat().st_size, system=platform.platform(), self_test=verification,
                   manual_acceptance='pending', **identity)
    if source_identity(manifest) != identity:
        raise RuntimeError('Source changed during Linux packaging')
    (output / 'Linux.verification.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print(archive, flush=True)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--source-manifest', type=Path)
    args = parser.parse_args()
    package(args.output, args.source_manifest)
