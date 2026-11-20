import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frontend.desktop import settings
from frontend.desktop.service import ViewerSettings


class DevicePictureSettingsTests(unittest.TestCase):
    def test_devices_use_independent_picture_values(self):
        viewer = ViewerSettings()
        device_a = settings.for_device(viewer, "device-a")
        device_a.stream_width = 1280
        settings.remember_device_picture(viewer, "device-a", device_a)

        self.assertEqual(settings.for_device(viewer, "device-a").stream_width, 1280)
        self.assertEqual(settings.for_device(viewer, "device-b").stream_width, 1920)

    def test_invalid_device_picture_falls_back_to_defaults(self):
        with patch("frontend.desktop.settings._read", return_value={
            "viewer": {
                "picture_preferences": {
                    "device-a": {"width": 321, "height": 1080, "fps": 60,
                                 "bitrate_kbps": 4000}
                }
            }
        }):
            _host, viewer = settings.load()

        self.assertEqual(viewer.picture_preferences, {})
        self.assertEqual(settings.for_device(viewer, "device-a").stream_width, 1920)


if __name__ == "__main__":
    unittest.main()
