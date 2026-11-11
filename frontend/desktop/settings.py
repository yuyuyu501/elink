"""界面设置持久化（JSON）。

用 JSON 而不是 QSettings 的原因：设置项要和后端 ``AppConfig`` 对齐、还要能被
命令行/脚本读取，INI/注册表都不方便；文件放在用户数据目录（见 ``paths``），
打包后依然可写。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from backend.logging.logger import get_logger

from .paths import data_dir, settings_file
from .service import HostSettings, ViewerSettings

logger = get_logger(__name__)

DEFAULT_HOST = HostSettings()
DEFAULT_VIEWER = ViewerSettings()


@dataclass
class UiSettings:
    """窗口行为（与后端参数分开保存）。"""

    start_on_login: bool = False
    close_to_tray: bool = False


def _read() -> dict:
    path = settings_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("设置文件损坏，已忽略: %s", path)
        return {}
    return data if isinstance(data, dict) else {}


def load() -> tuple[HostSettings, ViewerSettings]:
    data = _read()
    host = _merge(HostSettings, data.get("host"))
    viewer = _merge(ViewerSettings, data.get("viewer"))
    return host, viewer


def load_ui() -> UiSettings:
    data = _read()
    return _merge(UiSettings, data.get("ui"))


def save(host: HostSettings, viewer: ViewerSettings, ui: UiSettings | None = None) -> None:
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "host": asdict(host),
        "viewer": asdict(viewer),
        "ui": asdict(ui or UiSettings()),
    }
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logger.warning("设置保存失败: %s", path, exc_info=True)


def _merge(cls, values) -> object:
    """只接受已知字段，避免旧版本残留字段导致构造失败。"""
    if not isinstance(values, dict):
        return cls()
    allowed = {field for field in cls.__dataclass_fields__}
    return cls(**{key: value for key, value in values.items() if key in allowed})


def captures_dir():
    path = data_dir() / "captures"
    path.mkdir(parents=True, exist_ok=True)
    return path
