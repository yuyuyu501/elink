"""Composed GPU video/overlay checks and CPU/GPU presentation benchmark.

Run on an unlocked Windows desktop, not offscreen CI. Test windows temporarily
stay on top; native pixels are checked in memory, never saved. No input injection.
Optional stream checks use local synthetic H.264/WebRTC with software/hardware decode.
Timings include CPU submission, not GPU completion or physical scanout.
"""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace

import av
import numpy as np
from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from elink.core.media import FrameMailbox
from elink.core.media import bgr_pixels
from elink.core.profiling import PipelineProfile
from elink.ui.player import Player
from elink.ui.theme import apply_theme


class Runtime(QObject):
    feedback = Signal(object)

    def call(self, fn, *args):
        fn(*args)


def player(app, mode, *, statistics=True, render_scale=100, upscaler='fsr'):
    client = SimpleNamespace(profile=PipelineProfile(), pc=None, mailbox=FrameMailbox(), rtt_ms=0,
                             last_pong=0, focus=lambda _: None, set_renderer=lambda _: None,
                             send=lambda _: None)
    view = Player(Runtime(), client, controllers=False,
                  preferences={'renderer': mode, 'render_scale': render_scale, 'upscaler': upscaler,
                               'resolution': [960, 540]})
    view.timer.stop()
    view.show_statistics = statistics
    prepare_visible(view)
    view.show()
    app.processEvents()
    return view


def prepare_visible(view):
    # QScreen.grabWindow observes the real screen and can include an occluder.
    # Keep only this synthetic test window on top and within the work area.
    view.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint)
    area = view.screen().availableGeometry()
    view.resize(min(930, area.width()), min(650, area.height()))
    view.move(area.topLeft())


def screen_image(app, view):
    settle(app, .15)
    # Window-DC capture can return a stale pre-fullscreen surface on Windows.
    # Capture the displayed client rectangle from the desktop instead.
    position = view.mapToGlobal(QPoint()) - view.screen().geometry().topLeft()
    image = view.screen().grabWindow(0, position.x(), position.y(), view.width(), view.height()).toImage()
    if image.isNull():
        raise AssertionError('Native output unavailable; use an unlocked Windows desktop')
    return image


SAMPLE_POINTS = ((.25, .25), (.75, .25), (.25, .75), (.75, .75))


def stream_sample_points(expected):
    # A moving synthetic stripe can land exactly on a quadrant boundary.
    # Resampling blends that edge; compare flat interiors with the same strict
    # color tolerance instead of comparing a blended pixel to a single texel.
    height, width = expected.shape[:2]
    points = []
    for nx, ny in SAMPLE_POINTS:
        for offset in (0, .03, -.03, .06, -.06):
            x, y = int((nx + offset) * width), int(ny * height)
            patch = expected[y - 8:y + 9, x - 8:x + 9].astype(int)
            if np.max(patch.max(axis=(0, 1)) - patch.min(axis=(0, 1))) <= 2:
                points.append((nx + offset, ny))
                break
        else:
            raise AssertionError('Synthetic stream has no flat color sample in a quadrant')
    return points


def verify_pixels(view, image, expected, points=SAMPLE_POINTS):
    rect = view.rect_image
    ratio = image.width() / view.width()
    errors = []
    for nx, ny in points:
        actual = image.pixelColor(round((rect.x() + nx * rect.width()) * ratio),
                                  round((rect.y() + ny * rect.height()) * ratio)).getRgb()[:3]
        reference = expected[int(ny * expected.shape[0]), int(nx * expected.shape[1])].astype(int)
        errors.append(int(np.max(np.abs(reference - actual))))
    if max(errors) > 5:
        raise AssertionError(f'Native/composed video color errors {errors}; output may be black or occluded')
    return max(errors)


def settle(app, seconds=.1):
    until = time.perf_counter() + seconds
    while time.perf_counter() < until:
        app.processEvents()
        time.sleep(.001)


