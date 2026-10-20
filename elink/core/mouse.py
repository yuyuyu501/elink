"""Cursor presentation is separate from absolute/relative input coordinates."""

import math

MOUSE_MODES = (
    ('智能鼠标', 'smart', '根据被控端系统光标状态切换：桌面使用绝对定位，隐藏光标时使用游戏相对移动。'),
    ('使用被控端鼠标', 'remote', '显示被控端的系统光标；桌面按主控端位置定位，隐藏光标时使用游戏相对移动。光标随视频返回，有网络延迟。'),
    ('使用主控端鼠标', 'local', '桌面显示主控端软件光标；被控端隐藏光标时自动切换游戏相对移动并隐藏箭头。'),
)


def mouse_mode(value):
    return value if value in ('smart', 'remote', 'local') else 'smart'


def relative_mouse(mode, visible):
    # Cursor presentation must not change desktop movement. Raw counts would
    # apply host pointer acceleration and ignore the displayed video scale.
    # Keep the old remote-mode fallback only when visibility is unavailable.
    return visible is False or mode == 'remote' and visible is None


def sensitivity_value(value):
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
        # Preserve sub-count motion at low sensitivity instead of losing slow
        # movements when every individual hardware event rounds down to zero.
        scaled = [delta * self.factor + rest for delta, rest in zip((x, y), self.remainder)]
        counts = [max(-32767, min(32767, math.trunc(delta))) for delta in scaled]
        self.remainder = [delta - math.trunc(delta) for delta in scaled]
        return tuple(counts)
