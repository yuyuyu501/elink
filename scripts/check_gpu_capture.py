"""Check native D3D11 capture against known visible colors; save no pixels."""
import argparse
import ctypes
import fractions
import json
import os
from pathlib import Path
import time

import numpy as np
import av
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget

from elink.core.gpu_capture import D3D11Capture
from elink.core.media import bgr_pixels
from elink.core.codecs import CodecPolicy, make_encoder


COLORS = [(230, 30, 20), (20, 215, 35), (20, 35, 225), (200, 200, 200)]


class ColorWindow(QWidget):
    def paintEvent(self, event):
        painter = QPainter(self)
        for index, color in enumerate(COLORS):
            painter.fillRect(QRect((index % 2) * 320, (index // 2) * 180, 320, 180), QColor(*color))


def verify(app):
    window = ColorWindow()
    window.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
    area = app.primaryScreen().availableGeometry()
    window.setGeometry(area.x() + 40, area.y() + 40, 640, 360)
    window.show()
    try:
        until = time.perf_counter() + .2
        while time.perf_counter() < until:
            app.processEvents()
            time.sleep(.005)
        results = []
        for width, height in ((1920, 1080), (1440, 810), (1280, 720)):
            capture = D3D11Capture(width, height, False)
            try:
                frame = capture.read()
                rgb = bgr_pixels(frame)[:, :, ::-1]
                codec = make_encoder('h264_nvenc', width, height, CodecPolicy(), 'nv12',
                                     int(frame.colorspace), int(frame.color_range))
                frame.pts, frame.time_base = 0, fractions.Fraction(1, 90000)
                data = b''.join(bytes(packet) for packet in codec.encode(frame))
                decoder = av.CodecContext.create('h264', 'r')
                decoded = decoder.decode(av.Packet(data))[0]
                decoded_rgb = bgr_pixels(decoded)[:, :, ::-1]
                source_width = ctypes.windll.user32.GetSystemMetrics(0)
                source_height = ctypes.windll.user32.GetSystemMetrics(1)
                ratio = window.devicePixelRatioF()
                errors = []
                for index, expected in enumerate(COLORS):
                    x = round((window.x() + (index % 2) * 320 + 160) * ratio * width / source_width)
                    y = round((window.y() + (index // 2) * 180 + 90) * ratio * height / source_height)
                    actual = rgb[y, x].astype(int)
                    error = int(np.max(np.abs(actual - expected)))
                    error = max(error, int(np.max(np.abs(decoded_rgb[y, x].astype(int) - expected))))
                    if error > 6:
                        raise AssertionError(f'D3D11 {width}x{height}: {actual.tolist()} != {expected}, error={error}')
                    errors.append(error)
                results.append(dict(width=width, height=height, format=frame.format.name,
                                    colorspace=int(frame.colorspace), color_range=int(frame.color_range),
                                    decoded_colorspace=int(decoded.colorspace), decoded_color_range=int(decoded.color_range),
                                    max_rgb_error=max(errors), source_width=source_width, source_height=source_height))
            finally:
                capture.close()
        return results
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('.artifacts/gpu-capture-check.json'))
    args = parser.parse_args()
    os.environ['QT_QPA_PLATFORM'] = 'windows'
    app = QApplication.instance() or QApplication([])
    result = verify(app)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
