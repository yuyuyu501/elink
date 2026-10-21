import os
import sys


def is_wayland():
    return os.environ.get('XDG_SESSION_TYPE') == 'wayland' or bool(os.environ.get('WAYLAND_DISPLAY'))


def create_capture():
    if is_wayland():
        require_portal_runtime()
        from .portal import PortalCapture
        return PortalCapture()
    from .x11 import X11Capture
    return X11Capture()


def create_input(feedback, pad_backend, notify):
    if is_wayland():
        require_portal_runtime()
        from .portal import PortalInput
        return PortalInput(pad_backend, notify)
    from .x11 import X11Input
    return X11Input(pad_backend, notify)


def gamepads():
    from .gamepad import SDLGamepads
    return SDLGamepads()


def require_portal_runtime():
    if getattr(sys, 'frozen', False):
        raise RuntimeError('当前 Linux 安装包仅支持 X11 被控端；Wayland 被控端需要带 GI/GStreamer 的源码环境。')


def capabilities():
    wayland = is_wayland()
    host_available = not (wayland and getattr(sys, 'frozen', False))
    return dict(platform='linux', session='wayland' if wayland else 'x11',
                roles=['controller', 'host'] if host_available else ['controller'],
                capture=['portal-pipewire' if wayland else 'x11-mss'] if host_available else [],
                input=['portal' if wayland else 'xtest'] if host_available else [],
                cursor_modes=['remote'],
                clipboard=not wayland, gamepad=['uinput'], gamepad_feedback=False,
                host_permission_required=wayland)
