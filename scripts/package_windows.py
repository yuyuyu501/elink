"""Windows packaging: tests -> build -> EXE and updater checks -> portable ZIP."""
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
from scripts.test_portable_update import verify_portable_update
from scripts.check_decoder_colors import CASES as DECODER_COLOR_CASES

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


def validate_gpu_test(report):
    def colors_ok(colors):
        return len(colors) == 5 and all(isinstance(c.get('native_rgb_error'), (int, float))
                                       and 0 <= c['native_rgb_error'] <= 5 for c in colors)
    colors = report.get('colors', [])
    streams = report.get('streams', [])
    scaled_streams = report.get('super_resolution_streams', [])
    capture = report.get('native_capture', [])
    rtx = report.get('rtx_stream', {})
    decoded_colors = report.get('decoder_colors', [])
    live = report.get('live_settings', [])
    live_capture = report.get('live_capture', [])
    if (len(live) != 6 or {c.get('renderer') for c in live} != {'cpu', 'gpu'}
            or {c.get('decoder') for c in live} != {'software', 'hardware'}
            or any(c.get('same_session') is not True or not isinstance(c.get('native_rgb_error'), (int, float))
                   or not 0 <= c['native_rgb_error'] <= 5 for c in live)
            or len(live_capture) != 4
            or any(c.get('monotonic_pts') is not True or c.get('capture_backend') != 'gpu'
                   or not isinstance(c.get('max_rgb_error'), (int, float))
                   or not 0 <= c['max_rgb_error'] <= 6 for c in live_capture)):
        raise RuntimeError('Packaged live settings/capture checks have not passed.')
    if ({(c.get('width'), c.get('height'), c.get('colorspace'), c.get('color_range'), c.get('input_format'))
         for c in decoded_colors} != DECODER_COLOR_CASES
            or any(c.get('hardware_format') != 'nv12'
                   or c.get('software_colorspace') != c.get('colorspace')
                   or c.get('hardware_colorspace') != c.get('colorspace')
                   or c.get('software_color_range') != c.get('color_range')
                   or c.get('hardware_color_range') != c.get('color_range')
                   or any(not isinstance(c.get(key), (int, float)) or not 0 <= c[key] <= limit
                          for key, limit in (('software_rgb_error', 6), ('hardware_rgb_error', 6),
                                             ('decoder_rgb_difference', 3), ('native_rgb_error', 5),
                                             ('composed_rgb_error', 5)))
                   for c in decoded_colors)):
        raise RuntimeError('Packaged software/D3D11VA color parity checks have not passed.')
    if (not colors_ok(colors)
            or {s.get('decoder') for s in streams} != {'software-h264', 'D3D11VA'}
            or any(s.get('native_video') is not True or s.get('cpu_fallback') is not True for s in streams)
            or any(not colors_ok(report.get('super_resolution', {}).get(str(scale), [])) for scale in (75, 67, 50))
            or {s.get('render_scale') for s in scaled_streams} != {75, 67, 50}
            or any(s.get('upscaler') != 'FSR1-EASU-RCAS' or s.get('native_video') is not True
                   or s.get('cpu_fallback') is not True or not isinstance(s.get('fsr_gpu'), dict)
                   or s['fsr_gpu'].get('samples', 0) <= 0
                   for s in scaled_streams)
            or not colors_ok(report.get('rtx_colors', []))
            or rtx.get('render_scale') != 50 or rtx.get('upscaler') != 'RTX-VSR'
            or rtx.get('native_video') is not True or rtx.get('cpu_fallback') is not True
            or not isinstance(rtx.get('rtx_probe_mae'), (int, float)) or not rtx['rtx_probe_mae'] >= .01
            or not isinstance(rtx.get('rtx_gpu'), dict) or rtx['rtx_gpu'].get('samples', 0) <= 0
            or any(report.get('rtx_fallback', {}).get(key) is not True
                   for key in ('fsr_fallback', 'native_video', 'notice'))
            or {(s.get('width'), s.get('height')) for s in capture} != {(1920, 1080), (1440, 810), (1280, 720)}
            or any(s.get('format') != 'nv12' or not isinstance(s.get('max_rgb_error'), (int, float))
                   or not 0 <= s['max_rgb_error'] <= 6
                   or s.get('colorspace') != s.get('decoded_colorspace')
                   or s.get('color_range') != s.get('decoded_color_range') for s in capture)):
        raise RuntimeError('Packaged GPU native display checks have not passed.')


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
        gpu_result = artifacts / f'gpu-package-{stamp}.json'
        try:
            subprocess.run([str(ROOT / 'dist' / 'Elink' / 'Elink.exe'), '--self-test-gpu', str(gpu_result)],
                           cwd=ROOT, env=native_environment(), check=True, timeout=120)
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f'Packaged GPU check failed; diagnostic report: {gpu_result}') from exc
        gpu_report = json.loads(gpu_result.read_text(encoding='utf-8'))
        validate_gpu_test(gpu_report)
        upgrade = verify_portable_update(ROOT / 'dist' / 'Elink', temporary / 'update-check')
        if source_snapshot() != snapshot:
            raise RuntimeError('Source changed during packaging; rerun before distributing this build.')
        filename = f'Elink-{__version__}-windows-x64'
        archive = Path(shutil.make_archive(str(temporary / filename), 'zip', ROOT / 'dist', 'Elink'))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        receipt = dict(version=__version__, built_at=stamp, commit=revision, source_dirty=dirty,
                       source_sha256=snapshot, package_sha256=digest, self_test=report,
                       gpu_test=gpu_report, updater_test=upgrade, manual_acceptance='pending',
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
    return destination


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
