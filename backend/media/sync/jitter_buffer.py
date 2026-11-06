"""抖动缓冲：吸收乱序与网络抖动。

原则：

- **有序**：按序号排序后再交给解码器，解码器最怕乱序（丢帧/花屏）；
- **有界**：容量固定，满了就丢最旧的（视频）——内存不能随网络乱序无限增长；
- **自适应**：延迟按「观测到的最大乱序跨度」增长，稳定后再慢慢收回，
  否则固定大延迟会白白增加端到端时延。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class BufferedItem:
    sequence: int
    timestamp: int
    payload: bytes
    arrived_at: float


class JitterBuffer:
    """按序出包的抖动缓冲。"""

    def __init__(
        self,
        capacity: int = 128,
        min_delay_ms: float = 20.0,
        max_delay_ms: float = 400.0,
    ) -> None:
        self.capacity = capacity
        self.min_delay_ms = min_delay_ms
        self.max_delay_ms = max_delay_ms
        self._buffer: "OrderedDict[int, BufferedItem]" = OrderedDict()
        self._lock = threading.Lock()
        self._next_sequence: int | None = None
        self._delay_ms = min_delay_ms
        self._max_span = 0
        self.dropped_late = 0
        self.dropped_full = 0

    @property
    def delay_ms(self) -> float:
        return self._delay_ms

    def push(self, sequence: int, timestamp: int, payload: bytes) -> None:
        with self._lock:
            if self._next_sequence is not None:
                span = (sequence - self._next_sequence) & 0xFFFF
                if span > 0x8000:
                    # 迟到包：已经放出去了，直接丢
                    self.dropped_late += 1
                    return
                self._max_span = max(self._max_span, span)
                self._adapt()

            self._buffer[sequence] = BufferedItem(
                sequence=sequence,
                timestamp=timestamp,
                payload=payload,
                arrived_at=time.monotonic(),
            )
            while len(self._buffer) > self.capacity:
                self._buffer.popitem(last=False)
                self.dropped_full += 1

    def _adapt(self) -> None:
        """延迟 = 最小延迟 + 最大乱序跨度对应的时长，夹在上下限内。"""
        estimated = self.min_delay_ms + self._max_span
        self._delay_ms = min(self.max_delay_ms, max(self.min_delay_ms, estimated))
        # 慢速衰减，避免一次乱序后永久高延迟
        self._max_span = max(0, self._max_span - 1)

    def pop_ready(self) -> BufferedItem | None:
        """取出可以交付的下一项；未到交付时间或缺口未补齐时返回 None。"""
        with self._lock:
            if not self._buffer:
                return None

            if self._next_sequence is None:
                # 起始点取缓冲区里最小的序号：首包乱序到达时也能先排序再交付
                self._next_sequence = min(self._buffer)

            item = self._buffer.get(self._next_sequence)
            if item is None:
                # 缺口：等一个延迟窗口，超时就跳过（跳过即视为丢包）
                oldest = min(self._buffer.values(), key=lambda entry: entry.arrived_at)
                if (time.monotonic() - oldest.arrived_at) * 1000 >= self._delay_ms:
                    self._next_sequence = min(self._buffer)
                    item = self._buffer[self._next_sequence]
                else:
                    return None

            delay_ms = (time.monotonic() - item.arrived_at) * 1000
            if delay_ms < self._delay_ms:
                return None  # 还没攒够延迟

            del self._buffer[self._next_sequence]
            self._next_sequence = (self._next_sequence + 1) & 0xFFFF
            return item

    def flush(self) -> list[BufferedItem]:
        """关流时把剩下的按序吐出来。"""
        with self._lock:
            items = sorted(self._buffer.values(), key=lambda item: item.sequence)
            self._buffer.clear()
            return items

    def stats(self) -> dict[str, float]:
        with self._lock:
            return {
                "buffered": float(len(self._buffer)),
                "delay_ms": self._delay_ms,
                "dropped_late": float(self.dropped_late),
                "dropped_full": float(self.dropped_full),
            }

    def reset(self) -> None:
        with self._lock:
            self._buffer.clear()
            self._next_sequence = None
            self._max_span = 0
            self._delay_ms = self.min_delay_ms
