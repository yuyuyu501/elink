"""Windows 打包：构建 → 产物自检 → 便携 ZIP →（可选）Inno Setup 安装包。

自检用**打包后的 exe**跑 ``--self-test``：只有产物自己能证明 Qt 插件、
图标资源、ffmpeg 通路都在位。自检不通过就直接失败，绝不产出「看起来能装、
装完打不开」的安装包。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "Elink"

ISCC_CANDIDATES = [
    Path(os.environ["ELINK_ISCC"]) if os.environ.get("ELINK_ISCC") else None,
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
]
ISCC_CANDIDATES = [path for path in ISCC_CANDIDATES if path is not None]


def run_self_test(executable: Path, timeout: float = 180.0) -> dict:
    report_path = executable.parent / "self-test.json"
    if report_path.exists():
        report_path.unlink()

    print(f"自检: {executable}")
    try:
        process = subprocess.run(
            [str(executable), "--self-test", str(report_path)],
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"产物自检超时（{timeout}s）") from exc

    if not report_path.is_file():
        raise RuntimeError(
            "产物没有生成自检报告\n"
            f"stdout={process.stdout[-2000:]}\nstderr={process.stderr[-2000:]}"
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not report.get("ok"):
        raise RuntimeError(f"产物自检未通过: {report.get('error', report)}")
    media = report.get("media", {})
    if media.get("decoded_frames", 0) <= 0:
        raise RuntimeError("产物媒体链路自检失败：没有解出任何帧")
    print(
        "自检通过: ui=%d 页 媒体帧=%d 字体=%d"
        % (
            report.get("ui", {}).get("pages", 0),
            media.get("decoded_frames", 0),
            report.get("ui", {}).get("fonts", 0),
        )
    )
    return report


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_zip(folder: Path, version: str, out_dir: Path) -> Path:
    stamp = "windows-x64"
    target = out_dir / f"Elink-{version}-{stamp}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, Path(folder.name) / path.relative_to(folder))
    print(f"便携包: {target} ({target.stat().st_size / 1024 / 1024:.1f} MB)")
    return target


def make_installer(folder: Path, version: str, out_dir: Path) -> Path | None:
    iscc = next((path for path in ISCC_CANDIDATES if path.is_file()), None)
    if iscc is None:
        found = shutil.which("iscc") or shutil.which("ISCC")
        iscc = Path(found) if found else None
    if iscc is None:
        print("未找到 Inno Setup（ISCC.exe），跳过安装包。安装后重跑即可生成。")
        return None

    script = ROOT / "packaging" / "windows" / "elink.iss"
    args = [
        str(iscc),
        f"/DMyAppVersion={version}",
        f"/DSourceDir={folder}",
        f"/DOutputDir={out_dir}",
        str(script),
    ]
    print("ISCC:", " ".join(args))
    subprocess.run(args, cwd=ROOT, check=True)
    candidates = sorted(out_dir.glob("Elink-*-setup.exe"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        print("ISCC 执行成功但没有找到 setup 产物，请检查 .iss 的 OutputBaseFilename。")
        return None
    print(f"安装包: {candidates[-1]}")
    return candidates[-1]


def package(dist_dir: Path | None = None, *, installer: bool = True, run_tests: bool = True) -> dict:
    if os.name != "nt":
        raise RuntimeError("该脚本只在 Windows 上使用；Linux 见 scripts/package_linux.py")

    from frontend.desktop import __version__

    dist = (ROOT / (dist_dir or "dist")).resolve()

    if run_tests:
        print("运行回归测试…")
        subprocess.run([sys.executable, "-m", "scripts.check"], cwd=ROOT, check=True)

    executable = __import__("scripts.build", fromlist=["build"]).build(dist)
    report = run_self_test(executable)

    out_dir = dist / __version__
    out_dir.mkdir(parents=True, exist_ok=True)
    built_at = datetime.now(timezone.utc)
    archive = make_zip(executable.parent, __version__, out_dir)
    setup = make_installer(executable.parent, __version__, out_dir) if installer else None

    assets = [archive]
    if setup is not None:
        assets.append(setup)
    checksums = out_dir / "SHA256SUMS.txt"
    checksums.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in assets), encoding="ascii"
    )
    verification = {
        "app": APP_NAME,
        "version": __version__,
        "built_at": built_at.isoformat(),
        "format": "portable-folder" if setup is None else "portable-folder+inno-setup",
        "channel": "preview",
        "signed": False,
        "zip": {"name": archive.name, "sha256": sha256(archive), "size": archive.stat().st_size},
        "installer": (
            {"name": setup.name, "sha256": sha256(setup), "size": setup.stat().st_size}
            if setup is not None
            else None
        ),
        "self_test": {
            "ok": report.get("ok"),
            "ui_pages": report.get("ui", {}).get("pages"),
            "decoded_frames": report.get("media", {}).get("decoded_frames"),
            "ffmpeg": report.get("ffmpeg", {}).get("version"),
            "frozen": report.get("frozen"),
            "backend_import": report.get("registry", {}).get("module_import"),
        },
        "limitations": [
            "installer is not code-signed; SmartScreen may warn",
            "ffmpeg must be available on PATH at runtime",
        ],
    }
    verification_path = out_dir / "verification.json"
    verification_path.write_text(
        json.dumps(verification, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    summary = {
        "version": __version__,
        "built_at": built_at.isoformat(),
        "exe": str(executable),
        "zip": str(archive),
        "installer": str(setup) if setup else "",
        "package_dir": str(out_dir),
        "hashes": {
            archive.name: sha256(archive),
            **({setup.name: sha256(setup)} if setup is not None else {}),
        },
        "self_test": verification["self_test"],
    }
    (out_dir / "package-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dist / "package-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist-dir", type=Path, default=None)
    parser.add_argument("--no-installer", action="store_true")
    parser.add_argument("--skip-tests", action="store_true")
    options = parser.parse_args()
    package(options.dist_dir, installer=not options.no_installer, run_tests=not options.skip_tests)