def verify_colors(app, render_scale=100, upscaler='fsr'):
    view = player(app, 'gpu', render_scale=render_scale, upscaler=upscaler)
    results = []
    try:
        for fmt, space, full in [('yuv420p', 5, False), ('nv12', 5, False),
                                  ('yuv420p', 1, False), ('yuvj420p', 5, True),
                                  ('nv12', 1, True)]:
            # Non-aligned dimensions exercise PyAV line padding. Recreating
            # textures between formats also checks resize/resource lifetime.
            rgb = np.zeros((362, 642, 3), dtype=np.uint8)
            colors = [(240, 30, 20), (20, 220, 35), (15, 30, 230), (235, 235, 235)]
            rgb[:181, :321], rgb[:181, 321:] = colors[:2]
            rgb[181:, :321], rgb[181:, 321:] = colors[2:]
            frame = av.VideoFrame.from_ndarray(rgb, format='rgb24').reformat(
                format=fmt, dst_colorspace='ITU709' if space == 1 else 'ITU601',
                dst_color_range=2 if full else 1)
            frame.colorspace, frame.color_range = space, 2 if full else 1
            surface = view.gpu_surface
            if surface is None:
                raise RuntimeError('GPU renderer fell back before color validation')
            # Exercise the same mailbox/queued-signal handoff as a real session, with
            # the default statistics visible across consecutive video frames.
            for _ in range(3):
                view.client.mailbox.put(frame)
                app.processEvents()
                settle(app, .03)
            if surface.broken or not surface.isValid() or view.gpu_surface is None:
                raise RuntimeError('GPU renderer unavailable: ' + view.error_label.text())
            if render_scale != 100 and surface.upscaler is None:
                raise AssertionError('Super resolution was requested but is not active')
            actual_upscaler = view.client.profile.snapshot()['metadata'].get('upscaler', 'native')
            if render_scale != 100 and actual_upscaler != ('RTX-VSR' if upscaler == 'rtx' else 'FSR1-EASU-RCAS'):
                raise AssertionError('Requested upscaler fell back: ' + view.error_label.text())
            expected = frame.to_ndarray(format='rgb24', src_colorspace='ITU709' if space == 1 else 'ITU601',
                                        dst_colorspace='ITU601', src_color_range=2 if full else 1)
            # Read native output BEFORE grab(): QWidget.grab renders internally
            # and succeeds even when Qt's on-screen compositor is entirely black.
            native_error = verify_pixels(view, screen_image(app, view), expected)
            error = verify_pixels(view, view.grab().toImage(), expected)
            results.append(dict(format=fmt, colorspace=space, full_range=full,
                                max_rgb_error=error, native_rgb_error=native_error,
                                upscaler=actual_upscaler))
        # Resource reconstruction when moving between a window and fullscreen.
        view.toggle_fullscreen()
        settle(app)
        verify_pixels(view, screen_image(app, view), expected)
        view.toggle_fullscreen()
        settle(app)
        if view.gpu_surface is None or view.gpu_surface.broken:
            raise AssertionError('GPU context failed during fullscreen transition')
        verify_pixels(view, screen_image(app, view), expected)
        # Validate retained video, not merely context survival, after recreation.
        surface = view.gpu_surface
        image = view.grab().toImage()
        rect, ratio = view.rect_image, surface.devicePixelRatioF()
        actual = image.pixelColor(round((rect.x() + rect.width() * .25) * ratio),
                                  round((rect.y() + rect.height() * .25) * ratio)).getRgb()[:3]
        if np.max(np.abs(expected[90, 160].astype(int) - actual)) > 5:
            raise AssertionError('Retained frame was lost during fullscreen transition')
        # Both overlay removal and software cursor must compose correctly
        # without painting into the OpenGL video target.
        QTest.keyClick(view, Qt.Key.Key_F10)
        settle(app)
        without_stats = screen_image(app, view)
        QTest.keyClick(view, Qt.Key.Key_F10)
        settle(app)
        with_stats = screen_image(app, view)
        if without_stats == with_stats:
            raise AssertionError('Statistics overlay did not compose above video')
        view.software_cursor_pos = view.rect_image.center()
        view.software_cursor = True
        view.refresh_video()
        settle(app)
        with_cursor = screen_image(app, view)
        view.software_cursor = False
        view.refresh_video()
        settle(app)
        if with_cursor == screen_image(app, view):
            raise AssertionError('Software cursor overlay did not show/hide')
        view.controls_button.click()
        settle(app)
        if not view.control_menu.isVisible():
            raise AssertionError('GPU overlay blocked the control menu')
        view.control_menu.close()
        settle(app)
        before = view.client.profile.snapshot()['counters']['gpu_paint_submit']
        settle(app, .15)
        after = view.client.profile.snapshot()['counters']['gpu_paint_submit']
        if after - before > 2:
            raise AssertionError('Idle GPU player is repainting continuously')
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()
    return results


