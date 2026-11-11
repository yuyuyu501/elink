"""Packaging must reject incomplete dependencies and abnormal process exits."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.media.video.codec.probe import resolve_ffmpeg_path
from scripts.build import _copy_ffmpeg, _ffmpeg_source
from scripts.package_windows import run_self_test


class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.exe = self.root / "Elink.exe"
        self.ffmpeg = self.root / "ffmpeg" / "ffmpeg.exe"
        self.ffmpeg.parent.mkdir()
        self.ffmpeg.touch()

    def report(self, **changes):
        report = {
            "ok": True, "frozen": True,
            "ffmpeg": {"path": str(self.ffmpeg), "has_libx264": True},
            "media": {"decoded_frames": 12},
        }
        report.update(changes)
        (self.root / "self-test.json").write_text(json.dumps(report), encoding="utf-8")

    def process(self, returncode=0, **changes):
        def run(args, **kwargs):
            if "-protocols" in args:
                return subprocess.CompletedProcess(args, 0, "Input:\nsrt\nOutput:\nsrt", "")
            self.assertNotIn(str(self.ffmpeg.parent), kwargs["env"]["PATH"].split(os.pathsep))
            self.assertNotEqual(kwargs["env"]["PATH"], os.environ["PATH"])
            self.assertTrue(Path(kwargs["env"]["ELINK_DATA_DIR"]).is_dir())
            self.assertEqual(kwargs["cwd"], kwargs["env"]["ELINK_DATA_DIR"])
            self.report(**changes)
            return subprocess.CompletedProcess(args, returncode, "", "")
        return run

    def test_success_requires_bundled_dependency_and_isolation(self):
        with patch("scripts.package_windows.subprocess.run", side_effect=self.process()):
            report = run_self_test(self.exe)
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(report["system_ffmpeg_excluded"])

    def test_success_report_cannot_hide_crash(self):
        with patch("scripts.package_windows.subprocess.run", side_effect=self.process(-1073741819)):
            with self.assertRaisesRegex(RuntimeError, "-1073741819"):
                run_self_test(self.exe)

    def test_external_ffmpeg_report_rejected(self):
        with patch("scripts.package_windows.subprocess.run", side_effect=self.process(
            ffmpeg={"path": "C:/external/ffmpeg.exe", "has_libx264": True}
        )):
            with self.assertRaisesRegex(RuntimeError, "bundled"):
                run_self_test(self.exe)

    def test_missing_dependency_rejected(self):
        self.ffmpeg.unlink()
        with self.assertRaisesRegex(RuntimeError, "missing"):
            run_self_test(self.exe)

    def test_srt_required(self):
        with patch("scripts.package_windows.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "http", "")):
            with self.assertRaisesRegex(RuntimeError, "SRT"):
                run_self_test(self.exe)

    def test_runtime_ignores_old_path_only_when_frozen(self):
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "platform", "win32"), patch.object(sys, "executable", str(self.exe)):
            self.assertEqual(resolve_ffmpeg_path("C:/old/ffmpeg.exe"), str(self.ffmpeg))
        self.assertEqual(resolve_ffmpeg_path("custom-ffmpeg"), "custom-ffmpeg")

    def test_launcher_rejected(self):
        with patch.dict(os.environ, {"ELINK_FFMPEG_DIR": str(self.ffmpeg.parent)}):
            with self.assertRaisesRegex(RuntimeError, "launcher"):
                _ffmpeg_source()

    def test_bundle_includes_dlls_and_notices(self):
        source = self.root / "distribution" / "bin"
        source.mkdir(parents=True)
        (source / "ffmpeg.exe").touch()
        (source / "avcodec.dll").touch()
        (source.parent / "LICENSE").write_text("License", encoding="ascii")
        (source.parent / "README.txt").write_text("Build information", encoding="ascii")
        output = self.root / "output"
        _copy_ffmpeg(source, output)
        for name in ("ffmpeg.exe", "avcodec.dll", "LICENSE", "README.txt"):
            self.assertTrue((output / "ffmpeg" / name).is_file())

    def test_bundle_requires_license(self):
        with self.assertRaisesRegex(RuntimeError, "license"):
            _copy_ffmpeg(self.ffmpeg.parent, self.root / "output")


if __name__ == "__main__":
    unittest.main()
