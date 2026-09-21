"""Explicit two-machine test runner, isolated identity and recording-only input."""
import asyncio
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import time
import traceback
from pathlib import Path

from .core import codecs
from .core.input import RecordingInput
from .core.transport import Client, HostServer
from .storage import atomic_json


async def until(predicate, timeout=30):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise TimeoutError('Peer test condition timed out')
        await asyncio.sleep(0.05)


def selected_paths(pc):
    paths = []
    if pc and pc.sctp:
        connection = pc.sctp.transport.transport._connection
        for pair in connection._nominated.values():
            paths.append({'local': str(pair.local_candidate), 'remote': str(pair.remote_candidate)})
    return paths


async def host(config, root, log):
    backend = RecordingInput()
    server = HostServer(root / 'identity', synthetic=config.get('synthetic', True),
                        backend_factory=lambda: backend, notify=log)
    server.scope.allows = lambda address: address == config['peer']
    result = {'role': 'host', 'ok': False}
    started = time.monotonic()
    try:
        await server.start(config['bind'], config.get('port', 49318))
        atomic_json(root / 'ready.json', {'port': server.port, 'mode': 'automatic'})
        connected = False
        connected_at = None
        deadline = started + config.get('timeout', 180)
        while time.monotonic() < deadline:
            if server.pc and server.pc.connectionState == 'connected':
                if not connected:
                    result['paths'] = selected_paths(server.pc)
                    connected_at = time.monotonic()
                connected = True
            stop_due = connected_at is not None and config.get('stop_after') is not None and time.monotonic() - connected_at >= config['stop_after']
            if connected and ((root / 'stop').exists() or stop_due) and server.device_id:
                await server.end_session()
                result['host_disconnected'] = True
            if connected and not server.pc and not server.ending:
                break
            await asyncio.sleep(0.05)
        if not connected:
            raise RuntimeError('No peer stream connected')
        if server.pc:
            raise RuntimeError('Peer did not finish before deadline')
        result['events'] = backend.events
        keys = [event for event in backend.events if event['type'] == 'key']
        pads = [event for event in backend.events if event['type'] == 'pad']
        result['input_verified'] = any(e['down'] for e in keys) and any(not e['down'] for e in keys)
        result['pad_verified'] = any(e['values'][0] == 4096 for e in pads) and any(e['values'] == [0] * 7 for e in pads)
        result['metrics'] = dict(codecs.metrics)
        result['ok'] = result['input_verified'] and result['pad_verified']
    finally:
        await server.stop()
        result['cleanup'] = server.pc is None and server.runner is None and server.input is None
        result['elapsed_seconds'] = time.monotonic() - started
        atomic_json(root / 'host-result.json', result)


async def client(config, root, log):
    receiver = Client(root / 'identity', notify=log, play_audio=False)
    result = {'role': 'client', 'ok': False, 'samples': []}
    try:
        await receiver.connect(config['address'], dict(width=config.get('width', 1920), height=config.get('height', 1080),
                                                       fps=config.get('fps', 60), audio=config.get('audio', True),
                                                       bitrate=config.get('bitrate', 20)))
        await until(lambda: receiver.mailbox.received >= 5 and receiver.control.readyState == 'open')
        result['paths'] = selected_paths(receiver.pc)
        receiver.focus(True)
        await asyncio.sleep(0.2)
        receiver.send({'type': 'key', 'vk': 65, 'scan': 30, 'extended': False, 'down': True})
        receiver.send({'type': 'pad', 'index': 0, 'values': [4096, 128, 255, -32768, 32767, 1234, -1234]})
        await asyncio.sleep(0.3)
        receiver.focus(False)
        previous, last = receiver.mailbox.received, time.monotonic()
        hashes = set()
        for _ in range(config.get('seconds', 20)):
            await asyncio.sleep(1)
            now = time.monotonic()
            if receiver.pc is None:
                raise RuntimeError('Unexpected stream disconnect')
            sample = {'fps': (receiver.mailbox.received - previous) / (now - last),
                      'rtt_ms': receiver.rtt_ms, 'video_frames': receiver.mailbox.received,
                      'audio_frames': receiver.audio_frames,
                      'decoder': codecs.metrics['decoder'], 'decode_errors': codecs.metrics['decode_errors']}
            result['samples'].append(sample)
            previous, last = receiver.mailbox.received, now
            pixels = receiver.mailbox.take()
            if pixels is not None:
                hashes.add(hashlib.sha256(pixels.tobytes()).hexdigest())
                result['shape'] = list(pixels.shape)
                result['pixel_std'] = float(pixels.std())
            log(json.dumps(sample))
        result['metrics'] = dict(codecs.metrics)
        result['video_frames'], result['audio_frames'] = receiver.mailbox.received, receiver.audio_frames
        result['distinct_frames'] = len(hashes)
        result['stats'] = [{key: value.isoformat() if isinstance(value, datetime) else value
                            for key, value in asdict(stat).items()}
                           for stat in (await receiver.pc.getStats()).values()]
        if config.get('wait_stop'):
            atomic_json(root / 'awaiting-stop.json', {'ready': True})
            await until(lambda: receiver.pc is None, 30)
            result['host_disconnected'] = True
        else:
            await receiver.disconnect()
        result['ok'] = result['video_frames'] > 30 and (not config.get('audio', True) or result['audio_frames'] > 10)
        if config.get('synthetic', True):
            result['ok'] = result['ok'] and len(hashes) > 1
    finally:
        await receiver.disconnect()
        result['cleanup'] = receiver.pc is None and not receiver.tasks and receiver.output is None
        atomic_json(root / 'client-result.json', result)
    if not result['ok']:
        raise RuntimeError('Peer media verification failed; see client-result.json')


def run(path):
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    root = Path(config['root']).resolve()
    root.mkdir(parents=True, exist_ok=False)
    def log(message):
        with (root / 'events.log').open('a', encoding='utf-8') as stream:
            stream.write(str(message) + '\n')
    try:
        asyncio.run((host if config['role'] == 'host' else client)(config, root, log))
        return 0
    except Exception:
        (root / 'error.txt').write_text(traceback.format_exc(), encoding='utf-8')
        return 1
