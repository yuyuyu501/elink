import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frontend.desktop import updates


class UpdateTests(unittest.TestCase):
    def release(self, version="9.0.0", **flags):
        tag = "v" + version
        return dict(tag_name=tag, assets=[
            dict(name=name, size=100, browser_download_url=f"{updates.RELEASES_URL}/download/{tag}/{name}")
            for name in (f"Elink-{version}-windows-x64.zip", "SHA256SUMS.txt")], **flags)

    def test_select_verified_assets_and_preview_policy(self):
        releases = [self.release(), self.release("10.0.0-beta.1", prerelease=True)]
        self.assertEqual(updates.select_release(releases)["version"], "10.0.0-beta.1")
        self.assertEqual(updates.select_release(releases, include_preview=False)["version"], "9.0.0")
        releases[0]["assets"][0]["browser_download_url"] = "https://example.com/payload"
        self.assertIsNone(updates.select_release(releases, include_preview=False))

    def test_extract_rejects_escape_and_accepts_complete_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "update.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("Elink/../escape", "bad")
            with self.assertRaises(ValueError):
                updates.extract_verified(archive, root / "payload", threading.Event())
            self.assertFalse((root / "escape").exists())
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("Elink/Elink.exe", "test")
                bundle.writestr("Elink/_internal/library", "test")
            result = updates.extract_verified(archive, root / "payload", threading.Event())
            self.assertTrue(result.is_file())

    def test_checksum_failure_and_cancel_leave_no_staging(self):
        import io
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "Elink"
            target.mkdir()
            (target / "Elink.exe").touch()
            release = updates.select_release([self.release()])
            content = b"invalid zip"
            release["size"] = len(content)
            checksum = "0" * 64 + "  " + release["name"]
            with patch.object(updates, "data_dir", return_value=root / "data"), patch.object(
                updates, "read_small", return_value=checksum.encode()), patch.object(
                    updates, "response_for", return_value=io.BytesIO(content)):
                with self.assertRaisesRegex(ValueError, "SHA-256"):
                    updates.prepare_update(release, target, threading.Event(), lambda *_: None)
            self.assertEqual(list(root.glob(".Elink-update-*")), [])
            self.assertEqual(list((root / "data/updates").iterdir()), [])

    def test_update_helper_contains_handoff_and_rollback(self):
        script = (updates.resource_dir() / "apply-update.ps1").read_text(encoding="utf-8")
        for text in ("helper-ready", "armed", "app-ready", "Previous application restored", "-LiteralPath"):
            self.assertIn(text, script)

    def test_installed_application_uses_release_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "unins000.exe").touch()
            with patch.object(updates.sys, "platform", "win32"), patch.object(
                updates.sys, "frozen", True, create=True), patch.object(
                    updates.sys, "executable", str(root / "Elink.exe")):
                self.assertIsNone(updates.installation_dir())

    @unittest.skipUnless(sys.platform == "win32", "Windows update helper")
    def test_helper_replaces_and_rolls_back_isolated_application(self):
        compiler = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        if not compiler.is_file():
            self.skipTest("Windows C# compiler unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "stub.cs"
            source.write_text(
                'using System; using System.IO; using System.Threading; '
                'class Stub { static void Main() { '
                'string dir = AppDomain.CurrentDomain.BaseDirectory; '
                'File.WriteAllText(Path.Combine(dir, "pid.txt"), '
                'System.Diagnostics.Process.GetCurrentProcess().Id.ToString()); '
                'if (File.Exists(Path.Combine(dir, "fail"))) return; '
                'string ready = Environment.GetEnvironmentVariable("ELINK_UPDATE_READY"); '
                'if (!String.IsNullOrEmpty(ready)) File.WriteAllText(ready, "ok"); '
                'Thread.Sleep(60000); } }', encoding="ascii")
            executable = root / "stub.exe"
            subprocess.run([str(compiler), "/nologo", "/target:winexe",
                            f"/out:{executable}", str(source)], check=True, capture_output=True)
            for fail in (False, True):
                with self.subTest(fail=fail):
                    case = root / str(fail)
                    target = case / "Elink"
                    target.mkdir(parents=True)
                    shutil.copy2(executable, target / "Elink.exe")
                    (target / "original").touch()
                    workspace = Path(tempfile.mkdtemp(prefix=".Elink-update-", dir=case))
                    payload = workspace / "payload/Elink"
                    payload.mkdir(parents=True)
                    shutil.copy2(executable, payload / "Elink.exe")
                    if fail:
                        (payload / "fail").touch()
                    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"])
                    try:
                        with patch.object(updates.os, "getpid", return_value=parent.pid):
                            helper = updates.launch_helper(workspace, target, case / "data")
                        (workspace / "armed").touch()
                        parent.wait(timeout=5)
                        log = workspace / "update.log"
                        expected = "Previous application restored" if fail else "Update successful"
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            text = log.read_text(encoding="utf-8-sig") if log.exists() else ""
                            if expected in text and (target / "pid.txt").exists():
                                break
                            time.sleep(.05)
                        self.assertIn(expected, text)
                        helper.wait(timeout=5)
                        self.assertEqual((target / "original").exists(), fail)
                        if not fail:
                            self.assertTrue((workspace / "app-ready").is_file())
                    finally:
                        if parent.poll() is None:
                            parent.terminate()
                            parent.wait(timeout=5)
                        for path in case.rglob("pid.txt"):
                            pid = int(path.read_text())
                            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


if __name__ == "__main__":
    unittest.main()
