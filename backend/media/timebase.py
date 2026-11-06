"""时间基与时间戳换算。

``Timebase(num, den)`` 表示「1 个 tick = num/den 秒」，例如 MPEG-TS 的
``Timebase(1, 90000)``。所有跨模块传递的时间戳都必须带上它的时间基，
禁止裸数字跨层流动。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Timebase:
    num: int = 1
    den: int = 90_000

    def __post_init__(self) -> None:
        if self.num <= 0 or self.den <= 0:
            raise ValueError(f"非法时间基: {self.num}/{self.den}")

    # ---- 工厂 ----
    @classmethod
    def mpegts(cls) -> "Timebase":
        """MPEG-TS / PES 使用的 90kHz。"""
        return cls(1, 90_000)

    @classmethod
    def microseconds(cls) -> "Timebase":
        return cls(1, 1_000_000)

    @classmethod
    def milliseconds(cls) -> "Timebase":
        return cls(1, 1_000)

    # ---- 换算 ----
    def seconds(self, ticks: int) -> float:
        return ticks * self.num / self.den

    def ticks(self, seconds: float) -> int:
        return int(round(seconds * self.den / self.num))

    def rescale(self, ticks: int, target: "Timebase") -> int:
        """把本时间基下的 ticks 换算到 target 时间基（四舍五入）。"""
        if target == self:
            return ticks
        return int(round(ticks * self.num * target.den / (self.den * target.num)))

    def duration_ticks(self, other: "Timebase") -> int:
        """本时间基下一秒对应 target 的 tick 数。"""
        return self.rescale(self.den // self.num, other)

    def __str__(self) -> str:
        return f"{self.num}/{self.den}"


TIMEBASE_90K = Timebase.mpegts()
