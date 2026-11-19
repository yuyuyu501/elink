"""Windows elevation with an explicit handoff of the existing instance."""

import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import subprocess
import sys


def is_elevated():
    return os.name == "nt" and bool(C.windll.shell32.IsUserAnAdmin())


def restart_elevated(data_root):
    executable = str(Path(sys.executable).resolve())
    arguments = [] if getattr(sys, "frozen", False) else [
        str(Path(__file__).resolve().parents[2] / "main.py")]
    arguments += ["--ui", "--elevated-restart", str(os.getpid()), str(Path(data_root).resolve())]
    if "--startup" in sys.argv:
        arguments.append("--startup")
    shell = C.WinDLL("shell32", use_last_error=True)
    shell.ShellExecuteW.argtypes = [W.HWND, W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, W.LPCWSTR, C.c_int]
    shell.ShellExecuteW.restype = C.c_void_p
    result = shell.ShellExecuteW(None, "runas", executable, subprocess.list2cmdline(arguments),
                                str(Path(executable).parent), 1)
    if not result or result <= 32:
        raise OSError("管理员重启未获批准，当前应用继续运行。")


def wait_for_previous(pid, timeout_ms=30000):
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    kernel.WaitForSingleObject.restype = W.DWORD
    kernel.CloseHandle.argtypes = [W.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if not handle:
        if C.get_last_error() == 87:
            return
        raise C.WinError(C.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle, timeout_ms) != 0:
            raise TimeoutError("原 Elink 尚未退出，管理员实例未能接管。")
    finally:
        kernel.CloseHandle(handle)
