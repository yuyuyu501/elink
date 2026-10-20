from pathlib import Path

import pytest

from scripts.build_video import find_vcvars


@pytest.mark.parametrize('location', ['x86', 'x64'])
def test_find_compiler_in_both_program_files_roots(tmp_path, location):
    roots = {key: tmp_path / key for key in ('x86', 'x64')}
    candidate = roots[location] / 'Microsoft Visual Studio/2026/Enterprise/VC/Auxiliary/Build/vcvars64.bat'
    candidate.parent.mkdir(parents=True)
    candidate.touch()
    assert find_vcvars({'ProgramFiles(x86)': str(roots['x86']), 'ProgramFiles': str(roots['x64'])}) == candidate


def test_installer_can_find_custom_buildtools_location(tmp_path, monkeypatch):
    installer = tmp_path / 'Microsoft Visual Studio/Installer/vswhere.exe'
    installer.parent.mkdir(parents=True)
    installer.touch()
    candidate = tmp_path / 'custom toolchain/VC/Auxiliary/Build/vcvars64.bat'
    candidate.parent.mkdir(parents=True)
    candidate.touch()

    def query(args, **kwargs):
        assert Path(args[0]) == installer
        assert '-products' in args and '*' in args
        assert 'Microsoft.VisualStudio.Component.VC.Tools.x86.x64' in args
        return str(candidate) + '\n'

    monkeypatch.setattr('scripts.build_video.subprocess.check_output', query)
    assert find_vcvars({'ProgramFiles(x86)': str(tmp_path), 'ProgramFiles': str(tmp_path)}) == candidate
