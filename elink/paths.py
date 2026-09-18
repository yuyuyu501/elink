from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("ELINK_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share")) / "Elink"


def resource_dir() -> Path:
    return Path(__file__).resolve().parent / "resources"


def bundle_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def ensure_dirs(root: Path) -> None:
    for name in ("logs", "identity"):
        (root / name).mkdir(parents=True, exist_ok=True)
