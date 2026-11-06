"""统计聚合：帧率 / 码率 / 延迟 / 丢包。

所有聚合器都用「滑动窗口」而不用「累计平均」：实时链路上累计平均会把
突发抖动永久摊平，看不出当前质量。窗口内的样本按时间戳淘汰。
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass, field


class SlidingWindow:
    """按时间窗口保留样本。"""

    def __init__(self, window_s: float = 2.0) -> None:
        self.window_s = window_s
        self._samples: collections.deque[tuple[float, float]] = collections.deque()
        self._lock = threading.Lock()

    def add(self, value: float, timestamp: float | None = None) -> None:
        now = time.monotonic() if timestamp is None else timestamp
        with self._lock:
            self._samples.append((now, value))
            self._evict(now)

    def _evict(self, now: float) -> None:
        edge = now - self.window_s
        while self._samples and self._samples[0][0] < edge:
            self._samples.popleft()

    def sum(self) -> float:
        with self._lock:
            self._evict(time.monotonic())
            return sum(value for _, value in self._samples)

    def count(self) -> int:
        with self._lock:
            self._evict(time.monotonic())
            return len(self._samples)

    def mean(self) -> float:
        total = self.sum()
        count = self.count()
        return total / count if count else 0.0

    def latest(self) -> float | None:
        with self._lock:
            return self._samples[-1][1] if self._samples else None


class RateMeter:
    """计数速率（帧率 / 包率）。"""

    def __init__(self, window_s: float = 2.0) -> None:
        self.window = SlidingWindow(window_s)

    def tick(self, count: int = 1) -> None:
        self.window.add(count)

    @property
    def rate(self) -> float:
        return self.window.sum() / self.window.window_s

    def snapshot(self) -> dict[str, float]:
        return {"rate": round(self.rate, 2), "samples": self.window.count()}


class BitrateMeter:
    """字节速率（kbps）。"""

    def __init__(self, window_s: float = 2.0) -> None:
        self.window = SlidingWindow(window_s)

    def add_bytes(self, size: int) -> None:
        self.window.add(float(size))

    @property
    def kbps(self) -> float:
        return self.window.sum() * 8 / 1000 / self.window.window_s

    @property
    def bytes_total(self) -> float:
        return self.window.sum()

    def snapshot(self) -> dict[str, float]:
        return {"kbps": round(self.kbps, 1)}


class LatencyTracker:
    """端到端延迟（毫秒）：喂入「发送时刻 + 到达时刻」的差值。"""

    def __init__(self, window_s: float = 5.0) -> None:
        self.window = SlidingWindow(window_s)

    def observe(self, latency_ms: float) -> None:
        self.window.add(latency_ms)

    @property
    def mean_ms(self) -> float:
        return self.window.mean()

    @property
    def last_ms(self) -> float:
        latest = self.window.latest()
        return latest if latest is not None else 0.0

    def snapshot(self) -> dict[str, float]:
        return {"latency_ms": round(self.mean_ms, 1), "last_ms": round(self.last_ms, 1)}


class LossTracker:
    """丢包率：需要序列号连续性。"""

    def __init__(self) -> None:
        self._expected: int | None = None
        self._received = 0
        self._lost = 0
        self._lock = threading.Lock()

    def observe(self, sequence: int) -> None:
        with self._lock:
            if self._expected is None:
                self._expected = sequence + 1
                self._received += 1
                return
            if sequence >= self._expected:
                self._lost += sequence - self._expected
                self._expected = sequence + 1
            self._received += 1

    @property
    def loss_rate(self) -> float:
        with self._lock:
            total = self._received + self._lost
            return self._lost / total if total else 0.0

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {
                "loss_rate": round(self._lost / (self._received + self._lost), 4)
                if (self._received + self._lost)
                else 0.0,
                "received": float(self._received),
                "lost": float(self._lost),
            }


@dataclass
class StreamStats:
    """一路流的统计集合。"""

    fps: RateMeter = field(default_factory=RateMeter)
    packet_rate: RateMeter = field(default_factory=RateMeter)
    bitrate: BitrateMeter = field(default_factory=BitrateMeter)
    latency: LatencyTracker = field(default_factory=LatencyTracker)
    loss: LossTracker = field(default_factory=LossTracker)
    dropped_frames: int = 0

    def on_frame(self, dropped: bool = False) -> None:
        if dropped:
            self.dropped_frames += 1
        else:
            self.fps.tick()

    def on_packet(self, size: int, sequence: int | None = None) -> None:
        self.packet_rate.tick()
        self.bitrate.add_bytes(size)
        if sequence is not None:
            self.loss.observe(sequence)

    def snapshot(self) -> dict[str, float]:
        snapshot: dict[str, float] = {}
        snapshot.update({f"fps_{k}": v for k, v in self.fps.snapshot().items()})
        snapshot.update({f"pkt_{k}": v for k, v in self.packet_rate.snapshot().items()})
        snapshot.update(self.bitrate.snapshot())
        snapshot.update(self.latency.snapshot())
        snapshot.update(self.loss.snapshot())
        snapshot["dropped_frames"] = float(self.dropped_frames)
        return snapshot


class StatsAggregator:
    """多路统计的汇总出口，供 metrics 上报或日志打印。"""

    def __init__(self) -> None:
        self._streams: dict[str, StreamStats] = {}
        self._lock = threading.Lock()

    def stream(self, name: str = "video") -> StreamStats:
        with self._lock:
            if name not in self._streams:
                self._streams[name] = StreamStats()
            return self._streams[name]

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {name: stats.snapshot() for name, stats in self._streams.items()}

    def format(self) -> str:
        parts = []
        for name, values in self.snapshot().items():
            fps = values.get("fps_rate", 0.0)
            kbps = values.get("kbps", 0.0)
            loss = values.get("loss_rate", 0.0) * 100
            parts.append(f"{name}: {fps:.1f}fps {kbps:.0f}kbps loss={loss:.2f}%")
        return " | ".join(parts)
