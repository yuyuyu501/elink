"""设备权限申请。

各平台差异很大，所以做成「检查 -> 申请 -> 再检查」的统一流程，
由平台实现决定具体动作：

- Windows：录屏/麦克风没有系统级权限弹窗（设置里可全局关闭），主要检查
  会话是否可访问桌面（服务进程拿不到）；
- macOS：屏幕录制与麦克风需要 TCC 授权，只能引导用户去「系统设置」开启；
- Android：需要运行时权限（RECORD_AUDIO）与 MediaProjection 授权。

拿不到权限时统一抛 ``errors/permission.py`` 里的异常，而不是让采集在
第一帧时才神秘失败。
"""

from __future__ import annotations

import enum

from backend.errors.permission import (
    PermissionDeniedError,
    PermissionRequestError,
)
from backend.logging.logger import get_logger
from backend.platform import Platform, detect

logger = get_logger(__name__)


class PermissionKind(str, enum.Enum):
    SCREEN_CAPTURE = "screen_capture"
    MICROPHONE = "microphone"
    INPUT_INJECTION = "input_injection"


def check(kind: PermissionKind) -> bool:
    """只检查不申请；返回当前是否已有权限。"""
    platform = detect().platform

    if platform is Platform.WINDOWS:
        if kind is PermissionKind.SCREEN_CAPTURE:
            return detect().interactive
        return True

    if platform is Platform.MACOS:
        # TCC 的检查需要调用系统 API（Quartz/AVFoundation），Python 侧置为
        # 「未知 -> 视为需要申请」，由用户按提示开启
        return False

    if platform is Platform.ANDROID:
        return False if kind is PermissionKind.MICROPHONE else True

    return True


def request(kind: PermissionKind) -> bool:
    """申请权限；平台不支持自动申请时抛 ``PermissionRequestError``。"""
    platform = detect().platform

    if check(kind):
        return True

    if platform is Platform.MACOS:
        raise PermissionRequestError(
            f"macOS 需要手动授予 {kind.value} 权限：系统设置 -> 隐私与安全性",
            detail="屏幕录制需要「屏幕录制」勾选，麦克风需要「麦克风」勾选",
        )

    if platform is Platform.ANDROID and kind is PermissionKind.MICROPHONE:
        try:
            from jnius import autoclass  # type: ignore
        except ImportError as exc:
            raise PermissionRequestError(
                "Android 申请麦克风权限需要 pyjnius 与 Activity 上下文"
            ) from exc
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        activity.requestPermissions(["android.permission.RECORD_AUDIO"], 1001)
        raise PermissionRequestError(
            "已在系统中发起授权，请在应用内确认后重试",
            detail="requestPermissions(RECORD_AUDIO)",
        )

    if platform is Platform.WINDOWS and kind is PermissionKind.SCREEN_CAPTURE:
        raise PermissionDeniedError(
            "当前进程拿不到交互桌面（可能是服务/会话 0 隔离）",
            detail="请在登录用户的桌面会话中运行，或改用可交互的服务账户",
        )

    raise PermissionDeniedError(f"缺少 {kind.value} 权限")


def ensure(kind: PermissionKind) -> None:
    """检查并按需申请，失败抛异常；调用方无需关心平台差异。"""
    if check(kind):
        return
    request(kind)


__all__ = ["PermissionKind", "check", "request", "ensure"]
