"""Bounded RTP pacing; feedback and audio bypass video debt."""

import asyncio
import struct
import time


def wait_until(deadline):
    while (remaining := deadline - time.perf_counter()) > 0:
        time.sleep(remaining)


class FrameClock:
    def __init__(self, fps):
        self.period = 1 / fps
        self.deadline = None

    def wait(self):
        now = time.perf_counter()
        if self.deadline is None:
            self.deadline = now
        wait_until(self.deadline)
        now = time.perf_counter()
        missed = max(0, int((now - self.deadline) / self.period))
        self.deadline += (missed + 1) * self.period
        if self.deadline <= now:
            self.deadline += self.period


def install_rtp_pacer(pc, bitrate, fps):
    from aiortc.rtp import is_rtcp
    audio = {t.sender._ssrc for t in pc.getTransceivers() if t.kind == "audio"}
    for transport in {t.sender.transport for t in pc.getTransceivers()}:
        original = transport._send_rtp
        state = {"due": 0.0, "rate": bitrate * 1.08}
        credit = max(.008, min(2 / fps, .032))

        async def paced(data, send=original, budget=state):
            ssrc = struct.unpack_from("!I", data, 8)[0] if len(data) >= 12 else None
            if not is_rtcp(data) and ssrc not in audio:
                now = time.perf_counter()
                budget["due"] = max(budget["due"], now - credit) + len(data) * 8 / budget["rate"]
                delay = budget["due"] - now
                if delay > 0:
                    await asyncio.to_thread(wait_until, budget["due"])
            await send(data)

        transport._send_rtp = paced
        transport._elink_budget = state
