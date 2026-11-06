"""混音：把多路 PCM 帧叠加成一路。

用于「系统声音 + 麦克风」这类多源场景。叠加后按声道做饱和截断，不做自动增益，
否则会掩盖真实的电平问题；需要压限请在采集侧先做 AGC。
"""

from __future__ import annotations

import array
import sys

from backend.errors.base import ElinkError, ErrorCode
from backend.media.frame import Frame


class MixerError(ElinkError):
    code = ErrorCode.INVALID_ARGUMENT


class AudioMixer:
    """等长帧叠加；采样率/声道/格式不一致时直接报错，不做隐式转换。"""

    name = "mixer"

    def __init__(self, max_inputs: int = 4) -> None:
        self.max_inputs = max_inputs

    def mix(self, frames: list[Frame]) -> Frame:
        if not frames:
            raise MixerError("混音需要至少一路输入")
        if len(frames) > self.max_inputs:
            raise MixerError(
                f"输入路数超过上限 {self.max_inputs}", detail=str(len(frames))
            )

        base = frames[0]
        for frame in frames[1:]:
            if (
                frame.sample_rate != base.sample_rate
                or frame.channels != base.channels
                or (frame.sample_format or "s16") != (base.sample_format or "s16")
            ):
                raise MixerError(
                    "混音输入参数不一致",
                    detail=(
                        f"{base.sample_rate}Hz/{base.channels}ch/"
                        f"{base.sample_format} vs "
                        f"{frame.sample_rate}Hz/{frame.channels}ch/{frame.sample_format}"
                    ),
                )
            if len(frame.data) != len(base.data):
                raise MixerError(
                    "混音输入长度不一致",
                    detail=f"{len(base.data)} != {len(frame.data)}",
                )

        accumulators = []
        for frame in frames:
            samples = array.array("h")
            samples.frombytes(bytes(frame.data))
            if sys.byteorder == "big":
                samples.byteswap()
            accumulators.append(samples)

        mixed = array.array("h", [0]) * len(accumulators[0])
        for index in range(len(mixed)):
            total = 0
            for samples in accumulators:
                total += samples[index]
            mixed[index] = max(-32768, min(32767, total))

        if sys.byteorder == "big":
            mixed.byteswap()
        return base.with_data(mixed.tobytes())

    def process(self, frame: Frame) -> Frame:
        """单路输入时是恒等变换，便于放进滤镜链。"""
        return frame

    def close(self) -> None:
        pass
