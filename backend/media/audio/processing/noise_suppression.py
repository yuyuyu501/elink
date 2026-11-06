"""噪声抑制（自适应底噪门限）。

做法是**时域门控**而不是谱减法：

1. 用「慢速最小值跟踪」估计当前底噪（安静时下降快、有语音时几乎不动）；
2. 块内信噪比低于门限时按比例压低增益，避免把底噪放大；
3. 增益做平滑上升/下降，防止出现抽水式抖动。

时域门控算力极低、延迟为零，代价是只能抑制宽带稳态噪声（风扇、电流声），
对键盘声这类瞬态噪声无效——需要更强抑制时应换频谱类算法。
"""

from __future__ import annotations

import array
import math
import sys

from backend.media.frame import Frame


class NoiseSuppressor:
    name = "noise_suppression"

    def __init__(
        self,
        threshold_db: float = 6.0,
        floor_gain: float = 0.12,
        adaptation: float = 0.02,
    ) -> None:
        self.threshold_db = threshold_db
        self.floor_gain = floor_gain
        self.adaptation = adaptation
        self._noise_rms = 1.0
        self._gain = 1.0

    def process(self, frame: Frame) -> Frame:
        if (frame.sample_format or "s16") != "s16":
            raise ValueError("NoiseSuppressor 当前只支持 s16 采样格式")

        samples = array.array("h")
        samples.frombytes(bytes(frame.data))
        if sys.byteorder == "big":
            samples.byteswap()
        if not samples:
            return frame

        rms = math.sqrt(sum(float(v) * v for v in samples) / len(samples))

        # 慢速最小值跟踪：更小的 RMS 快速吸收，更大的 RMS 几乎不动
        if rms < self._noise_rms:
            self._noise_rms += (rms - self._noise_rms) * 0.5
        else:
            self._noise_rms += (rms - self._noise_rms) * self.adaptation

        snr_db = 20 * math.log10(max(rms, 1e-6) / max(self._noise_rms, 1e-6))
        if snr_db >= self.threshold_db:
            target = 1.0
        else:
            ratio = max(0.0, snr_db) / max(self.threshold_db, 1e-6)
            target = self.floor_gain + (1.0 - self.floor_gain) * ratio

        self._gain += (target - self._gain) * 0.3

        if self._gain >= 0.999:
            return frame

        gain = self._gain
        out = array.array(
            "h",
            (max(-32768, min(32767, int(value * gain))) for value in samples),
        )
        if sys.byteorder == "big":
            out.byteswap()
        return frame.with_data(out.tobytes())

    @property
    def noise_floor(self) -> float:
        return self._noise_rms

    def close(self) -> None:
        self._noise_rms = 1.0
        self._gain = 1.0
