"""Mouse presentation and sensitivity shared by settings and the viewer."""

import math

MOUSE_MODES = (
    ("智能鼠标", "smart"),
    ("使用被控端鼠标", "remote"),
    ("使用主控端鼠标", "local"),
)


def mouse_mode(value):
    if not isinstance(value, str):
        return "smart"
    value = {"absolute": "smart", "relative": "remote"}.get(value, value)
    return value if value in ("smart", "remote", "local") else "smart"


def relative_mouse(mode, visible):
    return visible is False or mode == "remote" and visible is None


def sensitivity_value(value):
    if isinstance(value, bool):
        return 0.0
    try:
        value = float(value)
        return round(max(-5.0, min(5.0, value)), 1) if math.isfinite(value) else 0.0
    except (TypeError, ValueError):
        return 0.0


class MouseSensitivity:
    def __init__(self, value=0):
        self.value = sensitivity_value(value)
        self.factor = 2 ** (self.value / 5)
        self.remainder = [0.0, 0.0]

    def relative(self, x, y):
        scaled = [delta * self.factor + rest for delta, rest in zip((x, y), self.remainder)]
        self.remainder = [delta - math.trunc(delta) for delta in scaled]
        return tuple(max(-32767, min(32767, math.trunc(delta))) for delta in scaled)
