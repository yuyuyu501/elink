"""Sequential GPU loopback coverage of every resolution/FPS preset in the UI.

Keep the test window visible on an unlocked Windows desktop. This is a short
throughput sweep, not two-machine gaming or sustained physical scanout acceptance.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from elink.core.transport import settings
from elink.models import ValidationError


RESOLUTIONS = ((1280, 720), (1600, 900), (1920, 1080), (2560, 1440), (3840, 2160))
FRAME_RATES = (30, 60, 90, 120, 144, 165, 240)


def cases():
    for width, height in RESOLUTIONS:
        for fps in FRAME_RATES:
            yield dict(width=width, height=height, fps=fps)


def assess(report):
    rates = report['rates']
    requested = report['config']['fps']
    errors = rates['decode_errors_per_second']
    stages = ('encoded_frames_per_second', 'decoded_frames_per_second', 'presented_frames_per_second')
    valid = not report.get('invalid_window_samples', 0) and all(rates[key] > 0 for key in stages)
    host = report.get('host', {}).get('metadata', {})
    client = report.get('client', {}).get('metadata', {})
    hardware = (host.get('encoder') in ('h264_nvenc', 'h264_amf', 'h264_qsv')
                and client.get('decoder') == 'D3D11VA' and client.get('renderer') == 'opengl-yuv')
    return dict(valid_measurement=valid, decode_errors=errors > 0,
                hardware_path=hardware,
                # A comparison threshold, not a claim that each second is locked.
                within_five_percent=valid and hardware and not errors
                and all(rates[key] >= requested * .95 for key in stages))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=10)
    parser.add_argument('--warmup', type=float, default=3)
    parser.add_argument('--bitrate', type=int, default=20)
    parser.add_argument('--source', choices=('motion', 'desktop'), default='motion')
    parser.add_argument('--render-scale', type=int, choices=(100, 75, 67, 50), default=100)
    parser.add_argument('--upscaler', choices=('fsr', 'rtx'), default='fsr')
    parser.add_argument('--capture-backend', choices=('auto', 'dxcam', 'gpu'), default='auto')
    parser.add_argument('--output', type=Path, default=Path('.artifacts/fps-matrix'))
    args = parser.parse_args(argv)
    if not (0 < args.seconds <= 300 and 0 <= args.warmup <= 60 and 1 <= args.bitrate <= 80):
        parser.error('Invalid measurement duration, warmup or bitrate')
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for case in cases():
        row = dict(case, render_scale=args.render_scale, capture_backend=args.capture_backend)
        stem = f"{case['width']}x{case['height']}-{case['fps']}"
        try:
            settings(dict(case, bitrate=args.bitrate, render_scale=args.render_scale))
        except ValidationError as exc:
            row.update(status='rejected', reason=str(exc))
        else:
            report = args.output / (stem + '.json')
            # Do not let a failed rerun accidentally reuse an older report.
            report.unlink(missing_ok=True)
            command = [sys.executable, '-m', 'scripts.benchmark_stream', '--present', '--separate-host',
                       '--source', args.source,
                       '--width', str(case['width']), '--height', str(case['height']),
                       '--fps', str(case['fps']), '--bitrate', str(args.bitrate),
                       '--render-scale', str(args.render_scale), '--upscaler', args.upscaler,
                       '--capture-backend', args.capture_backend,
                       '--seconds', str(args.seconds), '--warmup', str(args.warmup), '--output', str(report)]
            print('START ' + stem, flush=True)
            # The child owns its host cleanup. No timeout that kills just the
            # launcher and leaves competing GPU benchmark processes behind.
            run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding='utf-8', errors='replace',
                                 env=dict(os.environ, PYTHONIOENCODING='utf-8'))
            (args.output / (stem + '.log')).write_text(run.stdout, encoding='utf-8')
            row.update(status='measured' if run.returncode == 0 else 'failed', returncode=run.returncode)
            if report.exists():
                data = json.loads(report.read_text(encoding='utf-8'))
                row.update(rates=data['rates'], host=data['host']['metadata'], client=data['client']['metadata'],
                           **assess(data))
        rows.append(row)
        (args.output / 'summary.json').write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding='utf-8')
        print(json.dumps(row, ensure_ascii=False), flush=True)
    print(f'DONE: {len(rows)} combinations; measured does not mean target FPS was reached.', flush=True)
    return 0 if all(row['status'] == 'rejected' or
                    row['status'] == 'measured' and row.get('within_five_percent') for row in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
