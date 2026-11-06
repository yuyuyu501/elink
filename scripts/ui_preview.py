"""离屏渲染桌面端页面截图（设计走查用）。

用法::

    python -m scripts.ui_preview --out out/ui-preview

默认使用 Qt 的 ``offscreen`` 平台插件，不需要显示器；如果本机 offscreen
字体库为空（文字变成方块），加 ``--platform windows`` 并让窗口显示在屏幕外。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

PAGES = ("overview", "host", "viewer", "devices", "logs")
SIZE = (1120, 740)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="elink 桌面端页面截图")
    parser.add_argument("--out", default="out/ui-preview", help="输出目录")
    parser.add_argument("--platform", default="offscreen", help="Qt 平台插件（offscreen/windows）")
    parser.add_argument("--width", type=int, default=SIZE[0])
    parser.add_argument("--height", type=int, default=SIZE[1])
    parser.add_argument("--pages", default=",".join(PAGES), help="逗号分隔的页面 key")
    parser.add_argument("--demo-peers", action="store_true", help="设备页填入示例同网设备")
    args = parser.parse_args(argv)

    os.environ["QT_QPA_PLATFORM"] = args.platform
    data_dir = Path(tempfile.mkdtemp(prefix="elink-ui-preview-"))
    os.environ["ELINK_DATA_DIR"] = str(data_dir)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from frontend.desktop.theme import apply_theme
    from frontend.desktop.ui.window import MainWindow

    app = QApplication.instance() or QApplication(["elink-ui-preview"])
    apply_theme(app)

    window = MainWindow(data_dir, auto_refresh=False)
    window.resize(args.width, args.height)
    window.service.refresh_devices()
    if args.demo_peers:
        window.discovery.stop()
        window.devices_page.set_discovery_state(True, "发现 2 台同网设备")
        window.devices_page.set_local_state(True, "srt://0.0.0.0:9000", 7000)
        window.devices_page.set_peers(
            [
                {
                    "id": "a" * 16,
                    "name": "STUDIO-PC",
                    "ip": "192.168.1.23",
                    "platform": "Windows 11",
                    "version": "0.6.0",
                    "hosting": True,
                    "srt_port": 9000,
                    "control_port": 7000,
                },
                {
                    "id": "b" * 16,
                    "name": "LIVINGROOM",
                    "ip": "192.168.1.41",
                    "platform": "Windows 10",
                    "version": "0.6.0",
                    "hosting": False,
                    "srt_port": 0,
                    "control_port": 7000,
                },
            ]
        )
    app.processEvents()
    # 让 Qt 完成布局与绘制，但不把窗口真的映射到桌面上（截图走查不打扰用户）
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for key in [item.strip() for item in args.pages.split(",") if item.strip()]:
        window._show_page(key)
        app.processEvents()
        pixmap = window.grab()
        target = out / f"{key}.png"
        if pixmap.save(str(target), "PNG"):
            saved.append(target)

    window.close()
    app.processEvents()
    for path in saved:
        print(path)
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
