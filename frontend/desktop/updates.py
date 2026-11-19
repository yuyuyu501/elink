"""Verified GitHub portable updates; directory replacement runs after exit."""

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
from urllib.request import Request, urlopen
import zipfile

from . import __version__
from .paths import data_dir, resource_dir

RELEASES_URL = "https://github.com/yuyuyu501/elink/releases"
API_URL = "https://api.github.com/repos/yuyuyu501/elink/releases?per_page=100"
MAX_DOWNLOAD = 1024 ** 3
MAX_EXPANDED = 3 * MAX_DOWNLOAD


def version_key(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-([\w.-]+))?", value)
    if not match:
        raise ValueError("版本号无效")
    return (*map(int, match.group(1, 2, 3)), match[4] is None, match[4] or "")


def allowed_url(url):
    parts = urlsplit(url)
    return (parts.scheme == "https" and not parts.username and not parts.password
            and parts.port in (None, 443) and parts.hostname in {
                "api.github.com", "github.com", "release-assets.githubusercontent.com",
                "objects.githubusercontent.com"})


def response_for(url):
    if not allowed_url(url):
        raise ValueError("更新下载来源无效")
    response = urlopen(Request(url, headers={"User-Agent": f"Elink/{__version__}"}), timeout=30)
    if not allowed_url(response.geturl()):
        response.close()
        raise ValueError("更新重定向来源无效")
    return response


def read_small(url, limit):
    with response_for(url) as response:
        value = response.read(limit + 1)
    if len(value) > limit:
        raise ValueError("更新响应过大")
    return value


def select_release(entries, current=__version__, include_preview=True):
    candidates = []
    for entry in entries:
        try:
            tag = entry["tag_name"]
            key = version_key(tag)
            if entry.get("draft") or (not include_preview and (entry.get("prerelease") or not key[3])):
                continue
            if key <= version_key(current):
                continue
            version = tag.removeprefix("v")
            name = f"Elink-{version}-windows-x64.zip"
            assets = {asset["name"]: asset for asset in entry["assets"]}
            urls = {}
            for filename in (name, "SHA256SUMS.txt"):
                asset = assets[filename]
                url = f"{RELEASES_URL}/download/{tag}/{filename}"
                if (asset["browser_download_url"] != url or type(asset["size"]) is not int
                        or not 0 < asset["size"] <= MAX_DOWNLOAD):
                    raise ValueError("发布资产无效")
                urls[filename] = url
            candidates.append((key, dict(version=version, tag=tag, name=name, urls=urls,
                                         size=assets[name]["size"], page=f"{RELEASES_URL}/tag/{tag}")))
        except (KeyError, TypeError, ValueError):
            continue
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def check_release(include_preview=True):
    return select_release(json.loads(read_small(API_URL, 4 * 1024 * 1024)), include_preview=include_preview)


def installation_dir():
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    target = Path(sys.executable).resolve().parent
    if (any(target.glob("unins*.exe")) or target.is_symlink()
            or (data_dir().resolve() == target or target in data_dir().resolve().parents)):
        return None
    return target


def extract_verified(archive, destination, cancelled):
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        if len(members) > 50000 or sum(item.file_size for item in members) > MAX_EXPANDED:
            raise ValueError("更新解压大小超限")
        seen = set()
        for item in members:
            path = PurePosixPath(item.filename)
            if (not path.parts or path.parts[0] != "Elink" or path.is_absolute()
                    or any(p in {".", ".."} or ":" in p or p.rstrip(" .") != p for p in path.parts)
                    or "\\" in item.filename or "\0" in item.filename
                    or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError("更新包包含无效路径")
            key = str(path).casefold()
            if key in seen:
                raise ValueError("更新包包含重复路径")
            seen.add(key)
            if cancelled.is_set():
                raise InterruptedError("更新已取消")
            bundle.extract(item, destination)
    executable = destination / "Elink" / "Elink.exe"
    if not executable.is_file() or not (executable.parent / "_internal").is_dir():
        raise ValueError("更新包缺少完整程序")
    return executable


def prepare_update(release, target, cancelled, progress):
    root = data_dir() / "updates"
    root.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="download-", dir=root))
    staging = None
    try:
        if cancelled.is_set():
            raise InterruptedError("更新已取消")
        if target.is_symlink() or not (target / "Elink.exe").is_file():
            raise ValueError("更新目标不是完整程序目录")
        checksums = read_small(release["urls"]["SHA256SUMS.txt"], 65536).decode("ascii")
        matches = [line.split() for line in checksums.splitlines() if len(line.split()) == 2
                   and line.split()[1].lstrip("*") == release["name"]]
        if len(matches) != 1 or not re.fullmatch("[a-fA-F0-9]{64}", matches[0][0]):
            raise ValueError("缺少更新包 SHA-256")
        archive = workspace / release["name"]
        digest, received = hashlib.sha256(), 0
        with response_for(release["urls"][release["name"]]) as response, archive.open("wb") as output:
            while chunk := response.read(256 * 1024):
                if cancelled.is_set():
                    raise InterruptedError("更新已取消")
                received += len(chunk)
                if received > release["size"] or received > MAX_DOWNLOAD:
                    raise ValueError("更新包大小超限")
                digest.update(chunk)
                output.write(chunk)
                progress(received, release["size"])
        if received != release["size"] or digest.hexdigest().lower() != matches[0][0].lower():
            raise ValueError("更新包大小或 SHA-256 校验失败")
        executable = extract_verified(archive, workspace / "payload", cancelled)
        environment = native_environment()
        result = subprocess.run([str(executable), "--version"], capture_output=True, text=True,
                                timeout=30, env=environment,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or result.stdout.strip() != release["version"]:
            raise ValueError("更新程序版本校验失败")
        if cancelled.is_set():
            raise InterruptedError("更新已取消")
        staging = Path(tempfile.mkdtemp(prefix=".Elink-update-", dir=target.parent))
        shutil.copytree(workspace / "payload", staging / "payload")
        if cancelled.is_set():
            raise InterruptedError("更新已取消")
        return staging
    except BaseException:
        if staging is not None:
            shutil.rmtree(staging)
        raise
    finally:
        shutil.rmtree(workspace)


def native_environment():
    environment = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return environment


def launch_helper(workspace, target, root):
    script = workspace / "apply-update.ps1"
    shutil.copy2(resource_dir() / script.name, script)
    plan = workspace / "plan.json"
    plan.write_text(json.dumps(dict(target=str(target), workspace=str(workspace),
                                   parent_pid=os.getpid(), data_root=str(root.resolve()))), encoding="utf-8")
    executable = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    process = subprocess.Popen(
        [str(executable), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), "-PlanPath", str(plan)], cwd=workspace, env=native_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if (workspace / "helper-ready").exists():
            return process
        if process.poll() is not None:
            break
        time.sleep(.05)
    raise RuntimeError("更新助手未就绪，当前应用继续运行。")
