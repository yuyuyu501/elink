"""设备枚举（显示器 / 摄像头 / 麦克风 / 扬声器）。

统一成一个 ``DeviceInfo`` 列表，让上层（会话协商、前端设备下拉框）不需要
关心各平台怎么查设备：

- 显示器：走 ``device/display.py``；
- 摄像头/麦克风：Windows 上用 ``ffmpeg -list_devices``（dshow），
  Linux 上扫 ``/dev/video*`` 并用 ``arecord -l`` 兜底。
"""

from __future__ import annotations

import enum
import glob
import shutil
import subprocess
import sys
from dataclasses import dataclass

from backend.logging.logger import get_logger
from backend.media.device.display import DisplayInfo, list_displays

logger = get_logger(__name__)


class DeviceKind(str, enum.Enum):
    DISPLAY = "display"
    CAMERA = "camera"
    MICROPHONE = "microphone"
    SPEAKER = "speaker"


@dataclass(frozen=True)
class DeviceInfo:
    kind: DeviceKind
    identifier: str
    name: str
    is_default: bool = False
    detail: dict | None = None


def _ffmpeg_binary() -> str | None:
    return shutil.which("ffmpeg")


def _list_dshow_devices() -> dict[str, list[str]]:
    """Windows：解析 ffmpeg 的 dshow 设备清单（输出走 stderr）。"""
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return {}

    try:
        process = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("dshow 设备枚举失败", exc_info=True)
        return {}

    devices: dict[str, list[str]] = {"video": [], "audio": []}
    current: str | None = None
    for line in process.stderr.splitlines():
        if "(video)" in line:
            current = "video"
        elif "(audio)" in line:
            current = "audio"
        if current and '"' in line:
            name = line.split('"')[1] if line.count('"') >= 2 else ""
            if name and name not in devices[current]:
                devices[current].append(name)
    return devices


def _list_v4l2_devices() -> list[str]:
    return sorted(glob.glob("/dev/video*"))


def _list_alsa_devices() -> list[str]:
    arecord = shutil.which("arecord")
    if not arecord:
        return []
    try:
        output = subprocess.run(
            [arecord, "-l"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    devices = []
    for line in output.splitlines():
        if line.startswith("card ") and "device" in line:
            devices.append(line.strip())
    return devices


def enumerate_devices(kind: DeviceKind | None = None) -> list[DeviceInfo]:
    """按类别枚举设备；``kind`` 为空时返回全部。"""
    devices: list[DeviceInfo] = []

    if kind in (None, DeviceKind.DISPLAY):
        for display in list_displays():
            devices.append(
                DeviceInfo(
                    kind=DeviceKind.DISPLAY,
                    identifier=display.name,
                    name=display.describe(),
                    is_default=display.primary,
                    detail={
                        "x": display.x,
                        "y": display.y,
                        "width": display.width,
                        "height": display.height,
                    },
                )
            )

    if sys.platform.startswith("win"):
        dshow = _list_dshow_devices()
        if kind in (None, DeviceKind.CAMERA):
            devices.extend(
                DeviceInfo(DeviceKind.CAMERA, name, name, index == 0)
                for index, name in enumerate(dshow.get("video", []))
            )
        if kind in (None, DeviceKind.MICROPHONE, DeviceKind.SPEAKER):
            devices.extend(
                DeviceInfo(DeviceKind.MICROPHONE, name, name, index == 0)
                for index, name in enumerate(dshow.get("audio", []))
            )
    elif sys.platform.startswith("linux"):
        if kind in (None, DeviceKind.CAMERA):
            devices.extend(
                DeviceInfo(DeviceKind.CAMERA, path, path, index == 0)
                for index, path in enumerate(_list_v4l2_devices())
            )
        if kind in (None, DeviceKind.MICROPHONE):
            devices.extend(
                DeviceInfo(DeviceKind.MICROPHONE, name, name, index == 0)
                for index, name in enumerate(_list_alsa_devices())
            )

    return devices


def find_device(kind: DeviceKind, identifier: str) -> DeviceInfo | None:
    for device in enumerate_devices(kind):
        if device.identifier == identifier:
            return device
    return None


def default_device(kind: DeviceKind) -> DeviceInfo | None:
    for device in enumerate_devices(kind):
        if device.is_default:
            return device
    devices = enumerate_devices(kind)
    return devices[0] if devices else None


__all__ = [
    "DeviceKind",
    "DeviceInfo",
    "enumerate_devices",
    "find_device",
    "default_device",
    "DisplayInfo",
]