def benchmark(app, mode, frames, width, height):
    view = player(app, mode, statistics=False)
    rgb = np.zeros((height, width, 3), dtype=np.uint8)
    rgb[:, ::2, 1] = 180
    frame = av.VideoFrame.from_ndarray(rgb, format='rgb24').reformat(format='yuv420p')
    try:
        view.resize(1280, 760)
        settle(app)
        for index in range(frames + 10):
            if index == 10:
                view.client.profile = PipelineProfile()
                cpu_at, wall_at = time.process_time(), time.perf_counter()
            profile = view.client.profile
            started = time.perf_counter()
            if mode == 'gpu':
                if view.gpu_surface is None:
                    raise RuntimeError('GPU renderer fell back during benchmark')
                view.gpu_surface.set_frame(frame)
            else:
                pixels = bgr_pixels(frame)
                profile.elapsed('prepare_display', started)
                view.set_frame_pixels(pixels)
                view.update()
            view.frame_taken_at = time.perf_counter()
            # repaint is synchronous CPU submission. No image grab in the timed
            # region: a GPU readback would distort the result.
            (view.gpu_surface if mode == 'gpu' else view).repaint()
            app.processEvents()
            profile.elapsed('prepare_and_paint_call', started)
        result = view.client.profile.snapshot()
        result.update(mode=mode, frames=frames, source=[width, height],
                      cpu_seconds=time.process_time() - cpu_at, wall_seconds=time.perf_counter() - wall_at,
                      desktop_refresh_hz=view.screen().refreshRate())
        if mode == 'gpu' and view.gpu_surface.broken:
            raise RuntimeError('GPU renderer failed during benchmark')
        return result
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()


def verify_stream(app, root, decoder, render_scale=100, upscaler='fsr'):
    """Real H.264/WebRTC/GUI handoff using only local synthetic video."""
    from elink.core.runtime import Runtime as NetworkRuntime
    from elink.core.transport import Client, HostServer
    from elink.core.input import RecordingInput

    runtime = NetworkRuntime()
    host = HostServer(root / 'host', synthetic=True, backend_factory=RecordingInput)
    client = Client(root / 'client', play_audio=False)
    client.set_renderer('gpu')
    view = None

    async def connect():
        port = await host.start('127.0.0.1', 0)
        await client.connect(f'127.0.0.1:{port}', dict(width=1920, height=1080, fps=60, audio=False,
                                                    render_scale=render_scale), decoder)

    async def close():
        await client.disconnect()
        await host.stop()

    try:
        runtime.submit(connect()).result(25)
        view = Player(runtime, client, controllers=False, preferences={'renderer': 'gpu', 'upscaler': upscaler})
        prepare_visible(view)
        view.show()
        deadline = time.perf_counter() + 12
        while client.profile.snapshot()['counters'].get('presented_frames', 0) < 30:
            settle(app, .03)
            if view.gpu_surface is None or view.gpu_surface.broken:
                raise AssertionError('Real stream GPU display failed: ' + view.error_label.text())
            if time.perf_counter() > deadline:
                raise AssertionError('Real stream did not present 30 frames')
        view.timer.stop()
        client.mailbox.set_notifier(None)
        settle(app)
        surface = view.gpu_surface
        frame = surface.frame
        reference = bgr_pixels(frame)
        points = stream_sample_points(reference)
        verify_pixels(view, screen_image(app, view), reference[:, :, ::-1], points)
        image = view.grab().toImage()
        rect, ratio = view.rect_image, surface.devicePixelRatioF()
        for nx, ny in points:
            actual = image.pixelColor(round((rect.x() + nx * rect.width()) * ratio),
                                      round((rect.y() + ny * rect.height()) * ratio)).getRgb()[:3]
            expected = reference[int(ny * frame.height), int(nx * frame.width), ::-1].astype(int)
            if np.max(np.abs(expected - actual)) > 5:
                raise AssertionError(f'Real {decoder} stream composed pixels disagree with decoded frame')
        metadata = client.profile.snapshot()['metadata']
        if decoder == 'hardware' and metadata.get('decoder') != 'D3D11VA':
            raise AssertionError('Hardware validation did not use D3D11VA')

        # A genuine render failure must remove both GPU widgets and let the
        # receive worker deliver CPU frames, with an accessible error notice.
        surface.fail(RuntimeError('injected rendering failure for fallback check'))
        client.mailbox.set_notifier(view.frame_ready.emit)
        view.timer.start(8)
        deadline = time.perf_counter() + 5
        while view.gpu_surface is not None or view.image is None:
            settle(app, .03)
            if time.perf_counter() > deadline:
                raise AssertionError('Failed GPU stream did not resume CPU video')
        if view.gpu_overlay is not None or not view.error_label.isVisible():
            raise AssertionError('Fallback left an overlay over the CPU video or hid its notice')
        return dict(decoder=metadata['decoder'], pixel_format=frame.format.name,
                    composed_video=True, native_video=True, cpu_fallback=True, render_scale=render_scale,
                    upscaler=metadata.get('upscaler', 'native'), fsr_gpu=client.profile.snapshot()['stages'].get('fsr_gpu'),
                    rtx_gpu=client.profile.snapshot()['stages'].get('rtx_gpu'),
                    rtx_probe_mae=metadata.get('rtx_probe_mae'))
    finally:
        if view:
            view.close()
            view.deleteLater()
            app.processEvents()
        try:
            runtime.submit(close()).result(15)
        finally:
            runtime.stop()


