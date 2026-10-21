import json
import os
from pathlib import Path
import sys

import pytest

from elink import platforms, updates
from elink.core.input_session import InputSession, RecordingInput
from elink.platforms.linux import keyboard, integration


def test_linux_backend_is_selected_without_loading_windows(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    monkeypatch.setenv('XDG_SESSION_TYPE', 'x11')
    backend = platforms.backend()
    assert backend.__name__ == 'elink.platforms.linux.backend'
    assert backend.capabilities()['roles'] == ['controller', 'host']
    assert backend.capabilities()['cursor_modes'] == ['remote']
    monkeypatch.setenv('XDG_SESSION_TYPE', 'wayland')
    caps = backend.capabilities()
    assert caps['host_permission_required'] and not caps['clipboard']


def test_frozen_wayland_does_not_advertise_unbundled_host_support(monkeypatch):
    monkeypatch.setenv('XDG_SESSION_TYPE', 'wayland')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    from elink.platforms.linux import backend
    assert backend.capabilities()['roles'] == ['controller']
    with pytest.raises(RuntimeError, match='X11'):
        backend.create_input(None, 'disabled', print)


def test_linux_update_never_selects_a_windows_executable(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    assets = []
    entry = dict(tag_name='v0.5.0', assets=assets)
    for name in ('Elink-0.5.0-windows-x64.zip', 'Elink-0.5.0-linux-x64.deb'):
        assets.append(dict(name=name, size=100, browser_download_url=updates.RELEASES_URL + '/download/v0.5.0/' + name))
    assert updates.select_release([entry], '0.4.37').name.endswith('.deb')
    with pytest.raises(RuntimeError, match='Linux'):
        updates.installation_dir(Path('.'))


def test_keyboard_preserves_arrows_keypad_and_extended_enter():
    assert keyboard.keysym_name(dict(vk=37, extended=True)) == 'Left'
    assert keyboard.keysym_name(dict(vk=100)) == 'KP_4'
    assert keyboard.keysym_name(dict(vk=13, extended=True)) == 'KP_Enter'
    assert keyboard.keysym_name(dict(vk=163)) == 'Control_R'
    assert keyboard.keysym_name(dict(vk=9)) == 'Tab'


def test_linux_startup_uses_current_user_and_frozen_launcher(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', '/opt/Elink Preview/elink')
    integration.set_startup(True)
    path = tmp_path / 'autostart/elink.desktop'
    assert 'Exec="/opt/Elink Preview/elink" "--startup"' in path.read_text()
    assert '-m' not in path.read_text()
    integration.set_startup(False)
    assert not path.exists()


def test_input_generation_releases_held_keys_and_rejects_old_packets():
    sink = RecordingInput()
    session = InputSession(sink)
    def send(**event):
        return session.receive(json.dumps(event))
    assert send(type='heartbeat', epoch=1, active=True)
    assert send(type='key', epoch=1, vk=65, scan=0, extended=False, down=True)
    assert send(type='heartbeat', epoch=2, active=True)
    assert sink.events[-1]['down'] is False
    assert not send(type='move', epoch=1, seq=1, absolute=True, x=0, y=0)
    assert send(type='move', epoch=2, seq=2, absolute=True, x=65535, y=65535)
    assert not send(type='move', epoch=2, seq=1, absolute=True, x=0, y=0)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file permissions')
def test_linux_identity_survives_restart_with_private_permissions(tmp_path):
    from elink.core.security import Identity
    first = Identity(tmp_path)
    assert Identity(tmp_path).fingerprint == first.fingerprint
    assert first.directory.stat().st_mode & 0o777 == 0o700
    assert (first.directory / 'host-key.pem').stat().st_mode & 0o777 == 0o600
    (first.directory / 'host.pem').unlink()
    with pytest.raises(ValueError, match='身份文件不完整'):
        Identity(tmp_path)
