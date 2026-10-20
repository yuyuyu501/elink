from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage

from scripts.check_gpu_video import stream_sample_points, verify_pixels


def test_scaled_stream_edge_does_not_cause_false_black_screen_failure():
    rgb = np.zeros((540, 960, 3), dtype=np.uint8)
    rgb[:, :, 1] = 30
    rgb[:, 240:280, 0] = 255
    source = QImage(rgb.data, 960, 540, rgb.strides[0], QImage.Format.Format_RGB888)
    image = source.scaled(930, 523, Qt.AspectRatioMode.IgnoreAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
    view = SimpleNamespace(rect_image=QRect(0, 0, 930, 523), width=lambda: 930)
    with pytest.raises(AssertionError, match='color errors'):
        verify_pixels(view, image, rgb)
    points = stream_sample_points(rgb)
    assert verify_pixels(view, image, rgb, points) <= 5
    image.fill(Qt.GlobalColor.black)
    with pytest.raises(AssertionError, match='color errors'):
        verify_pixels(view, image, rgb, points)
