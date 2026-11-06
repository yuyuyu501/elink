"""指标采集器。

只做「记数」，不做「换算」：计数器、瞬时值、时间区间都在这里登记，
聚合（帧率/码率/丢包率）由 ``metrics/stats.py`` 负责。
线程安全：采集发生在采集/编码/网络各线程，读发生在主循环。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class MetricValue:
    """一条指标的当前值。"""

    name: str
    value: float = 0.0
    updated_at: float = field(default_factory=time.monotonic)


class MetricsCollector:
    """线程安全的计数器 / 瞬时值容器。"""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._gauges: dict[str, float] = {}
        self._lock = threading.RLock()

    # ---- 写入 ----
    def increment(self, name: str, amount: int = 1) -> int:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount
            return self._counters[name]

    def add(self, name: str, amount: float) -> float:
        """给瞬时值累加（例如累计字节数）。"""
        with self._lock:
            self._gauges[name] = self._gauges.get(name, 0.0) + amount
            return self._gauges[name]

    def set(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def reset(self, name: str) -> None:
        with self._lock:
            self._counters.pop(name, None)
            self._gauges.pop(name, None)

    def clear(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()

    # ---- 读取 ----
    def counter(self, name: str) -> int:
        with self._lock:
            return self._counters.get(name, 0)

    def gauge(self, name: str) -> float:
        with self._lock:
            return self._gauges.get(name, 0.0)

    def snapshot(self) -> dict[str, float]:
        """计数器与瞬时值合并成一份只读快照。"""
        with self._lock:
            merged: dict[str, float] = {}
            merged.update({f"{k}": float(v) for k, v in self._counters.items()})
            merged.update(self._gauges)
            return merged

    def values(self) -> list[MetricValue]:
        now = time.monotonic()
        with self._lock:
            items = [
                MetricValue(name, float(value), now)
                for name, value in {**self._counters, **self._gauges}.items()
            ]
        return items


class ScopedTimer:
    """``with ScopedTimer(collector, "encode_ms"):`` 记录耗时（毫秒）。"""

    def __init__(self, collector: MetricsCollector, name: str) -> None:
        self.collector = collector
        self.name = name
        self._start = 0.0

    def __enter__(self) -> "ScopedTimer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc_info) -> None:
        elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        self.collector.add(self.name, elapsed_ms)
