"""Frame deadlines for a worker thread, independent of asyncio timer ticks."""
import math
import time


def wait_until(deadline, clock=time.perf_counter, sleep=time.sleep):
    # CPython 3.11+ uses a high-resolution waitable timer on Windows. Do not
    # busy-spin or change the system-wide timer resolution. Recheck early wakes.
    while (remaining := deadline - clock()) > 0:
        sleep(remaining)


class FrameClock:
    def __init__(self, fps, clock=time.perf_counter, sleep=time.sleep):
        if not math.isfinite(fps) or fps <= 0:
            raise ValueError('Frame rate must be positive')
        self.period = 1 / fps
        self.clock, self.sleep = clock, sleep
        self.deadline = None

    def wait(self):
        now = self.clock()
        if self.deadline is None:
            self.deadline = now
        due = self.deadline
        wait_until(due, self.clock, self.sleep)
        now = self.clock()
        late = max(0.0, now - due)
        skipped = int(late / self.period)
        self.deadline = due + (skipped + 1) * self.period
        # A floating-point division exactly at a slot boundary can round down.
        if self.deadline <= now:
            self.deadline += self.period
            skipped += 1
        return late, skipped
