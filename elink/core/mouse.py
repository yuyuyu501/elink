"""Cursor presentation is separate from absolute/relative input coordinates."""

MOUSE_MODES = (
    ('智能鼠标', 'smart', '根据被控端系统光标状态切换：桌面使用绝对定位，隐藏光标时使用游戏相对移动。'),
    ('使用被控端鼠标', 'remote', '显示被控端的系统光标，使用相对移动；光标位置随视频传回，会受网络延迟影响。'),
    ('使用主控端鼠标', 'local', '桌面显示主控端软件光标；被控端隐藏光标时自动切换游戏相对移动并隐藏箭头。'),
)


def mouse_mode(value):
    return value if value in ('smart', 'remote', 'local') else 'smart'


def relative_mouse(mode, visible):
    # A local software cursor is useful on the desktop, but games commonly
    # hide/confine the host cursor and expect relative deltas. Follow that
    # state for local mode too so the software arrow disappears in-game.
    return mode == 'remote' or mode in ('smart', 'local') and visible is False
