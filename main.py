"""程序入口。

两套前端共用这一个入口：

    main.py                      命令行（采集/编码/解码的验证链路）
    main.py --ui                 桌面端图形界面（PySide6）
    main.py --self-test <json>   打包自检（界面 + 媒体链路）
    main.py --version            版本号

打包后的 ``Elink.exe`` 双击（无参数）默认打开图形界面；源码运行时无参数仍是
命令行链路。图形界面相关依赖（PySide6）只在 ``--ui`` / ``--self-test`` /
冻结包无参数时导入，所以没装 Qt 的机器依然可以用命令行跑通整条链路。
"""

from __future__ import annotations

import sys


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) == 2 and argv[0] == "--upscale-probe":
        import json
        from pathlib import Path
        from PySide6.QtGui import QGuiApplication
        from frontend.desktop.upscale import probe_report
        app = QGuiApplication([sys.argv[0]])
        Path(argv[1]).write_text(json.dumps(probe_report(), ensure_ascii=False), encoding="utf-8")
        return 0

    if argv and argv[0] in {"--version", "-V"}:
        from frontend.desktop import __version__

        print(__version__)
        return 0

    if argv and argv[0] in {"--ui", "--gui"}:
        from frontend.desktop.app import main as ui_main

        return ui_main([sys.argv[0], *argv[1:]])

    if argv and argv[0] == "--self-test":
        from frontend.desktop.app import main as ui_main

        return ui_main([sys.argv[0], *argv])

    if not argv and getattr(sys, "frozen", False):
        # 双击打包后的 Elink.exe：默认进图形界面，而不是静默跑命令行链路
        from frontend.desktop.app import main as ui_main

        return ui_main([sys.argv[0], "--ui"])

    from backend.app import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
