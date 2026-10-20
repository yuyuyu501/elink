"""Compile the small D3D11/OpenGL bridge using the installed Windows SDK/MSVC."""
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def build():
    env = os.environ.copy()
    if not shutil.which('cl'):
        base = Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft Visual Studio'
        candidates = sorted(base.glob('20*/*/VC/Auxiliary/Build/vcvars64.bat'), reverse=True)
        if not candidates:
            raise RuntimeError('Install Visual Studio C++ Build Tools and Windows SDK before building.')
        result = subprocess.check_output(f'cmd /d /s /c ""{candidates[0]}" >nul && set"', text=True)
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
