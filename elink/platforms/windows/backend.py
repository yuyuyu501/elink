"""Adapters retain the tested Windows native implementations during migration."""


def create_input(feedback, pad_backend, notify):
    from .input import WindowsInput
    return WindowsInput(feedback, pad_backend, notify)


def gamepads():
    from .input import XInput
    return XInput()


def capabilities():
    return dict(platform='windows', roles=['controller', 'host'],
                capture=['d3d11', 'dxcam'], input=['sendinput'],
                cursor_modes=['smart', 'remote', 'local'], clipboard=True,
                gamepad=['vigem', 'elinkpad'], gamepad_feedback=True)
