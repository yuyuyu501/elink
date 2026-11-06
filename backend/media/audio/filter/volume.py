"""音量滤镜（0.0~1.0 线性，带斜坡平滑）。

直接跳变音量会产生爆音，所以音量变化在 ``ramp_ms`` 时间内线性过渡：
每个样本的增益都不同，按帧内位置插值。
"""

from __future__ import annotations

import array
import sys

from backend.media.frame import Frame


class VolumeFilter:
    """用户可调音量，默认 1.0 表示不改变信号。"""

    name = "volume"

    def __init__(self, volume: float = 1.0, ramp_ms: float = 20.0) -> None:
        self._volume = max(0.0, min(1.0, volume))
        self._applied = self._volume
        self.ramp_ms = ramp_ms

    @property
    def volume(self) -> float:
        return self._volume

    def set_volume(self, volume: float) -> None:
        self._volume = max(0.0, min(1.0, volume))

    def process(self, frame: Frame) -> Frame:
        if (frame.sample_format or "s16") != "s16":
            raise ValueError("VolumeFilter 当前只支持 s16 采样格式")

        samples = array.array("h")
        samples.frombytes(bytes(frame.data))
        if sys.byteorder == "big":
            samples.byteswap()

        channels = max(1, frame.channels)
        frames_count = len(samples) // channels
        target = self._volume

        if abs(target - self._applied) < 1e-4 or frames_count == 0:
            self._applied = target
            if abs(target - 1.0) < 1e-4:
                return frame
            scaled = array.array(
                "h",
                (max(-32768, min(32767, int(v * target))) for v in samples),
            )
        else:
            sample_rate = frame.sample_rate or 48_000
            ramp_len = max(1, int(sample_rate * self.ramp_ms / 1000.0))
            ramp_len = min(ramp_len, frames_count)
            step = (target - self._applied) / ramp_len
            scaled = array.array("h", [0]) * len(samples)

            for index in range(frames_count):
                gain = self._applied if index >= ramp_len else self._applied + step * (index + 1)
                base = index * channels
                for channel in range(channels):
                    value = samples[base + channel]
                    scaled[base + channel] = max(-32768, min(32767, int(value * gain)))
            self._applied = target

        if sys.byteorder == "big":
            scaled.byteswap()
        return frame.with_data(scaled.tobytes())

    def close(self) -> None:
        pass
