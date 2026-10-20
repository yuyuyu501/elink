import sys
from types import SimpleNamespace

import av
import numpy as np
import pytest

from elink.core import media, gpu_capture
from elink.core.gpu_capture import calibrated_color_space


@pytest.mark.parametrize('space,name', [(1, 'ITU709'), (5, 'ITU601')])
@pytest.mark.parametrize('color_range', [1, 2])
def test_calibration_identifies_matrix_and_range_on_texture(space, name, color_range):
    rng = np.random.default_rng(48)
    pixels = np.repeat(np.repeat(rng.integers(20, 230, (24, 32, 3), dtype=np.uint8), 8, 0), 8, 1)
    reference = av.VideoFrame.from_ndarray(pixels, format='rgb24')
    frame = reference.reformat(format='nv12', dst_colorspace=name, dst_color_range=color_range)
    assert calibrated_color_space(frame, reference) == (space, color_range)


def test_calibration_rejects_gray_but_uses_small_colored_regions():
    pixels = np.full((720, 1280, 3), 90, np.uint8)
    reference = av.VideoFrame.from_ndarray(pixels.copy(), format='rgb24')
    with pytest.raises(RuntimeError, match='calibration'):
        calibrated_color_space(reference.reformat(format='nv12'), reference)
    pixels[100:200, 100:200] = (230, 30, 20)
    pixels[200:300, 200:300] = (20, 215, 35)
    reference = av.VideoFrame.from_ndarray(pixels, format='rgb24')
    frame = reference.reformat(format='nv12', dst_colorspace='ITU709', dst_color_range=2)
    assert calibrated_color_space(frame, reference) == (1, 2)


def test_native_cursor_reconfiguration_and_worker_cleanup(monkeypatch):
    captures = []
    class Capture:
        def __init__(self, width, height, draw_mouse):
            self.draw_mouse, self.closed = draw_mouse, False
            captures.append(self)
        def read(self):
            return av.VideoFrame(64, 36, 'nv12')
        def close(self):
            self.closed = True
    monkeypatch.setattr(gpu_capture, 'D3D11Capture', Capture)
    from elink.core import cursor
    monkeypatch.setattr(cursor, 'WindowsCursor', lambda: SimpleNamespace(visible=lambda: True))
    track = media.DesktopTrack(64, 36, 60)
    track.gpu_capture_disabled = False
    try:
        assert track.capture().format.name == 'nv12'
        track.cursor_mode = 'smart'
        track.capture()  # Smart and remote both composite the host cursor.
        assert len(captures) == 1
        track.cursor_mode = 'local'
        track.capture()
        assert captures[0].closed and len(captures) == 2 and not captures[1].draw_mouse
        assert track.cursor_visible is True
    finally:
        track.stop()
        track.release_future.result(5)
    assert captures[1].closed and track.gpu_capture is None


def test_native_failure_falls_back_once_and_forced_mode_reports_failure(monkeypatch, caplog):
    attempts, notices = [], []
    def fail(*args):
        attempts.append(True)
        raise RuntimeError('unsupported video processor')
    monkeypatch.setattr(gpu_capture, 'D3D11Capture', fail)
    camera = SimpleNamespace(start=lambda **kw: None, release=lambda: None,
                             get_latest_frame=lambda: np.zeros((36, 64, 4), np.uint8),
                             _output=SimpleNamespace(desc=SimpleNamespace(
                                 DesktopCoordinates=SimpleNamespace(left=0, top=0))))
    monkeypatch.setitem(sys.modules, 'dxcam', SimpleNamespace(create=lambda **kw: camera))
    from elink.core import cursor
    monkeypatch.setattr(cursor, 'WindowsCursor', lambda: SimpleNamespace(visible=lambda: False))
    track = media.DesktopTrack(64, 36, 60)
    track.gpu_capture_disabled = False
    track.com_initialized = True  # No COM calls or real desktop access in this test.
    track.cursor_mode = 'local'
    track.notify = notices.append
    try:
        track.capture()
        track.capture()
        assert attempts == [True] and len(notices) == 1
        assert track.gpu_capture_disabled
        assert track.profile.snapshot()['metadata']['capture_backend'] == 'dxcam'
        failures = track.profile.snapshot()['failures']
        assert len(failures) == 1
        assert failures[0]['type'] == 'RuntimeError'
        assert failures[0]['message'] == 'unsupported video processor'
        assert 'raise RuntimeError' in failures[0]['traceback']
        assert failures[0]['context'] == dict(width=64, height=36, fps=60,
                                             requested_backend='auto', draw_mouse=False)
        assert 'unsupported video processor' in caplog.text
    finally:
        track.com_initialized = False
        track.stop()
        track.release_future.result(5)
    track = media.DesktopTrack(64, 36, 60, capture_backend='gpu')
    track.gpu_capture_disabled = False
    try:
        with pytest.raises(RuntimeError, match='unsupported'):
            track.capture()
        assert track.camera is None
        assert track.profile.snapshot()['failures'][0]['context']['requested_backend'] == 'gpu'
    finally:
        track.stop()
        track.release_future.result(5)


