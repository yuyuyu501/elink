import asyncio
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import threading
import time
import zipfile

import pytest

from elink import updates


def entry(version, **values):
    name = f"Elink-{version}-windows-x64.zip"
    result = dict(tag_name=f"v{version}", draft=False, prerelease=False, assets=[dict(
        name=name, browser_download_url=f"{updates.RELEASES_URL}/download/v{version}/{name}",
        size=100, digest="sha256:" + "a" * 64)])
    result.update(values)
    return result


def test_release_order_channels_and_asset_selection():
    entries = [entry("0.9.0"), entry("0.10.0"), entry("1.0.0-rc.2", prerelease=True),
               entry("1.0.0-rc.10", prerelease=True), entry("2.0.0", draft=True),
               entry("3.0.0", assets=[]), entry("invalid")]
    assert updates.select_release(entries, "0.4.0").version == "1.0.0-rc.10"
    assert updates.select_release(entries, "0.4.0", False).version == "0.10.0"
    assert updates.select_release(entries, "1.0.0") is None
    assert updates.select_release([entry("0.4.0")], "0.4.0") is None
    assert updates.version_key("1.0.0") > updates.version_key("1.0.0-rc.10")
    altered = entry("9.0.0")
    altered["assets"][0]["browser_download_url"] = "https://example.com/evil.exe"
    assert updates.select_release([altered]) is None


@pytest.mark.parametrize("url", ["http://github.com/a", "https://github.com.evil.com/a",
                                      "https://github.com@evil.com/a", "file:///C:/x",
                                      "https://github.com:444/a"])
def test_reject_untrusted_downloads(url):
    assert not updates.allowed_url(url)


def bundle_bytes(version="0.5.0", extra=None):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        bundle.writestr("Elink/Elink.exe", b"test executable")
        bundle.writestr("Elink/_internal/runtime.dll", b"test runtime")
        bundle.writestr("Elink/build-info.json", json.dumps({"elink": version}))
        if extra:
            member = zipfile.ZipInfo("placeholder")
            member.filename = extra  # Avoid Windows ZipInfo normalizing backslashes.
            bundle.writestr(member, b"unsafe")
    return data.getvalue()


def extract(tmp_path, data, version="0.5.0"):
    archive = tmp_path / "update.zip"
    archive.write_bytes(data)
    return updates.extract_verified(archive, tmp_path / "payload", version, threading.Event())


@pytest.mark.parametrize("name", ["../outside", "/outside", "Elink/../../outside", "Elink\\outside",
                                 "Elink/C:outside", "Elink/CON.txt", "Elink/name. ",
                                 "Elink/./bad", "Elink//bad", "Elink/ELINK.EXE"])
def test_reject_unsafe_archives_before_extracting(tmp_path, name):
    with pytest.raises(ValueError):
        extract(tmp_path, bundle_bytes(extra=name))
    assert not (tmp_path / "payload").exists()


def test_symlink_archive_rejected(tmp_path):
    data = io.BytesIO(bundle_bytes())
    with zipfile.ZipFile(data, "a") as archive:
        link = zipfile.ZipInfo("Elink/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside")
    with pytest.raises(ValueError):
        extract(tmp_path, data.getvalue())


def test_extract_requires_matching_version(tmp_path):
    with pytest.raises(ValueError, match="版本"):
        extract(tmp_path, bundle_bytes(), "0.6.0")


class Content:
    def __init__(self, data):
        self.data = data

    async def iter_chunked(self, size):
        for offset in range(0, len(self.data), size):
            await asyncio.sleep(0)
            yield self.data[offset:offset + size]


class Response:
    def __init__(self, data=b"", status=200, headers=None):
        self.content = Content(data)
        self.status, self.headers = status, headers or {}
        self.released = False

    def release(self):
        self.released = True


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, **kwargs):
        return next(self.responses)


def test_redirect_is_checked_before_request():
    response = Response(status=302, headers={"Location": "https://example.com/payload"})
    with pytest.raises(ValueError, match="非 GitHub"):
        asyncio.run(updates.response_for(Session([response]), "https://github.com/test"))
    assert response.released


