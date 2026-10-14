"""Local synthetic OpenGL color/stride checks and CPU/GPU presentation benchmark.

Run on a Windows desktop, not offscreen CI. No desktop capture or input injection.
Timings include CPU submission, not GPU completion or physical scanout.
"""
import argparse
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace

import av
import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from elink.core.media import FrameMailbox
from elink.core.media import bgr_pixels
from elink.core.profiling import PipelineProfile
from elink.ui.player import Player


class Runtime(QObject):
    feedback = Signal(object)

    def call(self, fn, *args):
        fn(*args)


def player(app, mode):
    client = SimpleNamespace(profile=PipelineProfile(), pc=None, mailbox=FrameMailbox(), rtt_ms=0,
                             last_pong=0, focus=lambda _: None, set_renderer=lambda _: None,
                             send=lambda _: None)
    view = Player(Runtime(), client, controllers=False, preferences={'renderer': mode})
    view.timer.stop()
    view.show_statistics = False
    view.show()
    app.processEvents()
    return view


def settle(app, seconds=.1):
    until = time.perf_counter() + seconds
    while time.perf_counter() < until:
        app.processEvents()
        time.sleep(.001)


def verify_colors(app):
    view = player(app, 'gpu')
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
            view.resize(930, 650)
            surface = view.gpu_surface
            if surface is None:
                raise RuntimeError('GPU renderer fell back before color validation')
            surface.set_frame(frame)
            settle(app)
            if surface.broken or not surface.isValid() or view.gpu_surface is None:
                raise RuntimeError('GPU renderer unavailable: ' + view.error_label.text())
            image = surface.grabFramebuffer()
            rect, ratio = view.rect_image, surface.devicePixelRatioF()
            expected = frame.to_ndarray(format='rgb24', src_colorspace='ITU709' if space == 1 else 'ITU601',
                                        dst_colorspace='ITU601', src_color_range=2 if full else 1)
            errors = []
            for nx, ny in ((.25, .25), (.75, .25), (.25, .75), (.75, .75)):
                actual = image.pixelColor(round((rect.x() + nx * rect.width()) * ratio),
                                          round((rect.y() + ny * rect.height()) * ratio)).getRgb()[:3]
                reference = expected[int(ny * 362), int(nx * 642)].astype(int)
                errors.append(int(np.max(np.abs(reference - actual))))
            if max(errors) > 5:
                raise AssertionError(f'{fmt}/{space}/{full}: color errors {errors}')
            results.append(dict(format=fmt, colorspace=space, full_range=full, max_rgb_error=max(errors)))
        # Resource reconstruction when moving between a window and fullscreen.
        view.toggle_fullscreen()
        settle(app)
        view.toggle_fullscreen()
        settle(app)
        if view.gpu_surface is None or view.gpu_surface.broken:
            raise AssertionError('GPU context failed during fullscreen transition')
        # Validate retained video, not merely context survival, after recreation.
        surface = view.gpu_surface
        image = surface.grabFramebuffer()
        rect, ratio = view.rect_image, surface.devicePixelRatioF()
        actual = image.pixelColor(round((rect.x() + rect.width() * .25) * ratio),
                                  round((rect.y() + rect.height() * .25) * ratio)).getRgb()[:3]
        if np.max(np.abs(expected[90, 160].astype(int) - actual)) > 5:
            raise AssertionError('Retained frame was lost during fullscreen transition')
        view.show_statistics = True
        view.update()
        settle(app)
    finally:
        view.close()
        view.deleteLater()
        app.processEvents()
    return results


def benchmark(app, mode, frames, width, height):
    view = player(app, mode)
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
            # repaint is synchronous CPU submission. No grabFramebuffer in the
            # timed region: a GPU readback would distort the result.
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.artifacts/gpu-video-check.json'))
    parser.add_argument('--frames', type=int, default=120)
    args = parser.parse_args()
    if args.frames < 1:
        parser.error('--frames must be positive')
    os.environ['QT_QPA_PLATFORM'] = 'windows'
    app = QApplication.instance() or QApplication([])
    result = dict(colors=verify_colors(app), benchmarks=[benchmark(app, mode, args.frames, 1920, 1080)
                                                      for mode in ('cpu', 'gpu')],
                  limitations='Synthetic CPU submission benchmark, not game FPS, GPU time or input-to-photon latency.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
