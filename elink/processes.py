from __future__ import annotations

import os
import ctypes
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path


_launch_lock = threading.RLock()


@contextmanager
def native_launch():
    # PyInstaller's DLL search directory must not leak into independent Qt engines.
    with _launch_lock:
        frozen = os.name == "nt" and getattr(sys, "frozen", False)
        if frozen:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetDllDirectoryW.argtypes = [ctypes.c_wchar_p]
            kernel.SetDllDirectoryW.restype = ctypes.c_int
            kernel.SetDllDirectoryW(None)
        try:
            yield
        finally:
            if frozen:
                kernel.SetDllDirectoryW(sys._MEIPASS)


def native_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH", "QML_IMPORT_PATH",
                "QT_QPA_PLATFORM", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    if getattr(sys, "frozen", False):
        internal = Path(sys._MEIPASS).resolve()
        env["PATH"] = os.pathsep.join(part for part in env.get("PATH", "").split(os.pathsep)
                                      if part and not Path(part).resolve().is_relative_to(internal))
    return env


def run_native(executable: Path, arguments: list[str], *, timeout: float = 15,
               cwd: Path | None = None) -> subprocess.CompletedProcess:
    with native_launch():
        process = subprocess.Popen([str(executable), *arguments], cwd=cwd, env=native_environment(),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   encoding="utf-8", errors="replace", shell=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
