from __future__ import annotations

import os
import sys
import traceback

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .paths import data_dir, ensure_dirs
from . import __version__
from .ui.theme import apply_theme
from .ui.window import MainWindow


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--peer-test":
        from .peer_diagnostics import run
        return run(sys.argv[2])
    if len(sys.argv) >= 3 and sys.argv[1] in ("--self-test", "--self-test-desktop"):
        from pathlib import Path
        from .diagnostics import run
        return run(Path(sys.argv[2]).resolve(), desktop=sys.argv[1] == "--self-test-desktop")
    app = QApplication(sys.argv)
    app.setApplicationName("Elink")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Elink")
    apply_theme(app)
    if os.name != "nt":
        QMessageBox.critical(None, "Elink", "当前版本仅支持 Windows，Linux 和 Android 尚未实现。")
        return 1
    root = data_dir()
    try:
        ensure_dirs(root)
        lock = QLockFile(str(root / "elink.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            QMessageBox.information(None, "Elink", "此数据目录已有 Elink 实例正在运行。")
            return 1
        window = MainWindow(root)
    except Exception as exc:
        QMessageBox.critical(None, "Elink", f"启动失败：{exc}")
        return 1

    def exception_hook(kind, value, tb):
        window.append_log("".join(traceback.format_exception(kind, value, tb)))
        window.report_error(f"操作失败：{value}")

    sys.excepthook = exception_hook
    window.show()
    ready = os.environ.pop("ELINK_UPDATE_READY", "")
    failure = os.environ.pop("ELINK_UPDATE_FAILURE", "")
    if ready:
        def confirm_startup():
            from pathlib import Path
            Path(ready).write_text(__version__, encoding="utf-8")
        QTimer.singleShot(0, confirm_startup)
    if failure:
        QTimer.singleShot(0, lambda: QMessageBox.warning(
            window, "更新未完成", f"新版未能正常启动，已恢复旧版。\n更新日志：{failure}"))
    result = app.exec()
    lock.unlock()
    return result
