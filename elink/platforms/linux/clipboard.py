"""X11 clipboard transactions use xclip; Wayland requires separate capabilities."""
import os
import shutil
import subprocess


def read_text():
    if os.environ.get('WAYLAND_DISPLAY') or not shutil.which('xclip'):
        return None
    try:
        result = subprocess.run(['xclip', '-selection', 'clipboard', '-o'],
                                capture_output=True, timeout=.5)
        return result.stdout[:256000].decode('utf-8', 'replace')[:64000] if not result.returncode else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def write_text(value):
    if os.environ.get('WAYLAND_DISPLAY') or not shutil.which('xclip') or not isinstance(value, str):
        return False
    try:
        process = subprocess.Popen(['xclip', '-selection', 'clipboard', '-i'], stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.communicate(value[:64000].encode('utf-8'), timeout=.5)
        return process.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        if 'process' in locals():
            process.kill()
            process.wait()
        return False
