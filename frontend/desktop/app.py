"""桌面端入口。

命令行参数：

    elink-ui                 正常启动
    elink-ui --self-test X   跑自检并把 JSON 写到 X（打包脚本用它验证产物）
    elink-ui --version       打印版本

自检放在这里而不是单独脚本，是因为「能不能跑」必须由**打包后的可执行文件**
自己回答，包括 Qt 插件、图标资源、ffmpeg 通路。
"""

from __future__ import annotations

import logging
import os
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from backend.logging.logger import get_logger, setup_logging

from . import __version__, icons
from .paths import APP_NAME, data_dir, ensure_dirs, log_file, resource_dir
from .theme import apply_theme
from .ui.window import MainWindow

logger = get_logger(__name__)


def _configure_logging(level: str = "INFO") -> None:
    setup_logging(level)
    path = log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-5s [%(name)s] %(message)s")
        )
        logging.getLogger().addHandler(handler)
    except OSError:
        logger.warning("无法写入日志文件: %s", path, exc_info=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)

    if "--version" in argv:
        print(__version__)
        return 0

    if "--self-test" in argv:
        index = argv.index("--self-test")
        if index + 1 >= len(argv):
            print("用法: --self-test <输出 json 路径>", file=sys.stderr)
            return 2
        from .diagnostics import run_self_test

        return run_self_test(Path(argv[index + 1]).resolve())

    root = data_dir()
    ensure_dirs(root)
    _configure_logging(os.environ.get("ELINK_LOG_LEVEL", "INFO"))

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(APP_NAME)
    icon_path = resource_dir() / "elink.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    apply_theme(app)

    lock = QLockFile(str(root / "elink-desktop.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, "此数据目录已有 elink 桌面端在运行。")
        return 1

    try:
        window = MainWindow(root)
    except Exception as exc:  # 启动期异常必须让用户看到，而不是静默退出
        logger.exception("界面启动失败")
        QMessageBox.critical(None, APP_NAME, f"启动失败：{exc}")
        return 1

    window.shutdown_complete.connect(app.quit)

    def exception_hook(kind, value, tb):
        logger.error("未捕获的界面异常", exc_info=(kind, value, tb))
        window.log("".join(traceback.format_exception(kind, value, tb)), "error")

    sys.excepthook = exception_hook

    window.show()
    icons.clear_cache()  # 让首次渲染后缓存失效，避免主题切换残留旧色
    logger.info("界面已启动，版本 %s，数据目录 %s", __version__, root)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
