"""Check gray-screen calibration recovery and log a real D3D11 resource failure."""
import argparse
import json
import logging
import os
from pathlib import Path
import time
from unittest.mock import patch

import av
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from elink.core.media import DesktopTrack
from elink.core.media import bgr_pixels
from elink.core import gpu_capture
from elink.logging_setup import configure_logging
from elink.storage import atomic_json


class GrayWindow(QWidget):
    colored = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(90, 90, 90))
        if self.colored:
            colors = ((230, 30, 20), (20, 215, 35), (20, 35, 225), (200, 200, 200))
            for index, color in enumerate(colors):
                painter.fillRect(QRect(index % 2 * self.width() // 2, index // 2 * self.height() // 2,
                                       self.width() // 2, self.height() // 2), QColor(*color))


def verify(root):
    # Crop the real Desktop Duplication source to the synthetic window. Some
    # shells keep a colorful taskbar above fullscreen windows, invalidating a
    # neutral-desktop test. All conversion/calibration/fallback code stays real.
    graph_type = av.filter.Graph
    app = QApplication.instance()
    if app is None:
        os.environ['QT_QPA_PLATFORM'] = 'windows'
        app = QApplication([])
    ratio = app.primaryScreen().devicePixelRatio()
    region = f':video_size={round(640 * ratio)}x{round(360 * ratio)}:offset_x={round(40 * ratio)}:offset_y={round(40 * ratio)}'
    class RegionGraph:
        def __init__(self):
            self.graph = graph_type()
        def add(self, name, args=None):
            return self.graph.add(name, args + region if name == 'ddagrab' else args)
        def configure(self):
            self.graph.configure()
    with patch.object(gpu_capture.av.filter, 'Graph', RegionGraph):
        return verify_region(root, app)


def verify_region(root, app):
    filename = configure_logging(root)
    window = GrayWindow()
    window.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    origin = app.primaryScreen().geometry().topLeft()
    window.setGeometry(origin.x() + 40, origin.y() + 40, 640, 360)
    track = DesktopTrack(1280, 720, 120)
    track.cursor_mode = 'local'
    try:
        window.show()
        window.raise_()
        window.activateWindow()
        until = time.perf_counter() + .2
        while time.perf_counter() < until:
            app.processEvents()
            time.sleep(.005)
        frame = track.executor.submit(track.capture).result(20)
        report = track.profile.snapshot()
        assert report['metadata']['capture_backend'] == 'ddagrab-cpu-color', report
        assert report['metadata']['capture_calibration'] == 'pending' and not report['failures']
        assert abs(float(bgr_pixels(frame).mean()) - 90) <= 2
        capture = track.gpu_capture
        window.colored = True
        window.update()
        until = time.monotonic() + 5
        while time.monotonic() < until:
            app.processEvents()
            frame = track.executor.submit(track.capture).result(20)
            if not capture.calibration_pending:
                break
            time.sleep(.02)
        assert track.gpu_capture is capture and not capture.calibration_pending
        assert track.profile.snapshot()['metadata']['capture_backend'] == 'ddagrab-scale_d3d11'
        assert not track.gpu_capture_disabled
        rgb = bgr_pixels(frame)[:, :, ::-1]
        errors = [max(abs(rgb[y, x].astype(int) - expected)) for x, y, expected in
                  ((320, 180, (230, 30, 20)), (960, 180, (20, 215, 35)),
                   (320, 540, (20, 35, 225)), (960, 540, (200, 200, 200)))]
        assert max(errors) <= 6, errors
        recovery = track.profile.snapshot()
        track.stop()
        track.release_future.result(10)

        # NV12 textures cannot have odd dimensions. Exercise a real driver
        # allocation failure, with production fallback and persistent logging.
        track = DesktopTrack(1, 1, 60)
        track.cursor_mode = 'local'
        frame = track.executor.submit(track.capture).result(20)
        report = track.profile.snapshot()
        failure = report['failures'][-1]
        assert report['metadata']['capture_backend'] == 'dxcam'
        assert 'D3D11 stage=' in failure['traceback'] and failure['message']
        text = filename.read_text(encoding='utf-8')
        assert failure['message'] in text and 'D3D11 stage=' in text
        assert frame.format.name == 'yuv420p'
        result = dict(ok=True, gray_start_recovered=True, max_rgb_error=int(max(errors)),
                      recovery=recovery, continued_after_real_failure=True, profile=report,
                      log=str(filename.resolve()),
                      limitation='Controlled reproductions; original peer failure still requires the new logs.')
        atomic_json(root / 'result.json', result)
        return result
    finally:
        track.stop()
        track.release_future.result(10)
        window.close()
        app.processEvents()
        logger = logging.getLogger('elink')
        for handler in list(logger.handlers):
            if getattr(handler, 'baseFilename', None) == str(filename):
                logger.removeHandler(handler)
                handler.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('.artifacts/capture-diagnostics'))
    args = parser.parse_args()
    print(json.dumps(verify(args.output_dir.resolve()), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
