"""Trigger real gray-screen calibration fallback and verify persistent diagnostics."""
import argparse
import json
import os
from pathlib import Path
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from elink.core.media import DesktopTrack
from elink.logging_setup import configure_logging
from elink.storage import atomic_json


class GrayWindow(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(90, 90, 90))


def verify(root):
    os.environ['QT_QPA_PLATFORM'] = 'windows'
    app = QApplication.instance() or QApplication([])
    filename = configure_logging(root)
    window = GrayWindow()
    window.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    window.setGeometry(app.primaryScreen().geometry())
    track = DesktopTrack(1280, 720, 120)
    track.cursor_mode = 'local'
    try:
        window.show()
        until = time.perf_counter() + .2
        while time.perf_counter() < until:
            app.processEvents()
            time.sleep(.005)
        frame = track.executor.submit(track.capture).result(20)
        report = track.profile.snapshot()
        failure = report['failures'][-1]
        assert report['metadata']['capture_backend'] == 'dxcam'
        assert failure['type'] == 'RuntimeError'
        assert 'colored_blocks=0' in failure['message']
        assert 'stage=calibration_colors' in failure['traceback']
        text = filename.read_text(encoding='utf-8')
        assert failure['message'] in text and 'stage=calibration_colors' in text
        assert frame.format.name == 'yuv420p'
        result = dict(ok=True, continued_after_fallback=True, profile=report, log=str(filename.resolve()),
                      limitation='Controlled gray-screen reproduction; not proof of the reported peer failure cause.')
        atomic_json(root / 'result.json', result)
        return result
    finally:
        track.stop()
        track.release_future.result(10)
        window.close()
        app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('.artifacts/capture-diagnostics'))
    args = parser.parse_args()
    print(json.dumps(verify(args.output_dir.resolve()), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
