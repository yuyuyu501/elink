"""Bounded RTP pacing; feedback and audio bypass video debt."""

import asyncio
import struct
import time
import socket


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


class RtpPacer:
    def __init__(self, bitrate, fps):
        self.update(bitrate, fps)
        self.due = self.peak_due = 0.0
        self.lock = asyncio.Lock()
        self.retransmit_lock = asyncio.Lock()

    def update(self, bitrate, fps):
        self.rate = max(1, bitrate) * 1.08
        self.credit = max(.008, min(2 / fps, .032))

    async def send(self, data, send, retransmit=False):
        async with (self.retransmit_lock if retransmit else self.lock):
            now = time.perf_counter()
            self.due = max(self.due, now - self.credit) + len(data) * 8 / self.rate
            self.peak_due = max(self.peak_due, now - .004) + len(data) * 8 / (self.rate * 1.25)
            due = max(self.due, self.peak_due)
            if due > now:
                await asyncio.to_thread(wait_until, due)
            await send(data)


def mark_rtc_sockets(pc):
    updated = 0
    try:
        protocols = tuple(pc.sctp.transport.transport._connection._protocols)
    except AttributeError:
        return updated
    for protocol in protocols:
        try:
            sock = protocol.transport.get_extra_info("socket")
            if sock.family == socket.AF_INET:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 34 << 2)
            elif sock.family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS, 34 << 2)
            else:
                continue
            updated += 1
        except (OSError, AttributeError, ValueError):
            pass
    return updated


def install_rtp_pacer(pc, bitrate, fps):
    from aiortc.rtp import is_rtcp
    audio = {t.sender._ssrc for t in pc.getTransceivers() if t.kind == "audio"}
    retransmit = {t.sender._rtx_ssrc for t in pc.getTransceivers()
                  if t.kind == "video" and hasattr(t.sender, "_rtx_ssrc")}
    for transport in {t.sender.transport for t in pc.getTransceivers()}:
        existing = getattr(transport, "_elink_pacer", None)
        if existing is not None:
            existing.update(bitrate, fps)
            continue
        original = transport._send_rtp
        state = RtpPacer(bitrate, fps)

        async def paced(data, send=original, budget=state):
            ssrc = struct.unpack_from("!I", data, 8)[0] if len(data) >= 12 else None
            if not is_rtcp(data) and ssrc not in audio:
                return await budget.send(data, send, ssrc in retransmit)
            return await send(data)

        transport._send_rtp = paced
        transport._elink_pacer = state
