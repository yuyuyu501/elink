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


def player(app, mode, *, statistics=True, render_scale=100):
    client = SimpleNamespace(profile=PipelineProfile(), pc=None, mailbox=FrameMailbox(), rtt_ms=0,
                             last_pong=0, focus=lambda _: None, set_renderer=lambda _: None,
                             send=lambda _: None)
    view = Player(Runtime(), client, controllers=False,
                  preferences={'renderer': mode, 'render_scale': render_scale, 'resolution': [960, 540]})
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


def verify_pixels(view, image, expected):
    rect = view.rect_image
    ratio = image.width() / view.width()
    errors = []
    for nx, ny in ((.25, .25), (.75, .25), (.25, .75), (.75, .75)):
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


def verify_colors(app, render_scale=100):
    view = player(app, 'gpu', render_scale=render_scale)
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
            if render_scale != 100 and surface.fsr is None:
                raise AssertionError('FSR was requested but is not active')
            expected = frame.to_ndarray(format='rgb24', src_colorspace='ITU709' if space == 1 else 'ITU601',
                                        dst_colorspace='ITU601', src_color_range=2 if full else 1)
            # Read native output BEFORE grab(): QWidget.grab renders internally
            # and succeeds even when Qt's on-screen compositor is entirely black.
            native_error = verify_pixels(view, screen_image(app, view), expected)
            error = verify_pixels(view, view.grab().toImage(), expected)
            results.append(dict(format=fmt, colorspace=space, full_range=full,
                                max_rgb_error=error, native_rgb_error=native_error,
                                upscaler='FSR1-EASU-RCAS' if surface.fsr else 'native'))
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


def verify_stream(app, root, decoder, render_scale=100):
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
        view = Player(runtime, client, controllers=False, preferences={'renderer': 'gpu'})
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
        verify_pixels(view, screen_image(app, view), reference[:, :, ::-1])
        image = view.grab().toImage()
        rect, ratio = view.rect_image, surface.devicePixelRatioF()
        for nx, ny in ((.25, .25), (.75, .25), (.25, .75), (.75, .75)):
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
                    upscaler=metadata.get('upscaler', 'native'), fsr_gpu=client.profile.snapshot()['stages'].get('fsr_gpu'))
    finally:
        if view:
            view.close()
            view.deleteLater()
            app.processEvents()
        try:
            runtime.submit(close()).result(15)
        finally:
            runtime.stop()


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
                  super_resolution={str(scale): verify_colors(app, scale) for scale in (75, 67)},
                  limitations='Synthetic CPU submission benchmark, not game FPS, GPU time or input-to-photon latency.')
    if args.stream_check:
        from scripts.check_gpu_capture import verify as verify_capture
        result['native_capture'] = verify_capture(app)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='gpu-stream-', dir=args.output.parent) as temp:
            result['streams'] = [verify_stream(app, Path(temp) / decoder, decoder)
                                 for decoder in ('software', 'hardware')]
            result['super_resolution_streams'] = [verify_stream(app, Path(temp) / str(scale), 'hardware', scale)
                                                   for scale in (75, 67)]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
