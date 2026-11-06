"""PyInstaller 构建：产出 ``dist/Elink/Elink.exe``（onedir）。

为什么用 onedir 而不是 onefile：

- 启动快（onefile 每次都要解压到临时目录，Qt 应用第一次启动要好几秒）；
- 升级只替换变化的文件；
- 出错时磁盘上的文件可以直接看，便于排障。

``--collect-submodules backend`` 是必须的：注册表按「模块路径字符串」动态
导入各平台后端，PyInstaller 的静态分析看不到这些引用。
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "Elink"


def _venv_python() -> str:
    return sys.executable


def build(dist_dir: Path | None = None, *, clean: bool = True, console: bool = False) -> Path:
    dist = (ROOT / (dist_dir or "dist")).resolve()
    resources = ROOT / "frontend" / "desktop" / "resources"

    args = [
        _venv_python(),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        APP_NAME,
        "--onedir",
        "--distpath",
        str(dist),
        "--workpath",
        str(dist / "build"),
        "--specpath",
        str(dist),
        "--add-data",
        f"{resources}{os.pathsep}frontend/desktop/resources",
        "--collect-submodules",
        "backend",
        "--collect-submodules",
        "frontend",
        "--hidden-import",
        "PySide6.QtSvg",
        "--hidden-import",
        "frontend.desktop.upscale",
        "--icon",
        str(ROOT / "frontend" / "desktop" / "resources" / "elink.ico"),
        "--exclude-module",
        "pytest",
        "--exclude-module",
        "tests",
        "--exclude-module",
        "tkinter",
        str(ROOT / "main.py"),
    ]
    if clean:
        args.insert(3, "--clean")
    args.insert(3, "--console" if console else "--windowed")

    print("PyInstaller:", " ".join(args))
    subprocess.run(args, cwd=ROOT, check=True)

    output = dist / APP_NAME
    _copy_extras(output)
    _write_build_info(output)
    executable = output / (f"{APP_NAME}.exe" if os.name == "nt" else APP_NAME)
    print(executable)
    return executable


def _copy_extras(output: Path) -> None:
    """把用户可见的文档、移动端页面一并放进产物。"""
    for name in ("README.md", "THIRD_PARTY_NOTICES.md"):
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, output / name)
    docs = ROOT / "docs"
    if docs.is_dir():
        shutil.copytree(docs, output / "docs", dirs_exist_ok=True)
    licenses = ROOT / "licenses"
    if licenses.is_dir():
        shutil.copytree(licenses, output / "licenses", dirs_exist_ok=True)
    assets = ROOT / "assets"
    if assets.is_dir():
        shutil.copytree(assets, output / "assets", dirs_exist_ok=True)
    mobile = ROOT / "frontend" / "mobile"
    if mobile.is_dir():
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "__init__.py")
        shutil.copytree(mobile, output / "web" / "mobile", dirs_exist_ok=True, ignore=ignore)
        shutil.copytree(
            ROOT / "frontend" / "shared", output / "web" / "shared", dirs_exist_ok=True, ignore=ignore
        )


def _write_build_info(output: Path) -> None:
    from frontend.desktop import __version__

    versions: dict[str, str] = {}
    for name in ("PySide6", "pyinstaller"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue

    info = {
        "elink": __version__,
        "python": sys.version,
        "platform": sys.platform,
        "packages": versions,
        "ffmpeg_required": True,
    }
    (output / "build-info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=None)
    parser.add_argument("--console", action="store_true", help="保留控制台窗口（调试用）")
    build(parser.parse_args().dist_dir, console=parser.parse_args().console)
