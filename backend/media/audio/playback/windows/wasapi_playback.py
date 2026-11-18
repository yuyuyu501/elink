"""Persistent low latency output with a bounded callback buffer."""

import threading
from collections import deque


class WasapiPlayback:
    backend = "portaudio"

    def __init__(self, sample_rate=48000, channels=2, **_):
        self.sample_rate, self.channels = sample_rate, channels
        self.muted = False
        self._stream = None
        self._queue = deque(maxlen=4)
        self._pending = None
        self._lock = threading.Lock()

    def open(self):
        import sounddevice as sd
        self._stream = sd.OutputStream(samplerate=self.sample_rate, channels=self.channels,
                                       dtype="float32", latency="low", callback=self._callback)
        self._stream.start()

    def write(self, frame):
        import numpy as np
        pixels = np.frombuffer(frame.data, dtype="<i2").reshape(-1, frame.channels).astype("float32") / 32768
        self.write_samples(pixels)
        return True

    def write_samples(self, pixels):
        with self._lock:
            self._queue.append(pixels)

    def _callback(self, output, frames, time_info, status):
        output.fill(0)
        at = 0
        with self._lock:
            while at < frames:
                if self._pending is None or not len(self._pending):
                    if not self._queue:
                        break
                    self._pending = self._queue.popleft()
                count = min(frames - at, len(self._pending))
                if not self.muted:
                    output[at:at + count] = self._pending[:count]
                self._pending = self._pending[count:]
                at += count

    def close(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            self._queue.clear()
            self._pending = None