@pytest.mark.parametrize('stage', ['calibration_graph', 'calibration_frames', 'calibration_colors', 'capture_graph'])
def test_native_failure_reports_exact_initialization_stage(monkeypatch, stage):
    def build(capture, reference=False):
        if stage == ('calibration_graph' if reference else 'capture_graph'):
            raise RuntimeError('graph unavailable')
        video = av.VideoFrame(64, 36, 'nv12')
        video.pts = 1
        rgb = av.VideoFrame(64, 36, 'bgra')
        rgb.pts = 2 if stage == 'calibration_frames' else 1
        capture.sink = SimpleNamespace(pull=lambda: video)
        capture.reference_sink = SimpleNamespace(pull=lambda: rgb)
    def calibrate(*args):
        if stage == 'calibration_colors':
            raise RuntimeError('not enough colors')
        return 1, 1
    monkeypatch.setattr(gpu_capture.D3D11Capture, '_build', build)
    monkeypatch.setattr(gpu_capture, 'calibrated_color_space', calibrate)
    with pytest.raises(RuntimeError) as raised:
        gpu_capture.D3D11Capture(64, 36, False)
    assert any(f'stage={stage}' in note and 'output=64x36' in note for note in raised.value.__notes__)


def test_gray_start_continues_with_correct_colors_and_recovers_gpu_in_same_capture(monkeypatch):
    source = av.VideoFrame.from_ndarray(np.full((192, 256, 3), 90, np.uint8), format='rgb24')
    source.pts = 1
    builds = []
    def gpu_frame():
        return source.reformat(format='nv12', dst_colorspace='ITU709', dst_color_range=2)
    def build(capture, reference=False):
        builds.append(reference)
        capture.sink = SimpleNamespace(pull=gpu_frame)
        capture.reference_sink = SimpleNamespace(pull=lambda: source) if reference else None
    monkeypatch.setattr(gpu_capture.D3D11Capture, '_build', build)
    capture = gpu_capture.D3D11Capture(256, 192, False)
    try:
        frame = capture.read()
        assert capture.calibration_pending and builds == [True]
        assert (int(frame.colorspace), int(frame.color_range)) == (1, 1)
        assert np.max(np.abs(media.bgr_pixels(frame).astype(int) - 90)) <= 2
        rng = np.random.default_rng(48)
        pixels = np.repeat(np.repeat(rng.integers(20, 230, (24, 32, 3), dtype=np.uint8), 8, 0), 8, 1)
        source = av.VideoFrame.from_ndarray(pixels, format='rgb24')
        source.pts = 2
        capture.next_calibration_at = 0
        frame = capture.read()
        assert not capture.calibration_pending and builds == [True, False]
        assert (int(frame.colorspace), int(frame.color_range)) == (1, 2)
        actual = media.bgr_pixels(frame)[4::8, 4::8, ::-1].astype(int)
        assert np.max(np.abs(actual - pixels[4::8, 4::8].astype(int))) <= 4
        assert capture.read().format.name == 'nv12'
    finally:
        capture.close()
