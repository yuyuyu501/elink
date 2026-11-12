"""elink 桌面端（PySide6）。

与 ``frontend/mobile``（PWA）共用后端：界面只负责「选参数、看画面、按按钮」，
一切采集/编码/传输/注入都在 ``backend`` 里。桥接层见 :mod:`frontend.desktop.service`。
"""

__all__ = ["__version__"]

__version__ = "0.6.5"
