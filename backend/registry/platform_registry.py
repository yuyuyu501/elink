"""单平台实现注册表。

采集、渲染、注入这类「每平台一份」的能力统一在这里登记与查找：
上层只请求能力（:class:`~backend.platform.Capability`），由注册表决定用哪份实现，
且优先使用当前平台、其次按注册顺序回退（例如 D3D11 渲染失败时退到 OpenGL）。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from backend.errors.base import ElinkError, ErrorCode
from backend.logging.logger import get_logger
from backend.platform import Capability, Platform, detect

logger = get_logger(__name__)

Factory = Callable[..., object]


class ImplementationNotFoundError(ElinkError):
    code = ErrorCode.NOT_SUPPORTED


@dataclass(frozen=True)
class Implementation:
    """一条实现登记项。"""

    capability: Capability
    name: str
    platform: Platform | None
    factory: Factory
    priority: int = 0

    def matches(self, platform: Platform) -> bool:
        return self.platform is None or self.platform is platform


class PlatformRegistry:
    """按「能力 + 平台」索引的工厂注册表。"""

    def __init__(self) -> None:
        self._items: dict[Capability, list[Implementation]] = {}
        self._lock = threading.RLock()

    def register(
        self,
        capability: Capability,
        name: str,
        factory: Factory,
        *,
        platform: Platform | None = None,
        priority: int = 0,
    ) -> None:
        with self._lock:
            self._items.setdefault(capability, []).append(
                Implementation(capability, name, platform, factory, priority)
            )

    def names(self, capability: Capability) -> list[str]:
        platform = detect().platform
        with self._lock:
            return [
                item.name
                for item in self._candidates(capability, platform)
            ]

    def create(
        self,
        capability: Capability,
        *args,
        prefer: Iterable[str] = (),
        **kwargs,
    ):
        """创建实现实例；``prefer`` 中的名字优先，其余按优先级回退。"""
        platform = detect().platform
        candidates = self._candidates(capability, platform)
        if not candidates:
            raise ImplementationNotFoundError(
                f"没有为 {capability.value} 注册任何实现",
                detail=f"platform={platform.value}",
            )

        preferred = list(prefer)
        ordered = sorted(
            candidates,
            key=lambda item: (
                preferred.index(item.name) if item.name in preferred else len(preferred),
                -item.priority,
            ),
        )

        errors: list[str] = []
        for item in ordered:
            try:
                instance = item.factory(*args, **kwargs)
            except Exception as exc:  # 逐个回退，最后统一报错
                errors.append(f"{item.name}: {exc}")
                logger.debug("实现 %s 创建失败，尝试下一个", item.name, exc_info=True)
                continue
            if item is not ordered[0]:
                logger.info("回退到平台实现: %s", item.name)
            return instance

        raise ImplementationNotFoundError(
            f"{capability.value} 的全部实现都创建失败",
            detail="; ".join(errors),
        )

    def _candidates(
        self, capability: Capability, platform: Platform
    ) -> list[Implementation]:
        with self._lock:
            items = self._items.get(capability, [])
            return [item for item in items if item.matches(platform)]


def build_default_registry() -> PlatformRegistry:
    """登记当前已接入的平台实现。

    说明：这里只登记「能被导入」的实现，导入失败（缺少可选依赖）时跳过，
    让能力协商自然回退，而不是在启动阶段直接崩掉。
    """
    registry = PlatformRegistry()

    def try_register(capability: Capability, name: str, module: str, attr: str, **kwargs) -> None:
        try:
            module_object = __import__(module, fromlist=[attr])
            factory = getattr(module_object, attr)
        except ImportError as exc:  # 可选依赖缺失
            logger.debug("跳过平台实现 %s: %s", name, exc)
            return
        registry.register(capability, name, factory, **kwargs)

    try_register(
        Capability.SCREEN_CAPTURE,
        "gdi",
        "backend.media.video.capture.windows.dxgi_desktop_duplication",
        "DesktopCapture",
        platform=Platform.WINDOWS,
        priority=10,
    )
    try_register(
        Capability.SCREEN_CAPTURE,
        "x11grab",
        "backend.media.video.capture.linux.pipewire",
        "create_capture",
        platform=Platform.LINUX,
        priority=10,
    )
    try_register(
        Capability.SCREEN_CAPTURE,
        "media_projection",
        "backend.media.video.capture.android.media_projection",
        "create_capture",
        platform=Platform.ANDROID,
        priority=10,
    )
    try_register(
        Capability.INPUT_CAPTURE,
        "send_input_hook",
        "backend.input_capture.windows.send_input_hook",
        "SendInputHookCapture",
        platform=Platform.WINDOWS,
        priority=10,
    )
    try_register(
        Capability.INPUT_INJECTION,
        "send_input",
        "backend.input_injection.windows.send_input",
        "SendInputInjector",
        platform=Platform.WINDOWS,
        priority=10,
    )
    return registry
