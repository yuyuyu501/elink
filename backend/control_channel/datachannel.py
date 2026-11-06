"""WebRTC DataChannel 控制通道。

DataChannel 本身是「消息型」的（不会粘包），但不同实现有两种风格：

- 同步回调风格（大多数 Python 绑定）：``send(bytes)`` + ``on_message`` 回调；
- 异步风格（aiortc 等）：``send`` 是协程。

这里用「鸭子类型 + 适配器」把两种都接上：只要能 ``send``、并且能被挂上
消息回调，就是合法后端。底层框架的差异不该污染控制协议。
"""

from __future__ import annotations

import asyncio
import inspect
import threading

from backend.control_channel import protocol
from backend.control_channel.base import ChannelState, ControlChannel
from backend.errors.network import TransportClosedError
from backend.event.bus import EventBus
from backend.logging.logger import get_logger

logger = get_logger(__name__)


class DataChannelControlChannel(ControlChannel):
    kind = "datachannel"

    def __init__(self, channel, *, bus: EventBus | None = None, loop: asyncio.AbstractEventLoop | None = None) -> None:
        super().__init__(bus)
        self.channel = channel
        self._loop = loop
        self._decoder = protocol.FrameDecoder()

    def open(self) -> None:
        attach = getattr(self.channel, "on", None) or getattr(self.channel, "set_listener", None)
        if attach is None:
            raise TransportClosedError(
                "DataChannel 后端缺少消息回调接口（需要 on/set_listener）"
            )
        attach("message", self._on_channel_message)
        self.state = ChannelState.OPEN
        logger.info("DataChannel 控制通道已就绪")

    def close(self) -> None:
        close = getattr(self.channel, "close", None)
        if callable(close):
            close()
        self.state = ChannelState.CLOSED

    def _write(self, data: bytes) -> None:
        """DataChannel 是消息型的，data 已经是完整协议帧，直接交给后端。"""
        self.send_raw(data)

    def send_raw(self, data: bytes) -> None:
        """DataChannel 是消息型的，直接发协议帧即可（无需再包一层长度）。"""
        if self.state != ChannelState.OPEN:
            raise protocol.ProtocolError(f"控制通道未就绪: {self.state}")
        send = getattr(self.channel, "send", None)
        if send is None:
            raise TransportClosedError("DataChannel 后端缺少 send 接口")
        try:
            result = send(data)
        except OSError as exc:
            self.state = ChannelState.FAILED
            raise TransportClosedError("DataChannel 发送失败", detail=str(exc)) from exc
        if inspect.isawaitable(result):
            loop = self._loop or _get_loop()
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(result, loop).result(timeout=5)
            else:  # 没有运行中的事件循环：直接跑完
                asyncio.run(_await(result))
        self.sent_bytes += len(data)

    def _on_channel_message(self, *args) -> None:
        """兼容 ``(message)`` / ``(channel, message)`` 两种回调签名。"""
        payload = args[-1] if args else b""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            logger.debug("忽略非字节消息: %r", type(payload))
            return

        frames = self._decoder.push(bytes(payload))
        if frames:
            for frame_type, data in frames:
                if frame_type is protocol.FrameType.JSON:
                    self._dispatch(data)
        else:
            # 对端可能直接发裸 JSON（不是我们的帧格式）：兜底当 JSON 处理
            try:
                self._dispatch(bytes(payload))
            except protocol.ProtocolError:
                logger.debug("无法解析的 DataChannel 消息，已忽略")


def _get_loop() -> asyncio.AbstractEventLoop:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, name="datachannel-loop", daemon=True).start()
        return loop


async def _await(awaitable):
    return await awaitable
