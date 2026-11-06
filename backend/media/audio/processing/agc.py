"""自动增益控制（AGC）。

思路：以块为单位算 RMS，与目标电平比较得到「想要的增益」，再用攻击/释放两条
不同的时间常数平滑，避免忽大忽小。最终增益夹在 ``[min_gain, max_gain]`` 之间，
放大时会带上「防止削顶」的限幅，不会出现爆音。
"""

from __future__ import annotations

import array
import sys

from backend.media.frame import Frame


class AutomaticGainControl:
    name = "agc"

    def __init__(
        self,
        target_dbfs: float = -18.0,
        attack_ms: float = 5.0,
        release_ms: float = 200.0,
        max_gain_db: float = 30.0,
        min_gain_db: float = -6.0,
    ) -> None:
        self.target_dbfs = target_dbfs
        self.attack_ms = attack_ms
        self.release_ms = release_ms
        self.max_gain_db = max_gain_db
        self.min_gain_db = min_gain_db
        self._gain = 1.0

    def process(self, frame: Frame) -> Frame:
        if (frame.sample_format or "s16") != "s16":
            raise ValueError("AGC 当前只支持 s16 采样格式")

        samples = array.array("h")
        samples.frombytes(bytes(frame.data))
        if sys.byteorder == "big":
            samples.byteswap()
        if not samples:
            return frame

        rms = (sum(float(value) * value for value in samples) / len(samples)) ** 0.5
        rms_dbfs = 20 * _log10(max(rms, 1e-6) / 32768.0)
        error_db = self.target_dbfs - rms_dbfs

        desired_db = max(self.min_gain_db, min(self.max_gain_db, error_db))
        desired = 10 ** (desired_db / 20.0)

        # 增益下降（信号变响）走攻击，增益上升走释放
        sample_rate = frame.sample_rate or 48_000
        block_ms = 1000.0 * len(samples) / (sample_rate * max(1, frame.channels))
        if desired < self._gain:
            smoothing = _alpha(self.attack_ms, block_ms)
        else:
            smoothing = _alpha(self.release_ms, block_ms)
        self._gain += (desired - self._gain) * smoothing

        gain = self._gain
        out = array.array(
            "h",
            (max(-32768, min(32767, int(value * gain))) for value in samples),
        )
        if sys.byteorder == "big":
            out.byteswap()
        return frame.with_data(out.tobytes())

    @property
    def gain(self) -> float:
        return self._gain

    def close(self) -> None:
        self._gain = 1.0


def _alpha(time_ms: float, block_ms: float) -> float:
    """一阶平滑系数：时间常数越大，单块调整比例越小。"""
    if time_ms <= 0:
        return 1.0
    return min(1.0, block_ms / time_ms)


def _log10(value: float) -> float:
    import math

    return math.log10(value)
