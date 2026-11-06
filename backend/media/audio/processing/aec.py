"""回声消除（NLMS 自适应滤波）。

原理：把「扬声器参考信号」当作输入，把「麦克风信号」当作期望信号，用 NLMS
估计房间的冲激响应，再从麦克风信号里减掉它的预测值，剩下的就是近端语音。

约束与取舍：

- 逐样本处理，无块延迟，适合实时链路；
- ``filter_length`` 决定可消除的回声时延（16kHz 下 256 taps ≈ 16ms），
  房间混响较长时需要加大，代价是算力；
- 只在单声道参考下工作最好；立体声会按声道各自独立自适应。
"""

from __future__ import annotations

import array
import sys

from backend.media.frame import Frame


class EchoCanceller:
    name = "aec"

    def __init__(
        self,
        filter_length: int = 256,
        step_size: float = 0.3,
        channels: int = 1,
    ) -> None:
        if filter_length <= 0:
            raise ValueError("filter_length 必须大于 0")
        self.filter_length = filter_length
        self.step_size = step_size
        self.channels = max(1, channels)
        self._weights = [[0.0] * filter_length for _ in range(self.channels)]
        self._history = [[0.0] * filter_length for _ in range(self.channels)]

    def process(self, near_end: Frame, far_end: Frame | None = None) -> Frame:
        """``near_end`` 是麦克风信号，``far_end`` 是远端播放参考。

        ``far_end`` 为空时退化为「降噪前的直通」，用于参考信号缺失的场景。
        """
        if far_end is None:
            return near_end

        near = self._to_float(near_end)
        far = self._to_float(far_end)
        channels = min(self.channels, near_end.channels or 1, far_end.channels or 1)
        frames = min(len(near), len(far)) // max(1, near_end.channels or 1)
        if frames == 0:
            return near_end

        near_channels = near_end.channels or 1
        far_channels = far_end.channels or 1
        out = array.array("h", [0]) * (frames * near_channels)

        for channel in range(channels):
            weights = self._weights[channel]
            history = self._history[channel]

            for index in range(frames):
                reference = far[index * far_channels + min(channel, far_channels - 1)]
                history.insert(0, reference)
                history.pop()

                predicted = 0.0
                for tap in range(self.filter_length):
                    predicted += weights[tap] * history[tap]

                desired = near[index * near_channels + min(channel, near_channels - 1)]
                error = desired - predicted

                # NLMS：按参考能量归一化步长
                energy = 0.0
                for tap in range(self.filter_length):
                    energy += history[tap] * history[tap]
                norm = self.step_size / (energy + 1e-6)

                for tap in range(self.filter_length):
                    weights[tap] += norm * error * history[tap]

                out[index * near_channels + channel] = max(
                    -32768, min(32767, int(error))
                )

        for index in range(frames * near_channels):
            if index % near_channels >= channels:
                # 未参与自适应的声道（比如超低音）原样保留
                out[index] = near[index]

        if sys.byteorder == "big":
            out.byteswap()
        return near_end.with_data(out.tobytes())

    def reset(self) -> None:
        self._weights = [[0.0] * self.filter_length for _ in range(self.channels)]
        self._history = [[0.0] * self.filter_length for _ in range(self.channels)]

    def close(self) -> None:
        self.reset()

    @staticmethod
    def _to_float(frame: Frame) -> array.array:
        if (frame.sample_format or "s16") != "s16":
            raise ValueError("EchoCanceller 当前只支持 s16 采样格式")
        samples = array.array("h")
        samples.frombytes(bytes(frame.data))
        if sys.byteorder == "big":
            samples.byteswap()
        return samples
