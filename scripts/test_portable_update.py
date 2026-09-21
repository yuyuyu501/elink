"""Exercise a real frozen EXE directory replacement in isolated temporary folders."""
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from elink import __version__
from elink.paths import resource_dir
from elink.processes import native_environment


def app_windows(executable):
    """Return only windows owned by this exact temporary executable path."""
    user, kernel = C.WinDLL('user32'), C.WinDLL('kernel32')
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
    user.EnumWindows.argtypes = [callback_type, W.LPARAM]
    user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(W.DWORD)]
    user.IsWindowVisible.argtypes = [W.HWND]
    user.IsWindowVisible.restype = W.BOOL
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    matches = []

    @callback_type
    def visit(hwnd, _):
        if not user.IsWindowVisible(hwnd):
            return True
        pid = W.DWORD()
        user.GetWindowThreadProcessId(hwnd, C.byref(pid))
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if handle:
            try:
                buffer, length = C.create_unicode_buffer(32768), W.DWORD(32768)
                if kernel.QueryFullProcessImageNameW(handle, 0, buffer, C.byref(length)):
                    if Path(buffer.value).resolve() == executable.resolve():
                        matches.append((hwnd, pid.value))
            finally:
                kernel.CloseHandle(handle)
        return True
    user.EnumWindows(visit, 0)
    return matches


def close_app(executable, wait=False):
    user = C.WinDLL('user32')
    kernel = C.WinDLL('kernel32')
    user.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    kernel.WaitForSingleObject.restype = W.DWORD
    kernel.CloseHandle.argtypes = [W.HANDLE]
    windows = app_windows(executable)
    handles = [kernel.OpenProcess(0x100000, False, pid) for pid in {pid for _, pid in windows}] if wait else []
    try:
        for hwnd, _ in windows:
            user.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: normal Qt cleanup.
        for handle in handles:
            if handle and kernel.WaitForSingleObject(handle, 20000) != 0:
                raise RuntimeError('Updated application did not exit normally.')
    finally:
        for handle in handles:
            if handle:
                kernel.CloseHandle(handle)


def wait_for(predicate, message, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise RuntimeError(message)


def verify_portable_update(bundle, root):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    target = root / "Elink portable 中文 [1]"
    workspace = root / '.Elink-update-smoke'
    payload = workspace / 'payload' / 'Elink'
    data = root / 'userdata'
    data.mkdir()
    (data / 'desktop-v3.json').write_text(json.dumps({'address': '127.0.0.1:49200', 'update_previews': False}), encoding='utf-8')
    sentinel = data / 'preserved-user-data.txt'
    sentinel.write_text('must survive the update', encoding='utf-8')
    shutil.copytree(bundle, target)
    shutil.copytree(bundle, payload)
    executable = target / 'Elink.exe'
    old_ready = root / 'old-ready'
    environment = native_environment()
    environment.update(ELINK_DATA_DIR=str(data), ELINK_UPDATE_READY=str(old_ready), PYINSTALLER_RESET_ENVIRONMENT='1')
    old = subprocess.Popen([str(executable)], cwd=root, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    helper = None
    try:
        wait_for(lambda: old_ready.exists() or old.poll() is not None, 'Frozen app did not start.')
        if old.poll() is not None or not old_ready.exists():
            raise RuntimeError('Frozen app exited before startup confirmation.')
        plan = workspace / 'plan.json'
        plan.write_text(json.dumps(dict(target=str(target), workspace=str(workspace),
                                       parent_pid=old.pid, data_root=str(data))), encoding='utf-8')
        # Test the helper actually shipped inside the frozen application.
        script = workspace / 'apply-update.ps1'
        shipped = bundle / '_internal/elink/resources/apply-update.ps1'
        if shipped.read_bytes() != (resource_dir() / 'apply-update.ps1').read_bytes():
            raise RuntimeError('Packaged update helper differs from source.')
        shutil.copy2(shipped, script)
        powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        helper = subprocess.Popen([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                   '-File', str(script), '-PlanPath', str(plan)], cwd=workspace,
                                  env=native_environment(), creationflags=subprocess.CREATE_NO_WINDOW)
        wait_for(lambda: (workspace / 'helper-ready').exists() or helper.poll() is not None,
                 'Packaged update helper did not start.', timeout=20)
        if helper.poll() is not None:
            raise RuntimeError('Packaged update helper failed before hand-off.')
        (workspace / 'armed').write_text('ready', encoding='ascii')
        close_app(executable)
        old.wait(timeout=20)
        if helper.wait(timeout=55) != 0:
            raise RuntimeError('Frozen replacement failed: ' + (workspace / 'update.log').read_text(encoding='utf-8-sig'))
        if (workspace / 'app-ready').read_text(encoding='utf-8') != __version__:
            raise RuntimeError('Restarted app reported an unexpected version.')
        if not (workspace / 'previous/Elink.exe').is_file():
            raise RuntimeError('Updater did not retain the previous program.')
        saved = json.loads((data / 'desktop-v3.json').read_text(encoding='utf-8'))
        if saved['address'] != '127.0.0.1:49200' or saved['update_previews'] is not False or sentinel.read_text() != 'must survive the update':
            raise RuntimeError('User configuration was not preserved.')
        close_app(executable, wait=True)
        wait_for(lambda: not app_windows(executable), 'Updated app did not close normally.', timeout=15)
        return dict(ok=True, frozen=True, directory_replacement=True, restart_confirmed=True,
                    configuration_preserved=True, backup_retained=True,
                    note='Same-version replacement using two isolated copies; not a manual game test.')
    finally:
        close_app(executable)
        if old.poll() is None:
            try:
                old.wait(timeout=10)
            except subprocess.TimeoutExpired:
                old.kill()  # Only this test's owned process.
                old.wait(timeout=5)
        if helper is not None and helper.poll() is None:
            helper.kill()
            helper.wait(timeout=5)
