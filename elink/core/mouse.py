"""Cursor presentation is separate from absolute/relative input coordinates."""

MOUSE_MODES = (
    ('智能鼠标', 'smart', '根据被控端系统光标状态切换：桌面使用绝对定位，隐藏光标时使用游戏相对移动。'),
    ('使用被控端鼠标', 'remote', '显示被控端的系统光标，使用相对移动；光标位置随视频传回，会受网络延迟影响。'),
    ('使用主控端鼠标', 'local', '显示本机箭头，使用绝对定位，移动更即时；部分游戏可能不兼容。'),
)


def mouse_mode(value):
    return value if value in ('smart', 'remote', 'local') else 'smart'


def relative_mouse(mode, visible):
    return mode == 'remote' or mode == 'smart' and visible is False
