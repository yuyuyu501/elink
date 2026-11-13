"""Super-resolution state and dimensions without requiring a graphics adapter."""

import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication
from frontend.desktop.connection import picture_settings, stream_picture, output_picture
from frontend.desktop.service import ViewerSettings, SessionService
from frontend.desktop.settings import load
from frontend.desktop.upscale import RtxUpscaler, UpscaleProcessor, probe_native
from backend.media.video.filter.scaler import Scaler
from backend.media.frame import Frame, PixelFormat

APP = QCoreApplication.instance() or QCoreApplication([])
PICTURE = {"width": 2560, "height": 1440, "fps": 120, "bitrate_kbps": 12000}


class UpscaleTests(unittest.TestCase):
    def test_downscale_preserves_previous_nearest_neighbor_sampling(self):
        width, height = 14, 10
        source = bytes(range(140)) * 4
        frame = Frame(data=source, width=width, height=height, pixel_format=PixelFormat.BGRA)
        for target_width, target_height in ((8, 6), (4, 2), (20, 16)):
            expected = bytearray()
            for y in range(target_height):
                for x in range(target_width):
                    index = (int(y * (height / target_height)) * width
                             + int(x * (width / target_width))) * 4
                    expected.extend(source[index:index + 4])
            result = Scaler(target_width, target_height).process(frame)
            self.assertEqual(result.data, bytes(expected))

    def test_ratio_dimensions_are_even_and_target_is_preserved(self):
        for ratio, size in ((33, (844, 474)), (50, (1280, 720)), (100, (2560, 1440))):
            with self.subTest(ratio=ratio):
                wire = stream_picture(PICTURE, ViewerSettings(sr_enabled=True, sr_percent=ratio))
                self.assertEqual((wire["width"], wire["height"]), size)
                self.assertEqual(output_picture(wire), PICTURE)
                self.assertEqual(wire["bitrate_kbps"], 12000)
        self.assertEqual(stream_picture(PICTURE, ViewerSettings()), PICTURE)

    def test_small_wire_dimensions_and_invalid_options(self):
        wire = stream_picture({**PICTURE, "width": 320, "height": 240},
                              ViewerSettings(sr_enabled=True, sr_percent=33))
        self.assertEqual((wire["width"], wire["height"]), (104, 78))
        for update in ({"percent": 32}, {"mode": "unknown"}, {"sharpness": 101},
                       {"sharpen": 1}, {"width": 321}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                picture_settings({**wire, "upscale": {**wire["upscale"], **update}})

    def test_old_settings_ignore_old_output_and_sanitize_preferences(self):
        with patch("frontend.desktop.settings._read", return_value={
            "viewer": {"sr_output": "4K", "stream_width": 2560, "stream_height": 1440,
                       "sr_percent": 10, "sr_sharpness": "bad", "sr_mode": "invalid"}}):
            _, viewer = load()
        self.assertEqual(viewer.stream_width, 2560)
        self.assertEqual(viewer.sr_percent, 33)
        self.assertEqual(viewer.sr_sharpness, 50)
        self.assertEqual(viewer.sr_mode, "fsr")

    def test_rtx_failure_is_retried(self):
        class Processor(RtxUpscaler):
            attempts = 0
            def __init__(self, size):
                type(self).attempts += 1
            def upscale(self, frame):
                if self.attempts == 1:
                    raise RuntimeError("temporary driver failure")
            def close(self):
                pass
        self.assertFalse(Processor.available())
        self.assertTrue(Processor.available())
        self.assertEqual(Processor.attempts, 2)

    def test_runtime_state_does_not_claim_rtx_enhancement(self):
        class Processor:
            effect = 0
            def __init__(self, size):
                pass
            def upscale(self, frame):
                return frame
            def close(self):
                pass
        states = []
        frame = Frame(data=b"\0\0\0\xff" * 4, width=2, height=2, pixel_format=PixelFormat.BGRA)
        with patch("frontend.desktop.upscale.RtxUpscaler", Processor):
            processor = UpscaleProcessor("rtx", (1280, 720), notify=lambda *v: states.append(v))
            self.assertIs(processor.upscale(frame), frame)
            processor.close()
        self.assertEqual(states, [("rtx", "unobserved")])

    def test_cancelled_helper_is_killed_and_waited(self):
        cancel = threading.Event()
        cancel.set()
        with patch("frontend.desktop.upscale.subprocess.Popen") as popen:
            process = popen.return_value.__enter__.return_value
            process.poll.return_value = None
            with self.assertRaises(RuntimeError):
                probe_native(cancel)
            process.kill.assert_called_once()
            process.wait.assert_called_once()

    def test_failed_probe_is_not_cached_and_active_processor_is_not_probed(self):
        service = SessionService()
        with patch("frontend.desktop.upscale.probe_native", side_effect=[
            RuntimeError("driver changed"), {"fsr": {"state": "available"}}]) as probe:
            service.refresh_upscale(True)
            service._upscale_worker.join(3)
            self.assertIn("driver changed", service.upscale_status)
            service.refresh_upscale(True)
            service._upscale_worker.join(3)
            self.assertEqual(service.upscale_capabilities["fsr"]["state"], "available")
            service.refresh_upscale()
            self.assertEqual(probe.call_count, 2)
            with patch.object(SessionService, "running", property(lambda _: True)):
                service._viewer_live = ViewerSettings(sr_enabled=True)
                service.refresh_upscale(True)
            self.assertEqual(probe.call_count, 2)
        service.cancel_upscale_probe()

    def test_100_percent_is_not_reported_as_active_upscaling(self):
        class Processor:
            def __init__(self, size, **kwargs):
                pass
            def upscale(self, frame):
                return frame
            def close(self):
                pass
        frame = Frame(data=b"\0\0\0\xff" * 4, width=2, height=2, pixel_format=PixelFormat.BGRA)
        states = []
        with patch("frontend.desktop.upscale.FsrUpscaler", Processor):
            processor = UpscaleProcessor("fsr", (2, 2), notify=lambda *v: states.append(v))
            processor.upscale(frame)
            processor.close()
        self.assertEqual(states, [("fsr", "bypass")])


if __name__ == "__main__":
    unittest.main()
