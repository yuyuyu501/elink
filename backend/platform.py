"""平台探测与能力差异封装。

上层不允许直接 `os.name == "nt"` 判断平台，一律走这里：既能集中处理
「Android 也是 Linux」这类边界，也让能力探测（能否采集/注入/硬解）有唯一出口。
"""

from __future__ import annotations

import enum
import functools
import os
import platform as _platform
import sys
from dataclasses import dataclass, field


class Platform(str, enum.Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    ANDROID = "android"
    MACOS = "macos"
    UNKNOWN = "unknown"


class Capability(str, enum.Enum):
    """运行时能力（与具体实现无关，是否能做取决于后端是否接入）。"""

    SCREEN_CAPTURE = "screen_capture"
    WINDOW_CAPTURE = "window_capture"
    AUDIO_CAPTURE = "audio_capture"
    AUDIO_PLAYBACK = "audio_playback"
    INPUT_CAPTURE = "input_capture"
    INPUT_INJECTION = "input_injection"
    GPU_D3D11 = "gpu_d3d11"
    GPU_D3D12 = "gpu_d3d12"
    GPU_OPENGL = "gpu_opengl"
    HW_VIDEO_ENCODE = "hw_video_encode"
    HW_VIDEO_DECODE = "hw_video_decode"


_CAPABILITY_MATRIX: dict[Platform, frozenset[Capability]] = {
    Platform.WINDOWS: frozenset(
        {
            Capability.SCREEN_CAPTURE,
            Capability.WINDOW_CAPTURE,
            Capability.AUDIO_CAPTURE,
            Capability.AUDIO_PLAYBACK,
            Capability.INPUT_CAPTURE,
            Capability.INPUT_INJECTION,
            Capability.GPU_D3D11,
            Capability.GPU_D3D12,
            Capability.GPU_OPENGL,
            Capability.HW_VIDEO_ENCODE,
            Capability.HW_VIDEO_DECODE,
        }
    ),
    Platform.LINUX: frozenset(
        {
            Capability.SCREEN_CAPTURE,
            Capability.WINDOW_CAPTURE,
            Capability.AUDIO_CAPTURE,
            Capability.AUDIO_PLAYBACK,
            Capability.INPUT_CAPTURE,
            Capability.INPUT_INJECTION,
            Capability.GPU_OPENGL,
            Capability.HW_VIDEO_ENCODE,
            Capability.HW_VIDEO_DECODE,
        }
    ),
    Platform.ANDROID: frozenset(
        {
            Capability.SCREEN_CAPTURE,
            Capability.AUDIO_CAPTURE,
            Capability.AUDIO_PLAYBACK,
            Capability.INPUT_CAPTURE,
            Capability.INPUT_INJECTION,
            Capability.GPU_OPENGL,
            Capability.HW_VIDEO_ENCODE,
            Capability.HW_VIDEO_DECODE,
        }
    ),
    Platform.MACOS: frozenset(
        {
            Capability.SCREEN_CAPTURE,
            Capability.WINDOW_CAPTURE,
            Capability.AUDIO_CAPTURE,
            Capability.AUDIO_PLAYBACK,
            Capability.INPUT_CAPTURE,
            Capability.INPUT_INJECTION,
            Capability.GPU_OPENGL,
        }
    ),
    Platform.UNKNOWN: frozenset(),
}


def _is_android() -> bool:
    if "ANDROID_ARGUMENT" in os.environ or "ANDROID_ROOT" in os.environ:
        return True
    return "android" in _platform.platform().lower()


def detect_platform() -> Platform:
    """当前运行平台。注意 Android 也会返回 linux 的内核信息，需先判 Android。"""
    if sys.platform.startswith("win"):
        return Platform.WINDOWS
    if sys.platform.startswith("darwin"):
        return Platform.MACOS
    if _is_android():
        return Platform.ANDROID
    if sys.platform.startswith("linux"):
        return Platform.LINUX
    return Platform.UNKNOWN


def detect_arch() -> str:
    machine = _platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"i386", "i686", "x86"}:
        return "x86"
    return machine or "unknown"


@dataclass(frozen=True)
class PlatformInfo:
    """一次探测的结果快照（进程生命周期内不变）。"""

    platform: Platform
    arch: str
    release: str = ""
    interactive: bool = True
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @property
    def is_windows(self) -> bool:
        return self.platform is Platform.WINDOWS

    @property
    def is_android(self) -> bool:
        return self.platform is Platform.ANDROID

    def describe(self) -> str:
        return (
            f"{self.platform.value}/{self.arch} release={self.release or '?'} "
            f"interactive={self.interactive}"
        )


@functools.lru_cache(maxsize=1)
def detect() -> PlatformInfo:
    """探测平台信息（带缓存：进程内不变）。"""
    target = detect_platform()
    interactive = True
    if target is Platform.WINDOWS:
        # 会话 0 隔离：服务里拿不到桌面，采集/注入都会失败
        interactive = bool(os.environ.get("SESSIONNAME")) or bool(
            os.environ.get("USERNAME")
        )
    return PlatformInfo(
        platform=target,
        arch=detect_arch(),
        release=_platform.release(),
        interactive=interactive,
        capabilities=_CAPABILITY_MATRIX[target],
    )


def supports(capability: Capability) -> bool:
    return detect().supports(capability)


def require(capability: Capability) -> None:
    """能力不满足时抛出明确错误，而不是让调用方自己猜。"""
    from backend.errors.permission import CapabilityNotSupportedError

    info = detect()
    if not info.supports(capability):
        raise CapabilityNotSupportedError(
            f"当前平台不支持 {capability.value}", detail=info.describe()
        )
