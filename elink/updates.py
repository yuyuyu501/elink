"""GitHub portable updates: inspect, download, verify, stage, then hand off to Windows."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
import zipfile

import aiohttp

from . import __version__
from .paths import resource_dir
from .processes import native_environment, native_launch

REPOSITORY = "yuyuyu501/elink"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases?per_page=100"
MAX_DOWNLOAD = 1024 * 1024 * 1024
MAX_EXPANDED = 3 * MAX_DOWNLOAD
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")


def version_key(value):
    match = VERSION.fullmatch(value)
    if not match:
        raise ValueError("无效的版本号")
    major, minor, patch, preview = match.groups()
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in (preview or "").split("."))
    return int(major), int(minor), int(patch), preview is None, parts


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    name: str
    url: str
    size: int
    digest: str
    preview: bool


def select_release(entries, current=__version__, include_preview=True):
    if not isinstance(entries, list):
        raise ValueError("GitHub 返回了无效的版本列表")
    candidates = []
    for entry in entries:
        try:
            tag = entry["tag_name"]
            version = tag.removeprefix("v")
            key = version_key(version)
            preview = bool(entry.get("prerelease")) or "-" in version
            if entry.get("draft") or (preview and not include_preview) or key <= version_key(current):
                continue
            name = f"Elink-{version}-windows-x64.zip"
            assets = [a for a in entry["assets"] if a["name"] == name]
            if len(assets) != 1:
                continue
            asset = assets[0]
            url = f"{RELEASES_URL}/download/{tag}/{name}"
            if (asset["browser_download_url"] != url or type(asset["size"]) is not int
                    or not 0 < asset["size"] <= MAX_DOWNLOAD):
                continue
            digest = asset.get("digest") or ""
            if digest and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
                continue
            candidates.append((key, Release(version, tag, name, url, asset["size"],
                                            digest.removeprefix("sha256:").lower(), preview)))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def allowed_url(url):
    parts = urlsplit(url)
    return (parts.scheme == "https" and not parts.username and not parts.password
            and parts.port in (None, 443) and parts.hostname in {
                "api.github.com", "github.com", "release-assets.githubusercontent.com",
                "objects.githubusercontent.com"})


async def response_for(session, url):
    for _ in range(6):
        if not allowed_url(url):
            raise ValueError("更新下载被重定向到非 GitHub 地址")
        response = await session.get(url, allow_redirects=False)
        if response.status in (301, 302, 303, 307, 308):
            url = response.headers.get("Location", "")
            response.release()
            continue
        if response.status != 200:
            status = response.status
            response.release()
            raise RuntimeError(f"GitHub 请求失败（HTTP {status}），请稍后重试或手动下载")
        return response
    raise RuntimeError("更新下载重定向次数过多")


def session():
    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=1800, connect=20, sock_read=30),
        headers={"User-Agent": f"Elink/{__version__}", "Accept-Encoding": "identity"},
        auto_decompress=False)


async def read_small(client, url, limit):
    response = await response_for(client, url)
    try:
        result = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            result.extend(chunk)
            if len(result) > limit:
                raise ValueError("更新元数据超过大小限制")
        return bytes(result)
    finally:
        response.release()


async def check_release(include_preview=True):
    try:
        async with session() as client:
            async with asyncio.timeout(45):
                entries = json.loads(await read_small(client, API_URL, 4 * 1024 * 1024))
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise RuntimeError("无法连接 GitHub（网络错误或超时），请重试或手动下载。") from exc
    return select_release(entries, include_preview=include_preview)


def installation_dir(data_root):
    if os.name != "nt" or not getattr(sys, "frozen", False):
        raise RuntimeError("源码运行模式只能检测更新；自动替换需使用 Windows x64 便携版。")
    target = Path(sys.executable).absolute().parent
    if target.is_symlink() or target.resolve() != target or not (target / "_internal").is_dir():
        raise RuntimeError("当前目录不是受支持的便携版安装目录，请手动更新。")
    info = json.loads((target / "build-info.json").read_text(encoding="utf-8"))
    if info.get("elink") != __version__ or Path(sys.executable).name.lower() != "elink.exe":
        raise RuntimeError("无法确认当前便携包版本，请手动更新。")
    if Path(data_root).resolve().is_relative_to(target):
        raise RuntimeError("用户数据位于应用目录内，请先将 ELINK_DATA_DIR 移到应用目录之外。")
    return target


def extract_verified(archive, destination, version, cancelled):
    """Validate all members before extraction; never write to the active app directory."""
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > 30000 or sum(i.file_size for i in entries) > MAX_EXPANDED:
            raise ValueError("更新包解压大小或文件数超出限制")
        seen = set()
        for item in entries:
            name = item.orig_filename
            path = PurePosixPath(name)
            parts = name.rstrip("/").split("/")
            mode = item.external_attr >> 16
            if (name != item.filename or not parts or parts[0] != "Elink" or path.is_absolute() or "\\" in name
                    or any(p in ("", ".", "..") or p.endswith((".", " "))
                           or re.search(r'[<>:"|?*\x00-\x1f]', p)
                           or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?", p)
                           for p in parts)
                    or stat.S_ISLNK(mode) or item.flag_bits & 1
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                raise ValueError("更新包包含不安全的文件路径或类型")
            folded = str(path).casefold()
            if folded in seen:
                raise ValueError("更新包包含重复文件")
            seen.add(folded)
        for item in entries:
            if cancelled.is_set():
                raise RuntimeError("已取消更新")
            output = destination.joinpath(*PurePosixPath(item.filename).parts)
            if item.is_dir():
                output.mkdir(parents=True, exist_ok=True)
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(item) as source, output.open("xb") as sink:
                while chunk := source.read(1024 * 1024):
                    if cancelled.is_set():
                        raise RuntimeError("已取消更新")
                    sink.write(chunk)
    payload = destination / "Elink"
    if not (payload / "Elink.exe").is_file() or not (payload / "_internal").is_dir():
        raise ValueError("更新包缺少 Elink.exe 或运行库")
    metadata = payload / "build-info.json"
    if metadata.stat().st_size > 1024 * 1024:
        raise ValueError("构建信息过大")
    if json.loads(metadata.read_text(encoding="utf-8")).get("elink") != version:
        raise ValueError("更新包版本与 Release 不一致")
    return payload


async def prepare_update(release, target, cancelled, progress):
    # A sibling workspace guarantees same-volume directory renames at install time.
    workspace = Path(tempfile.mkdtemp(prefix=".Elink-update-", dir=target.parent))
    extraction = None
    try:
        if shutil.disk_usage(workspace).free < release.size + MAX_EXPANDED:
            raise RuntimeError("更新需要至少 3 GiB 加下载包大小的可用磁盘空间")
        archive = workspace / "update.zip"
        async with session() as client:
            expected = release.digest
            if not expected:
                checksums = (await read_small(client, f"{RELEASES_URL}/download/{release.tag}/SHA256SUMS.txt", 65536)).decode("ascii")
                matches = [line.split() for line in checksums.splitlines()
                           if len(line.split()) == 2 and line.split()[1] == release.name]
                if len(matches) != 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", matches[0][0]):
                    raise ValueError("Release 缺少有效 SHA-256 校验值，拒绝安装")
                expected = matches[0][0].lower()
            response = await response_for(client, release.url)
            digest, received = hashlib.sha256(), 0
            try:
                with archive.open("xb") as output:
                    async for chunk in response.content.iter_chunked(256 * 1024):
                        if cancelled.is_set():
                            raise RuntimeError("已取消更新")
                        received += len(chunk)
                        if received > release.size or received > MAX_DOWNLOAD:
                            raise ValueError("更新下载超过声明大小")
                        output.write(chunk)
                        digest.update(chunk)
                        progress(received, release.size)
            finally:
                response.release()
            if received != release.size or digest.hexdigest() != expected:
                raise ValueError("更新包长度或 SHA-256 校验失败，未修改应用")
        progress(release.size, release.size)
        extraction = asyncio.create_task(asyncio.to_thread(
            extract_verified, archive, workspace / "payload", release.version, cancelled))
        await asyncio.shield(extraction)
        archive.unlink()
        return workspace
    except BaseException as exc:
        if isinstance(exc, asyncio.CancelledError):
            cancelled.set()
        if extraction:
            try:
                await extraction
            except (Exception, asyncio.CancelledError):
                pass
        shutil.rmtree(workspace, ignore_errors=True)
        if isinstance(exc, (aiohttp.ClientError, TimeoutError)):
            raise RuntimeError("下载连接失败或超时，应用未修改，请重试或手动下载。") from exc
        raise


def launch_helper(workspace, target, data_root):
    """Start an independent process. It will not rename anything before this PID exits."""
    script = workspace / "apply-update.ps1"
    shutil.copy2(resource_dir() / "apply-update.ps1", script)
    plan = dict(target=str(target), workspace=str(workspace), parent_pid=os.getpid(),
                data_root=str(Path(data_root).resolve()))
    plan_path = workspace / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    with native_launch():
        process = subprocess.Popen(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script), "-PlanPath", str(plan_path)],
            cwd=workspace, env=native_environment(), creationflags=subprocess.CREATE_NO_WINDOW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (workspace / "helper-ready").exists():
            return
        if process.poll() is not None:
            break
        time.sleep(0.05)
    # The helper requires an explicit arm file, so a late start cannot apply anything.
    raise RuntimeError(f"更新助手未能启动，应用保持运行。日志目录：{workspace}")
