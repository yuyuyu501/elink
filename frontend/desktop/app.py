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
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from backend.logging.logger import get_logger, setup_logging

from . import __version__, icons
from .paths import APP_NAME, data_dir, ensure_dirs, log_file, resource_dir
from .theme import apply_theme
from .ui.window import MainWindow
from .error_report import install_recent_logs, save_error

logger = get_logger(__name__)


def _configure_logging(level: str = "INFO") -> None:
    setup_logging(level)
    path = log_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger("elink")
        if not any(isinstance(handler, logging.FileHandler)
                   and Path(handler.baseFilename) == path.resolve() for handler in root.handlers):
            setup_logging(level, log_file=str(path))
    except OSError:
        logger.warning("无法写入日志文件: %s", path, exc_info=True)
    install_recent_logs()

    def thread_exception(args):
        save_error(args.exc_value, context={"thread": args.thread.name if args.thread else ""})
        logger.error("未捕获的后台线程异常",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    threading.excepthook = thread_exception

    def exception_hook(kind, value, tb):
        save_error(value.with_traceback(tb), context={"stage": "desktop"})
        logger.error("未捕获的界面异常", exc_info=(kind, value, tb))

    sys.excepthook = exception_hook


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

    if "--elevated-restart" in argv:
        from .privileges import wait_for_previous
        index = argv.index("--elevated-restart")
        os.environ["ELINK_DATA_DIR"] = str(Path(argv[index + 2]).resolve())
        wait_for_previous(int(argv[index + 1]))
    root = data_dir()
    ensure_dirs(root)
    _configure_logging(os.environ.get("ELINK_LOG_LEVEL", "INFO"))

    from PySide6.QtGui import QSurfaceFormat
    from .ui.video_surface import video_surface_format

    QSurfaceFormat.setDefaultFormat(video_surface_format())
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
    from . import settings
    from .privileges import is_elevated, restart_elevated
    if sys.platform == "win32" and settings.load_ui().admin_startup and not is_elevated():
        try:
            restart_elevated(root)
        except OSError:
            pass
        else:
            lock.unlock()
            return 0

    try:
        window = MainWindow(root)
    except Exception as exc:  # 启动期异常必须让用户看到，而不是静默退出
        save_error(exc, context={"stage": "startup"})
        logger.exception("界面启动失败")
        QMessageBox.critical(None, APP_NAME, f"启动失败：{exc}")
        return 1

    window.shutdown_complete.connect(app.quit)

    def exception_hook(kind, value, tb):
        logger.error("未捕获的界面异常", exc_info=(kind, value, tb))
        fault_id = save_error(value.with_traceback(tb), context={"stage": "ui"})
        window.log("".join(traceback.format_exception(kind, value, tb)), "error", fault_id=fault_id)

    sys.excepthook = exception_hook

    window.show()
    ready = os.environ.pop("ELINK_UPDATE_READY", None)
    if ready:
        target = Path(ready)
        if target.name == "app-ready" and target.parent.name.startswith(".Elink-update-"):
            QTimer.singleShot(0, lambda: target.write_text(__version__, encoding="ascii"))
    icons.clear_cache()  # 让首次渲染后缓存失效，避免主题切换残留旧色
    logger.info("界面已启动，版本 %s，数据目录 %s", __version__, root)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
