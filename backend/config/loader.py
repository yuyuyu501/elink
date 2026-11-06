"""配置加载。

支持 JSON 配置文件；未提供的字段走 ``schema.py`` 中的默认值。
加载后统一走 :meth:`AppConfig.validate`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from backend.config.schema import AppConfig, ConfigError
from backend.logging.logger import get_logger

logger = get_logger(__name__)

DEFAULT_CONFIG_FILES = ("config.json", "elink.json")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON: {path}", detail=str(exc)) from exc


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    """加载配置；``path`` 为空时尝试默认文件名，都没有则返回默认配置。"""
    if path is not None:
        return AppConfig.from_dict(_read_json(Path(path)))

    for name in DEFAULT_CONFIG_FILES:
        candidate = Path(name)
        if candidate.is_file():
            logger.info("加载配置文件: %s", candidate.resolve())
            return AppConfig.from_dict(_read_json(candidate))

    logger.debug("未找到配置文件，使用默认配置")
    return AppConfig()


def merge_cli_overrides(config: AppConfig, overrides: dict[str, Any]) -> AppConfig:
    """把命令行覆盖项写回配置（点号路径，如 ``capture.fps``）。"""
    for key, value in overrides.items():
        if value is None:
            continue
        target: Any = config
        parts = key.split(".")
        for part in parts[:-1]:
            if not hasattr(target, part):
                raise ConfigError(f"未知配置路径: {key}")
            target = getattr(target, part)
        if not hasattr(target, parts[-1]):
            raise ConfigError(f"未知配置路径: {key}")
        setattr(target, parts[-1], value)

    if config.capture.backend == "auto":
        # 当前只接了 Windows 的 GDI 采集通道；接入 DXGI 桌面复制后在此切换
        config.capture.backend = "gdi"

    config.validate()
    return config
