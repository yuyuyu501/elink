"""Compile the small D3D11/OpenGL bridge using the installed Windows SDK/MSVC."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def find_vcvars(env):
    roots = [Path(env.get(key, default)) for key, default in (
        ('ProgramFiles(x86)', 'C:/Program Files (x86)'), ('ProgramFiles', 'C:/Program Files'))]
    # Installer metadata covers Enterprise/BuildTools and custom installation paths.
    for root in roots:
        vswhere = root / 'Microsoft Visual Studio/Installer/vswhere.exe'
        if vswhere.is_file():
            result = subprocess.check_output(
                [str(vswhere), '-latest', '-products', '*', '-requires',
                 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64', '-find',
                 'VC/Auxiliary/Build/vcvars64.bat'], text=True, env=env, timeout=15)
            for line in result.splitlines():
                candidate = Path(line.strip())
                if candidate.is_file():
                    return candidate
    candidates = sorted({candidate for root in roots for candidate in
                         (root / 'Microsoft Visual Studio').glob('*/*/VC/Auxiliary/Build/vcvars64.bat')},
                        reverse=True)
    if not candidates:
        raise RuntimeError('Install Visual Studio C++ Build Tools and Windows SDK before building.')
    return candidates[0]


def build():
    env = os.environ.copy()
    if not shutil.which('cl'):
        vcvars = find_vcvars(env)
        result = subprocess.check_output(f'cmd /d /s /c ""{vcvars}" >nul && set"', text=True)
        env.update(line.split('=', 1) for line in result.splitlines() if '=' in line and not line.startswith('='))
    env['VSLANG'] = '1033'
    destination = ROOT / '.artifacts' / 'video-build'
    subprocess.run(['cmake', '-S', str(ROOT / 'native' / 'video'), '-B', str(destination),
                    '-G', 'Ninja' if shutil.which('ninja', path=env.get('PATH')) else 'NMake Makefiles',
                    '-DCMAKE_BUILD_TYPE=Release'], env=env, check=True)
    subprocess.run(['cmake', '--build', str(destination)], env=env, check=True)
    return destination / 'elink_video.dll'


if __name__ == '__main__':
    print(build())
