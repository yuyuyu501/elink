"""Linux 打包：PyInstaller 构建 → ``.tar.gz`` + 桌面项。

Linux 上不做「安装器」，而是给一个可解压即用的目录 + ``.desktop`` 文件，
用户自己放到 ``~/.local/opt`` 或 ``/opt``；这比维护 deb/rpm/AppImage 三套
打包脚本便宜得多，也不会因为发行版差异而碎。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "Elink"


def package(dist_dir: Path | None = None, *, run_tests: bool = True) -> Path:
    if not sys.platform.startswith("linux"):
        print("提示：当前不在 Linux 上，产出的包只在本机可用作验证。")

    from frontend.desktop import __version__

    dist = (ROOT / (dist_dir or "dist")).resolve()

    if run_tests:
        subprocess.run([sys.executable, "-m", "scripts.check"], cwd=ROOT, check=True)

    from scripts.build import build

    executable = build(dist)
    output = executable.parent

    desktop = output / "elink.desktop"
    template = (ROOT / "packaging" / "linux" / "elink.desktop").read_text(encoding="utf-8")
    desktop.write_text(
        template.replace("@EXEC@", str(output / APP_NAME)).replace("@VERSION@", __version__),
        encoding="utf-8",
    )
    desktop.chmod(0o755)

    icon = ROOT / "packaging" / "linux" / "elink.svg"
    if icon.is_file():
        shutil.copy2(icon, output / "elink.svg")

    archive = dist / f"elink-{__version__}-linux-{os.uname().machine}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(output, arcname=APP_NAME)
    print(f"发布包: {archive}")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=None)
    parser.add_argument("--skip-tests", action="store_true")
    options = parser.parse_args()
    package(options.dist_dir, run_tests=not options.skip_tests)