@pytest.mark.parametrize("failure", [None, "checksum", "truncated", "oversized", "cancel"])
def test_download_validation_and_active_install_untouched(tmp_path, monkeypatch, failure):
    data = bundle_bytes()
    release = updates.select_release([entry("0.5.0")])
    release = replace(release, size=len(data), digest=hashlib.sha256(data).hexdigest())
    cancelled = threading.Event()
    if failure == "checksum":
        release = replace(release, digest="0" * 64)
    elif failure == "truncated":
        data = data[:-5]
    elif failure == "oversized":
        data += b"excess"
    elif failure == "cancel":
        cancelled.set()
    monkeypatch.setattr(updates, "session", lambda: Session([Response(data)]))
    target = tmp_path / "Elink"
    target.mkdir()
    (target / "Elink.exe").write_bytes(b"old")
    run = updates.prepare_update(release, target, cancelled, lambda *_: None)
    if failure:
        with pytest.raises((ValueError, RuntimeError)):
            asyncio.run(run)
        assert not list(tmp_path.glob(".Elink-update-*"))
        assert cancelled.is_set() == (failure == "cancel")
    else:
        workspace = asyncio.run(run)
        assert (workspace / "payload/Elink/Elink.exe").read_bytes() == b"test executable"
        assert not (workspace / "update.zip").exists()
    assert (target / "Elink.exe").read_bytes() == b"old"


def test_checksum_file_fallback(tmp_path, monkeypatch):
    data = bundle_bytes()
    release = replace(updates.select_release([entry("0.5.0")]), size=len(data), digest="")
    checksum = f"{hashlib.sha256(data).hexdigest()}  {release.name}\n".encode()
    monkeypatch.setattr(updates, "session", lambda: Session([Response(checksum), Response(data)]))
    workspace = asyncio.run(updates.prepare_update(release, tmp_path / "Elink", threading.Event(), lambda *_: None))
    assert (workspace / "payload/Elink/Elink.exe").exists()


def test_source_cannot_replace_python_installation(tmp_path):
    with pytest.raises(RuntimeError, match="源码"):
        updates.installation_dir(tmp_path)


def test_portable_update_protects_user_data(tmp_path, monkeypatch):
    target = tmp_path / "Elink"
    (target / "_internal").mkdir(parents=True)
    (target / "build-info.json").write_text(json.dumps({"elink": updates.__version__}))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(target / "Elink.exe"))
    assert updates.installation_dir(tmp_path / "userdata") == target
    with pytest.raises(RuntimeError, match="用户数据"):
        updates.installation_dir(target / "userdata")


@pytest.mark.parametrize("armed,broken", [(False, False), (True, True), (True, False)])
def test_powershell_transaction_and_startup_rollback(tmp_path, armed, broken):
    """Exercise the real helper with isolated fake applications; no installed files touched."""
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    target = tmp_path / "Elink spaces 中文 [1] 'quoted'"
    target.mkdir()
    (target / "old-marker").write_text("preserve")
    workspace = tmp_path / ".Elink-update-fixture"
    payload = workspace / "payload/Elink"
    payload.mkdir(parents=True)
    # Compile only a tiny test fixture, not an Elink release or driver.
    compiler = tmp_path / "fixture.ps1"
    compiler.write_text("""param([string]$Output)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Threading;
public class Fixture {
    public static void Main() {
        string path = Environment.GetEnvironmentVariable("ELINK_UPDATE_READY");
        if (!String.IsNullOrEmpty(path)) {
            File.WriteAllText(path, "ready");
            Thread.Sleep(1500);
        }
    }
}
'@ -OutputAssembly $Output -OutputType WindowsApplication
""", encoding="utf-8-sig")
    subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                    "-File", str(compiler), str(tmp_path / "fixture.exe")], check=True, capture_output=True)
    shutil.copy2(tmp_path / "fixture.exe", target / "Elink.exe")
    if broken:
        (payload / "Elink.exe").write_bytes(b"not executable")
    else:
        shutil.copy2(target / "Elink.exe", payload / "Elink.exe")
    parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                              creationflags=subprocess.CREATE_NO_WINDOW)
    helper = None
    try:
        plan = workspace / "plan.json"
        plan.write_text(json.dumps(dict(target=str(target), workspace=str(workspace),
                                       parent_pid=parent.pid, data_root=str(tmp_path / "data"))), encoding="utf-8")
        helper = subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                   "-File", str(updates.resource_dir() / "apply-update.ps1"), "-PlanPath", str(plan)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 15
        while not (workspace / "helper-ready").exists() and helper.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (workspace / "helper-ready").exists(), helper.communicate(timeout=2)
        assert (target / "old-marker").exists()
        if armed:
            (workspace / "armed").write_text("ready")
        parent.terminate()
        parent.wait(timeout=5)
        stdout, stderr = helper.communicate(timeout=15)
        if armed and not broken:
            assert helper.returncode == 0, (stdout, stderr, (workspace / "update.log").read_text(encoding="utf-8-sig"))
            assert (workspace / "previous/old-marker").exists()
            assert not (target / "old-marker").exists()
            assert (workspace / "app-ready").exists()
        else:
            assert helper.returncode == 1
            assert (target / "old-marker").read_text() == "preserve"
            if broken:
                assert (workspace / "failed/Elink.exe").read_bytes() == b"not executable"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if helper and helper.poll() is None:
            helper.kill()
            helper.communicate(timeout=5)
