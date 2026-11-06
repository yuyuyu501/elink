"""增益滤镜（dB）。

与 ``volume.py`` 的分工：``gain`` 是链路里的固定校准（例如统一不同采集设备的
电平），一次设定不再变化；``volume`` 是用户可随时调的可变音量。
"""

from __future__ import annotations

import array
import math
import sys

from backend.media.frame import Frame


def db_to_linear(db: float) -> float:
    return math.pow(10.0, db / 20.0)


class GainFilter:
    """按分贝放大/衰减 16bit PCM。"""

    name = "gain"

    def __init__(self, gain_db: float = 0.0) -> None:
        self.gain_db = gain_db
        self._factor = db_to_linear(gain_db)

    def set_gain_db(self, gain_db: float) -> None:
        self.gain_db = gain_db
        self._factor = db_to_linear(gain_db)

    def process(self, frame: Frame) -> Frame:
        if (frame.sample_format or "s16") != "s16":
            raise ValueError("GainFilter 当前只支持 s16 采样格式")

        samples = array.array("h")
        samples.frombytes(bytes(frame.data))
        if sys.byteorder == "big":
            samples.byteswap()

        factor = self._factor
        clamped = array.array(
            "h",
            (
                max(-32768, min(32767, int(value * factor)))
                for value in samples
            ),
        )
        if sys.byteorder == "big":
            clamped.byteswap()
        return frame.with_data(clamped.tobytes())

    def close(self) -> None:
        pass
