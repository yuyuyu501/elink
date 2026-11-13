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
    ffmpeg_dir = _ffmpeg_source() if os.name == "nt" else None
    video_library = None
    if os.name == "nt":
        from scripts.build_video import build as build_video
        video_library = build_video()

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
    if video_library is not None:
        shutil.copy2(video_library, output / "_internal/frontend/desktop/resources/elink_video.dll")
    if ffmpeg_dir is not None:
        _copy_ffmpeg(ffmpeg_dir, output)
    _write_build_info(output)
    executable = output / (f"{APP_NAME}.exe" if os.name == "nt" else APP_NAME)
    print(executable)
    return executable


def _ffmpeg_source() -> Path:
    configured = os.environ.get("ELINK_FFMPEG_DIR")
    found = shutil.which("ffmpeg")
    source = Path(configured).resolve() if configured else None
    if source is None and found:
        source = Path(found).resolve().parent
    if source is None or not (source / "ffmpeg.exe").is_file():
        raise RuntimeError("Set ELINK_FFMPEG_DIR to a real ffmpeg distribution bin directory")
    # Chocolatey shims are launchers and must never become the bundled dependency.
    if (source / "ffmpeg.exe").stat().st_size < 1024 * 1024:
        raise RuntimeError("ffmpeg.exe is a launcher; set ELINK_FFMPEG_DIR to the actual distribution")
    return source


def _copy_ffmpeg(source: Path, output: Path) -> None:
    target = output / "ffmpeg"
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / "ffmpeg.exe", target / "ffmpeg.exe")
    for dll in source.glob("*.dll"):
        shutil.copy2(dll, target / dll.name)
    notices = [
        path for folder in (source, source.parent) for path in folder.iterdir()
        if path.is_file() and path.name.lower().startswith(("license", "copying", "readme"))
    ]
    if not any(path.name.lower().startswith(("license", "copying")) for path in notices):
        raise RuntimeError("ffmpeg distribution is missing its license")
    for path in notices:
        shutil.copy2(path, target / path.name)


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
        "ffmpeg_bundled": (output / "ffmpeg" / "ffmpeg.exe").is_file(),
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
