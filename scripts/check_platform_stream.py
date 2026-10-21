"""Bounded desktop/peer streaming probe. Hosted input is recorded, never injected."""
import argparse
import asyncio
import json
from pathlib import Path
import time

from elink.core import codecs
from elink.core.input import RecordingInput
from elink.core.profiling import PipelineProfile
from elink.core.transport import Client, HostServer
from elink.logging_setup import configure_logging
from elink.platforms import capabilities


async def run(args):
    host, client = None, None
    result = dict(platform=capabilities(), ok=False)
    sink = RecordingInput()
    try:
        if args.serve or not args.endpoint:
            host = HostServer(args.output / 'host', synthetic=not args.desktop, backend_factory=lambda: sink,
                              notify=lambda message: print(message, flush=True))
            await host.start(args.bind, args.port)
            print(f'READY {host.port}', flush=True)
        if args.serve:
            deadline = time.monotonic() + args.seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(.1)
            result.update(ok=True, host=host.profile.snapshot() if host.profile else {}, input_events=sink.events)
        else:
            client = Client(args.output / 'client', notify=lambda message: print(message, flush=True), play_audio=False)
            client.set_renderer('gpu')
            address = args.endpoint or f'127.0.0.1:{host.port}'
            await client.connect(address, dict(width=args.width, height=args.height, fps=args.fps,
                                               bitrate=args.bitrate, audio=args.audio), decoder=args.decoder)
            started = time.monotonic()
            previous = 0
            samples = []
            while time.monotonic() - started < args.seconds:
                await asyncio.sleep(1)
                total = client.mailbox.received
                samples.append(total - previous)
                previous = total
                print(dict(frames=total, audio=client.audio_frames, encoder=client.encoder,
                           decoder=codecs.metrics['decoder']), flush=True)
            result.update(ok=client.mailbox.received >= args.fps * args.seconds * .5 and (not args.audio or client.audio_frames > 0),
                          video_frames=client.mailbox.received, audio_frames=client.audio_frames,
                          samples=samples, metrics=dict(codecs.metrics), performance=await client.performance_report())
            frame = client.mailbox.take()
            if frame is not None:
                result['frame'] = dict(width=frame.width, height=frame.height,
                                       colorspace=int(frame.colorspace), color_range=int(frame.color_range))
    except Exception as exc:
        import traceback
        result['error'] = traceback.format_exc()
        PipelineProfile().exception('platform_probe', exc)
    finally:
        if client:
            await client.disconnect()
        if host:
            await host.stop()
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'result.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k not in ('performance', 'host', 'input_events')}, ensure_ascii=False))
    return 0 if result['ok'] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.artifacts/platform-check'))
    parser.add_argument('--endpoint')
    parser.add_argument('--serve', action='store_true')
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--seconds', type=int, default=10)
    parser.add_argument('--desktop', action='store_true')
    parser.add_argument('--audio', action='store_true')
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--fps', type=int, default=60)
    parser.add_argument('--bitrate', type=int, default=20)
    parser.add_argument('--decoder', choices=('auto', 'hardware', 'software'), default='auto')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error('seconds must be between 1 and 300')
    args.output.mkdir(parents=True, exist_ok=True)
    configure_logging(args.output)
    return asyncio.run(run(args))


if __name__ == '__main__':
    raise SystemExit(main())
