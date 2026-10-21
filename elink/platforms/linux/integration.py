"""Current-user desktop integration; never edits system-wide permissions."""
import os
from pathlib import Path
import sys


def desktop_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'


def set_startup(enabled):
    directory = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'autostart'
    path = directory / 'elink.desktop'
    if enabled:
        directory.mkdir(parents=True, exist_ok=True)
        arguments = ([sys.executable] if getattr(sys, 'frozen', False)
                     else [sys.executable, '-m', 'elink.app']) + ['--startup']
        path.write_text('[Desktop Entry]\nType=Application\nName=Elink\nTerminal=false\nExec='
                        + ' '.join(desktop_quote(a) for a in arguments) + '\n', encoding='utf-8')
    else:
        path.unlink(missing_ok=True)
