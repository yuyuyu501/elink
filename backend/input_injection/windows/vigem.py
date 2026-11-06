"""ViGEmBus 兼容手柄注入（Xbox 360 虚拟手柄）。

只做 Windows 本机注入：主播端把远端发来的手柄状态写入虚拟 Xbox 360 手柄。
依赖两样东西：

- ``ViGEmClient.dll``（随应用资源分发）；
- 系统已安装 **ViGEmBus** 驱动（未安装时构造失败，界面会提示，不影响键鼠/串流）。

与 ``E:\\elink`` 的兼容模式保持相同的按钮/摇杆布局；震动反馈通过回调把
``left/right`` 强度发回调用方。
"""

from __future__ import annotations

import ctypes as C
import threading
from ctypes import wintypes as W
from pathlib import Path

VIGEM_ERROR_NONE = 0x20000000

if hasattr(C, "WinDLL"):
    _dll_type = C.WinDLL
else:  # pragma: no cover - 仅 Windows
    _dll_type = C.CDLL


class _Pad(C.Structure):
    _fields_ = [
        ("buttons", C.c_ushort),
        ("lt", C.c_ubyte),
        ("rt", C.c_ubyte),
        ("lx", C.c_short),
        ("ly", C.c_short),
        ("rx", C.c_short),
        ("ry", C.c_short),
    ]


_FEEDBACK = C.CFUNCTYPE(None, C.c_void_p, C.c_void_p, C.c_ubyte, C.c_ubyte, C.c_ubyte, C.c_void_p)


class VirtualPads:
    """按索引维护一个或多个虚拟 Xbox 360 手柄。"""

    def __init__(self, dll_path: Path, feedback=None) -> None:
        if not dll_path.is_file():
            raise RuntimeError(f"缺少 ViGEmClient.dll：{dll_path}")
        self._dll = _dll_type(str(dll_path))
        signatures = {
            "vigem_alloc": ([], C.c_void_p),
            "vigem_free": ([C.c_void_p], None),
            "vigem_connect": ([C.c_void_p], C.c_uint),
            "vigem_disconnect": ([C.c_void_p], None),
            "vigem_target_x360_alloc": ([], C.c_void_p),
            "vigem_target_free": ([C.c_void_p], None),
            "vigem_target_add": ([C.c_void_p, C.c_void_p], C.c_uint),
            "vigem_target_remove": ([C.c_void_p, C.c_void_p], C.c_uint),
            "vigem_target_x360_update": ([C.c_void_p, C.c_void_p, _Pad], C.c_uint),
            "vigem_target_x360_get_user_index": (
                [C.c_void_p, C.c_void_p, C.POINTER(C.c_ulong)],
                C.c_uint,
            ),
            "vigem_target_x360_register_notification": (
                [C.c_void_p, C.c_void_p, C.c_void_p, C.c_void_p],
                C.c_uint,
            ),
            "vigem_target_x360_unregister_notification": ([C.c_void_p], None),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self._dll, name)
            function.argtypes, function.restype = args, result

        self._feedback = feedback
        self._lock = threading.Lock()
        self._targets: dict[int, tuple[int, object]] = {}
        self._client = self._dll.vigem_alloc()
        if not self._client or self._dll.vigem_connect(self._client) != VIGEM_ERROR_NONE:
            if self._client:
                self._dll.vigem_free(self._client)
            self._client = None
            raise RuntimeError("手柄注入需要主机已安装 ViGEmBus 驱动；键鼠与串流仍可使用。")

    @property
    def available(self) -> bool:
        return self._client is not None

    def update(self, index: int, values) -> None:
        if self._client is None:
            return
        values = list(values)
        if len(values) != 7:
            return
        with self._lock:
            if index not in self._targets:
                target = self._dll.vigem_target_x360_alloc()
                if not target or self._dll.vigem_target_add(self._client, target) != VIGEM_ERROR_NONE:
                    if target:
                        self._dll.vigem_target_free(target)
                    raise RuntimeError("无法创建虚拟 Xbox 360 手柄。")
                callback = _FEEDBACK(
                    lambda _client, _pad, large, small, _led, _user, i=index: self._notify(i, large, small)
                )
                self._targets[index] = (target, callback)
                self._dll.vigem_target_x360_register_notification(self._client, target, callback, None)
            target = self._targets[index][0]
            if self._dll.vigem_target_x360_update(self._client, target, _Pad(*values)) != VIGEM_ERROR_NONE:
                raise RuntimeError("虚拟手柄状态更新失败。")

    def _notify(self, index: int, large: int, small: int) -> None:
        if self._feedback is None:
            return
        try:
            self._feedback(index, int(large) * 257, int(small) * 257)
        except Exception:
            pass

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
            if client is None:
                return
            for target, _callback in self._targets.values():
                try:
                    self._dll.vigem_target_x360_update(client, target, _Pad())
                    self._dll.vigem_target_x360_unregister_notification(target)
                    self._dll.vigem_target_remove(client, target)
                    self._dll.vigem_target_free(target)
                except Exception:
                    pass
            self._targets.clear()
            try:
                self._dll.vigem_disconnect(client)
                self._dll.vigem_free(client)
            except Exception:
                pass

    def __del__(self) -> None:  # pragma: no cover - 进程退出兜底
        try:
            self.close()
        except Exception:
            pass
