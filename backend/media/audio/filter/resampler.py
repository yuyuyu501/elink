"""重采样（线性插值）。

线性插值不是高保真重采样（无抗混叠滤波），但延迟为 0、实现可预测，适合实时
语音/桌面音频。高保真场景应改用 ffmpeg 的 ``aresample``（本类保留为轻量路径）。

跨帧连续性：保存上一帧最后一个样本，避免帧边界出现相位跳变。
"""

from __future__ import annotations

import array
import sys

from backend.media.frame import Frame


class Resampler:
    name = "resampler"

    def __init__(self, target_rate: int, channels: int | None = None) -> None:
        if target_rate <= 0:
            raise ValueError("目标采样率必须大于 0")
        self.target_rate = target_rate
        self.channels = channels
        self._last_sample: list[float] = []
        self._phase = 0.0

    def process(self, frame: Frame) -> Frame:
        source_rate = frame.sample_rate or 48_000
        channels = self.channels or frame.channels or 1
        if source_rate == self.target_rate and channels == frame.channels:
            return frame
        if (frame.sample_format or "s16") != "s16":
            raise ValueError("Resampler 当前只支持 s16 采样格式")

        samples = array.array("h")
        samples.frombytes(bytes(frame.data))
        if sys.byteorder == "big":
            samples.byteswap()

        source_frames = len(samples) // max(1, frame.channels or 1)
        if source_frames == 0:
            return frame

        ratio = self.target_rate / source_rate
        out_frames = max(1, int(source_frames * ratio))

        if not self._last_sample:
            self._last_sample = [0.0] * channels

        out = array.array("h", [0]) * (out_frames * channels)
        source_channels = frame.channels or 1

        for out_index in range(out_frames):
            position = out_index / ratio
            left_index = int(position)
            frac = position - left_index
            right_index = min(left_index + 1, source_frames - 1)

            for channel in range(channels):
                source_channel = min(channel, source_channels - 1)
                left_value = (
                    self._last_sample[channel]
                    if left_index < 0
                    else samples[left_index * source_channels + source_channel]
                )
                right_value = samples[right_index * source_channels + source_channel]
                value = left_value + (right_value - left_value) * frac
                out[out_index * channels + channel] = max(
                    -32768, min(32767, int(value))
                )

        self._last_sample = [
            float(samples[(source_frames - 1) * source_channels + min(c, source_channels - 1)])
            for c in range(channels)
        ]

        if sys.byteorder == "big":
            out.byteswap()

        return frame.with_data(
            out.tobytes(),
            sample_rate=self.target_rate,
            channels=channels,
            samples=out_frames,
        )

    def close(self) -> None:
        self._last_sample = []
