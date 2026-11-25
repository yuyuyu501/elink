import unittest
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from frontend.desktop import settings
from frontend.desktop.service import ViewerSettings


class DeviceNameSettingsTests(unittest.TestCase):
    def test_custom_name_round_trips_with_whitespace_trimmed(self):
        with TemporaryDirectory() as folder, patch.object(
            settings, "settings_file", return_value=Path(folder) / "config" / "desktop.json"
        ):
            host, viewer = settings.load()
            self.assertTrue(settings.save(host, viewer, settings.UiSettings(device_name="  工作电脑  ")))
            self.assertEqual(settings.load_ui().device_name, "工作电脑")

    def test_old_and_invalid_values_use_default_name(self):
        for values in ({}, {"device_name": None}, {"device_name": 9},
                       {"device_name": "  "}, {"device_name": "x" * 81}):
            with self.subTest(values=values), patch.object(settings, "_read", return_value={"ui": values}):
                self.assertEqual(settings.load_ui().device_name, "")
        self.assertEqual(settings.normalize_device_name("x" * 80), "x" * 80)


class DevicePictureSettingsTests(unittest.TestCase):
    def test_volume_migrates_legacy_mute(self):
        for raw, expected in (({"muted": True}, 0), ({"muted": True, "volume": 35}, 35), ({}, 100)):
            with patch.object(settings, "_read", return_value={"viewer": raw}):
                self.assertEqual(settings.load()[1].volume, expected)

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