def verify_rtx_fallback(app):
    """An inactive driver must retain GPU video via FSR and explain the fallback."""
    from unittest.mock import patch
    from elink.ui import rtx_video

    with patch.object(rtx_video, 'RtxProcessor', side_effect=RuntimeError('injected inactive RTX driver')):
        view = player(app, 'gpu', render_scale=50, upscaler='rtx')
        try:
            rgb = np.full((270, 480, 3), 128, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(rgb, format='rgb24').reformat(format='nv12')
            view.client.mailbox.put(frame)
            settle(app)
            metadata = view.client.profile.snapshot()['metadata']
            if (view.gpu_surface is None or view.gpu_surface.broken
                    or metadata.get('upscaler') != 'FSR1-EASU-RCAS'
                    or 'inactive RTX driver' not in metadata.get('upscaler_fallback', '')
                    or not view.error_label.isVisible()):
                raise AssertionError('Inactive RTX driver did not fall back to FSR with a notice')
            verify_pixels(view, screen_image(app, view), frame.to_ndarray(format='rgb24'))
            return dict(fsr_fallback=True, native_video=True, notice=True)
        finally:
            view.close()
            view.deleteLater()
            app.processEvents()


def verify_decoder_colors(app):
    from scripts.check_decoder_colors import POINTS, verify

    view = player(app, 'gpu', statistics=False)
    try:
        def present(frame, reference):
            view.client.mailbox.put(frame)
            settle(app, .03)
            if view.gpu_surface is None or view.gpu_surface.broken:
                raise AssertionError('Decoded color check lost its GPU renderer: ' + view.error_label.text())
            native_error = verify_pixels(view, screen_image(app, view), reference, POINTS)
            composed_error = verify_pixels(view, view.grab().toImage(), reference, POINTS)
            return dict(native_rgb_error=native_error, composed_rgb_error=composed_error)
        return verify(present)
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()


def verify_live_settings(app, root):
    from elink.core.transport import HostServer
    from elink.core.input import RecordingInput
    from elink.ui.window import MainWindow
    from elink.ui.controls import StreamOptions

    window = MainWindow(root / 'ui', auto_refresh=False, auto_host=False)
    window.client.play_audio = False
    host = HostServer(root / 'host', synthetic=True, backend_factory=RecordingInput)
    def wait(predicate):
        deadline = time.perf_counter() + 15
        while not predicate():
            settle(app, .01)
            if time.perf_counter() > deadline:
                raise AssertionError('Live settings timed out: ' + (window.player.error_label.text() if window.player else 'no player'))
    try:
        port = window.runtime.submit(host.start('127.0.0.1', 0)).result(15)
        window.controllers.setChecked(False)
        window.audio.setChecked(False)
        window.address.setText(f'127.0.0.1:{port}')
        window.connect_remote()
        wait(lambda: window.player is not None and window.player.image is not None)
        view, client = window.player, window.client
        prepare_visible(view)
        view.show()
        client_pc, host_pc, identifier, channel = client.pc, host.pc, client.session_id, client.control
        view.set_muted(True)
        view.set_statistics(False)
        results = []
        cases = [([1280, 720], 90, 'hardware', 'gpu', 100, 'fsr', False),
                 ([1920, 1080], 60, 'software', 'cpu', 100, 'fsr', True),
                 ([2560, 1440], 60, 'hardware', 'gpu', 50, 'fsr', False),
                 ([1920, 1080], 90, 'hardware', 'gpu', 50, 'rtx', False),
                 ([1920, 1080], 60, 'hardware', 'gpu', 100, 'rtx', True),
                 ([1280, 720], 30, 'software', 'cpu', 100, 'fsr', False)]
        for size, fps, decoder, renderer, scale, upscaler, audio in cases:
            print(f'Live settings: {size}, {fps} FPS, {decoder}, {renderer}, {scale}%, {upscaler}', flush=True)
            view.open_quality()
            dialog = view.settings_dialog
            options = dialog.findChild(StreamOptions)
            options.resolution.setCurrentIndex(options.resolution.findData(size))
            options.fps.setCurrentIndex(options.fps.findData(fps))
            options.decoder.setCurrentIndex(options.decoder.findData(decoder))
            options.renderer.setCurrentIndex(options.renderer.findData(renderer))
            options.render_scale.setCurrentIndex(options.render_scale.findData(scale))
            options.upscaler.setCurrentIndex(options.upscaler.findData(upscaler))
            options.audio.setChecked(audio)
            options.bitrate.setValue(12)
            count = client.profile.snapshot()['counters'].get('presented_frames', 0)
            dialog.apply_button.click()
            wait(lambda: not view.quality_applying)
            if view.settings_dialog:
                raise AssertionError('Live application failed: ' + dialog.note.text())
            wait(lambda: client.profile.snapshot()['counters'].get('presented_frames', 0) > count + 15)
            if renderer == 'gpu' and view.gpu_surface is None:
                raise AssertionError('Live GPU switch fell back: ' + view.error_label.text())
            assert window.player is view and client.pc is client_pc and host.pc is host_pc
            assert client.session_id == identifier == host.session_id and client.control is channel
            assert view.muted and not view.show_statistics and client.decoder == decoder
            assert host.tracks[0].fps == fps and host.tracks[1].enabled.is_set() == audio
            if renderer == 'gpu':
                client.mailbox.set_notifier(None)
                settle(app)
                frame = view.gpu_surface.frame
                expected = bgr_pixels(frame)[:, :, ::-1]
                error = verify_pixels(view, screen_image(app, view), expected, stream_sample_points(expected))
                client.mailbox.set_notifier(view.frame_ready.emit)
                width, height = frame.width, frame.height
            else:
                client.mailbox.set_notifier(None)
                settle(app)
                width, height = view.image.width(), view.image.height()
                expected = view.image_pixels[:, :, ::-1]
                error = verify_pixels(view, screen_image(app, view), expected, stream_sample_points(expected))
                client.mailbox.set_notifier(view.frame_ready.emit)
            if renderer == 'gpu' and scale != 100:
                algorithm = client.profile.snapshot()['metadata'].get('upscaler')
                assert algorithm == ('RTX-VSR' if upscaler == 'rtx' else 'FSR1-EASU-RCAS'), algorithm
            from elink.core.scaling import encoding_size
            assert (width, height) == encoding_size(client.config)
            results.append(dict(resolution=size, fps=fps, decoder=decoder, renderer=renderer,
                                render_scale=scale, audio=audio, same_session=True, native_rgb_error=error))
        return results
    finally:
        stopped = window.runtime.submit(host.stop())
        wait(stopped.done)
        stopped.result()
        window.close()
        wait(lambda: window._closed)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.artifacts/gpu-video-check.json'))
    parser.add_argument('--frames', type=int, default=120)
    parser.add_argument('--stream-check', action='store_true',
                        help='Also verify real local WebRTC video with software H.264 and D3D11VA')
    args = parser.parse_args(argv)
    if args.frames < 1:
        parser.error('--frames must be positive')
    os.environ['QT_QPA_PLATFORM'] = 'windows'
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    result = dict(colors=verify_colors(app), benchmarks=[benchmark(app, mode, args.frames, 1920, 1080)
                                                      for mode in ('cpu', 'gpu')],
                  super_resolution={str(scale): verify_colors(app, scale) for scale in (75, 67, 50)},
                  rtx_colors=verify_colors(app, 50, 'rtx'),
                  rtx_fallback=verify_rtx_fallback(app),
                  limitations='Synthetic CPU submission benchmark, not game FPS, GPU time or input-to-photon latency.')
    if args.stream_check:
        from scripts.check_gpu_capture import verify as verify_capture, verify_live_capture
        result['native_capture'] = verify_capture(app)
        result['live_capture'] = verify_live_capture(app)
        result['decoder_colors'] = verify_decoder_colors(app)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='gpu-stream-', dir=args.output.parent) as temp:
            result['streams'] = [verify_stream(app, Path(temp) / decoder, decoder)
                                 for decoder in ('software', 'hardware')]
            result['super_resolution_streams'] = [verify_stream(app, Path(temp) / str(scale), 'hardware', scale)
                                                   for scale in (75, 67, 50)]
            result['rtx_stream'] = verify_stream(app, Path(temp) / 'rtx', 'hardware', 50, 'rtx')
            result['live_settings'] = verify_live_settings(app, Path(temp) / 'live')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
