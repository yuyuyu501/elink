"""Explicit Windows packaging: tests -> build -> EXE self-test -> portable ZIP."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

from elink import __version__
from elink.processes import native_environment

ROOT = Path(__file__).resolve().parents[1]


def source_snapshot():
    names = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT)
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in sorted(set(names.decode('utf-8').split('\0'))) if name and (ROOT / name).is_file()}


def validate_self_test(report):
    if not isinstance(report, dict) or report.get('ok') is not True or report.get('runtime_stopped') is not True:
        raise RuntimeError('Packaged EXE self-test or shutdown failed; no release archive created.')
    if report.get('video_frames', 0) <= 0 or report.get('audio_frames', 0) <= 0:
        raise RuntimeError('Packaged EXE did not receive both video and audio.')


def package():
    if os.name != 'nt' or sys.maxsize <= 2**32:
        raise RuntimeError('Build this package using 64-bit Python on Windows.')
    metadata = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    if metadata['project']['version'] != __version__:
        raise RuntimeError('pyproject.toml and elink.__version__ must match.')
    snapshot = source_snapshot()
    revision = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT))
    subprocess.run([sys.executable, '-m', 'pytest', '-q'], cwd=ROOT, check=True)
    subprocess.run([sys.executable, '-m', 'scripts.build'], cwd=ROOT, check=True)
    artifacts = ROOT / '.artifacts'
    artifacts.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    with tempfile.TemporaryDirectory(prefix='package-check-', dir=artifacts) as temp:
        temporary = Path(temp)
        diagnostics = temporary / 'self-test'
        subprocess.run([str(ROOT / 'dist' / 'Elink' / 'Elink.exe'), '--self-test', str(diagnostics)],
                       cwd=ROOT, env=native_environment(), check=True, timeout=120)
        report = json.loads((diagnostics / 'result.json').read_text(encoding='utf-8'))
        validate_self_test(report)
        if source_snapshot() != snapshot:
            raise RuntimeError('Source changed during packaging; rerun before distributing this build.')
        filename = f'Elink-{__version__}-windows-x64'
        archive = Path(shutil.make_archive(str(temporary / filename), 'zip', ROOT / 'dist', 'Elink'))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        receipt = dict(version=__version__, built_at=stamp, commit=revision, source_dirty=dirty,
                       source_sha256=snapshot, package_sha256=digest, self_test=report,
                       signed=False, channel='preview', format='portable-folder')
        (temporary / 'verification.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        (temporary / 'SHA256SUMS.txt').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
        destination = ROOT / 'dist' / 'releases' / f'v{__version__}' / stamp
        destination.mkdir(parents=True, exist_ok=False)
        for path in (archive, temporary / 'verification.json', temporary / 'SHA256SUMS.txt'):
            shutil.copy2(path, destination / path.name)
    print(f'Verified preview package: {destination}')
    print(f'Run: {ROOT / "dist" / "Elink" / "Elink.exe"}')
    print('Unzip the whole portable folder; keep its DLLs. No Git push, tag, release upload or driver install performed.')


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        package()
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Packaging stopped: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
