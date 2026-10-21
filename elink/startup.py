"""Current-user Windows startup registration for portable and source builds."""
import os
from pathlib import Path
import subprocess
import sys


RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE = 'Elink'


def startup_command():
    executable = Path(sys.executable).resolve()
    if getattr(sys, 'frozen', False):
        arguments = [str(executable)]
    else:
        windowed = executable.with_name('pythonw.exe')
        arguments = [str(windowed if windowed.is_file() else executable),
                     str(Path(__file__).resolve().parents[1] / 'main.py')]
    return subprocess.list2cmdline([*arguments, '--startup'])


def set_enabled(enabled):
    if sys.platform.startswith('linux'):
        from .platforms.linux.integration import set_startup
        return set_startup(enabled)
    if os.name != 'nt':
        raise OSError('开机自启仅支持 Windows。')
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
