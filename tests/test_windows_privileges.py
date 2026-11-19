import ctypes
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from frontend.desktop import privileges


class PrivilegeTests(unittest.TestCase):
    def test_restart_preserves_root_and_explicit_ui_route(self):
        execute = Mock(return_value=42)
        with patch.object(ctypes, "WinDLL", return_value=SimpleNamespace(ShellExecuteW=execute)):
            privileges.restart_elevated(Path("out/test-data"))
        args = execute.call_args.args
        self.assertEqual(args[1], "runas")
        self.assertIn("--ui --elevated-restart", args[3])
        self.assertIn(str(Path("out/test-data").resolve()), args[3])

    def test_cancel_keeps_current_instance(self):
        execute = Mock(return_value=5)
        with patch.object(ctypes, "WinDLL", return_value=SimpleNamespace(ShellExecuteW=execute)):
            with self.assertRaises(OSError):
                privileges.restart_elevated(Path("out/test-data"))

    def test_handoff_waits_and_closes_process_handle(self):
        api = SimpleNamespace(OpenProcess=Mock(return_value=17), WaitForSingleObject=Mock(return_value=0),
                              CloseHandle=Mock())
        with patch.object(ctypes, "WinDLL", return_value=api):
            privileges.wait_for_previous(123)
        api.WaitForSingleObject.assert_called_once_with(17, 30000)
        api.CloseHandle.assert_called_once_with(17)


if __name__ == "__main__":
    unittest.main()
