from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .paths import data_dir, ensure_dirs
from .logging_setup import configure_logging
from . import __version__
from .ui.theme import apply_theme
from .ui.window import MainWindow


def main() -> int:
    if os.name == 'nt' and len(sys.argv) == 4 and sys.argv[1] == '--input-broker':
        from .platforms.windows.input_broker import run_input_broker
        return run_input_broker(int(sys.argv[2]), sys.argv[3])
    if os.name == 'nt' and len(sys.argv) == 4 and sys.argv[1] == '--elevated-restart':
        from .platforms.windows.privileges import is_elevated, wait_for_previous
        if not is_elevated():
            return 1
        wait_for_previous(int(sys.argv[2]))
        os.environ['ELINK_DATA_DIR'] = sys.argv[3]
    if len(sys.argv) == 3 and sys.argv[1] == '--self-test-gpu':
        from pathlib import Path
        output = Path(sys.argv[2])
        try:
            from scripts.check_gpu_video import main as check_gpu
            check_gpu(['--output', str(output), '--frames', '30', '--stream-check'])
        except Exception:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({'ok': False, 'error': traceback.format_exc()}, indent=2), encoding='utf-8')
            return 1
        return 0
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
    if os.name != 'nt' and not sys.platform.startswith('linux'):
        QMessageBox.critical(None, "Elink", "桌面端支持 Windows 和 Linux。")
        return 1
    root = data_dir()
    try:
        ensure_dirs(root)
        lock = QLockFile(str(root / "elink.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            QMessageBox.information(None, "Elink", "此数据目录已有 Elink 实例正在运行。")
            return 1
        configure_logging(root)
        window = MainWindow(root)
        window.shutdown_complete.connect(app.quit)
    except Exception as exc:
        logging.getLogger(__name__).exception('Application startup failed')
        QMessageBox.critical(None, "Elink", f"启动失败：{exc}")
        return 1

    def exception_hook(kind, value, tb):
        logging.getLogger(__name__).error('Unhandled GUI exception', exc_info=(kind, value, tb))
        window.append_log("".join(traceback.format_exception(kind, value, tb)))
        window.report_error(f"操作失败：{value}")

    sys.excepthook = exception_hook
    def thread_exception_hook(args):
        logging.getLogger(__name__).error('Unhandled worker exception: %s', getattr(args.thread, 'name', 'unknown'),
                                         exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_exception_hook
    if '--startup' in sys.argv:
        window.show_startup()
    else:
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


if __name__ == '__main__':
    raise SystemExit(main())
