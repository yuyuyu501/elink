"""Persist original failures before session cleanup, with bounded recent logs."""

from __future__ import annotations

import json
import logging
import threading
import traceback
import uuid
from collections import OrderedDict, deque
from datetime import datetime

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger

from . import __version__
from .paths import error_dir

_lock = threading.RLock()
_recent = deque(maxlen=100)
_reports = OrderedDict()
_writing = threading.local()
logger = get_logger(__name__)


class RecentLogs(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        remember(self.format(record))


def remember(message: str) -> None:
    with _lock:
        _recent.append(str(message)[-4000:])


def install_recent_logs() -> None:
    root = logging.getLogger("elink")
    if not any(isinstance(handler, RecentLogs) for handler in root.handlers):
        handler = RecentLogs()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)


def save_error(error: BaseException | str, *, fault_id: str = "", source: str = "local",
               context: dict | None = None) -> str:
    """Return a fault ID even if storage fails; never raise while reporting errors."""
    fault_id = fault_id or uuid.uuid4().hex
    if getattr(_writing, "active", False):
        return fault_id
    _writing.active = True
    try:
        now = datetime.now().astimezone()
        project_error = isinstance(error, ElinkError)
        entry = {
            "time": now.isoformat(timespec="seconds"),
            "version": __version__, "fault_id": fault_id, "source": source,
            "type": type(error).__name__ if isinstance(error, BaseException) else "SessionError",
            "code": error.code.value if project_error else ErrorCode.UNKNOWN.value,
            "message": error.message if project_error else str(error),
            "detail": error.detail if project_error else None,
            "traceback": (
                "".join(traceback.format_exception(type(error), error, error.__traceback__))
                if isinstance(error, BaseException) and error.__traceback__ is not None
                else "No local traceback available"
            ),
            "process": getattr(error, "process_diagnostics", None),
            "context": context or {},
        }
        with _lock:
            directory = error_dir()
            directory.mkdir(parents=True, exist_ok=True)
            key = (str(directory), fault_id)
            path = _reports.get(key)
            entry["recent_logs"] = list(_recent) if path is None else []
            text = json.dumps(entry, ensure_ascii=False, indent=2, default=str) + "\n"
            if path is None:
                stem = now.strftime("%Y%m%d_%H%M%S")
                index = 0
                while True:
                    suffix = f"_{index:02d}" if index else ""
                    path = directory / f"{stem}{suffix}.txt"
                    try:
                        with path.open("x", encoding="utf-8") as output:
                            output.write(text)
                        break
                    except FileExistsError:
                        index += 1
                _reports[key] = path
                if len(_reports) > 256:
                    _reports.popitem(last=False)
            else:
                with path.open("a", encoding="utf-8") as output:
                    output.write("\n" + text)
    except Exception:
        logger.warning("Unable to save error report", exc_info=True)
    finally:
        _writing.active = False
    return fault_id
