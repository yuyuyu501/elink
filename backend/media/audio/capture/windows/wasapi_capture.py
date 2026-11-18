"""WASAPI system sound loopback, owned by the audio worker thread."""

from backend.media.frame import Frame
from backend.media.stream import MediaType
from backend.media.timebase import Timebase


class WasapiCapture:
    backend = "wasapi-loopback"

    def __init__(self, sample_rate=48000, channels=2, frame_ms=20, timebase=None):
        self.sample_rate, self.channels = sample_rate, channels
        self.samples = round(sample_rate * frame_ms / 1000)
        self.timebase = timebase or Timebase(1, sample_rate)
        self._recorder = None
        self._samples = 0
        self._com_initialized = False

    def open(self):
        import soundcard as sc
        import os
        if os.name == "nt":
            import ctypes
            self._com_initialized = ctypes.windll.ole32.CoInitializeEx(None, 0) in (0, 1)
        speaker = sc.default_speaker()
        if speaker is None:
            raise RuntimeError("没有可用的系统播放设备")
        microphone = sc.get_microphone(speaker.id, include_loopback=True)
        self._recorder = microphone.recorder(samplerate=self.sample_rate, channels=self.channels,
                                             blocksize=self.samples)
        self._recorder.__enter__()

    def read(self):
        import numpy as np
        pixels = self._recorder.record(numframes=self.samples)
        pixels = (np.clip(pixels, -1, 1) * 32767).astype("<i2")
        frame = Frame(data=pixels.tobytes(), media_type=MediaType.AUDIO, sample_rate=self.sample_rate,
                      channels=self.channels, samples=len(pixels), sample_format="s16",
                      pts=self._samples, timebase=self.timebase)
        self._samples += len(pixels)
        return frame

    def close(self):
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            recorder.__exit__(None, None, None)
        if self._com_initialized:
            import ctypes
            ctypes.windll.ole32.CoUninitialize()
            self._com_initialized = False
