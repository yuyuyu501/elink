"""Best-effort Windows QoS marking for the WebRTC ICE sockets."""
from __future__ import annotations

import asyncio
import socket
import struct
import time

from .pacing import wait_until


VIDEO_DSCP = 34  # AF41: high-priority interactive video, leaving EF for voice.


def mark_rtc_sockets(pc, dscp: int = VIDEO_DSCP) -> int:
    """Mark local ICE UDP sockets and return the number successfully updated.

    This is intentionally best effort: Windows policy, Wi-Fi equipment and
    Tailscale/DERP may ignore or rewrite DSCP. It does not reserve bandwidth and
    never makes a connection fail when marking is unavailable.
    """
    value = max(0, min(int(dscp), 63)) << 2
    updated = 0
    try:
        ice = pc.sctp.transport.transport
        connection = ice._connection
        protocols = tuple(getattr(connection, "_protocols", ()))
    except AttributeError:
        return 0
    for protocol in protocols:
        try:
            sock = protocol.transport.get_extra_info("socket")
            if sock is None:
                continue
            if sock.family == socket.AF_INET:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, value)
            elif sock.family == socket.AF_INET6:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS, value)
            else:
                continue
            updated += 1
        except (AttributeError, OSError, ValueError):
            continue
    return updated


class RtpPacer:
    """Two bounded token schedules: average budget and short-term peak.

    Reserve tokens before waiting, including retransmissions. No encoded-frame
    drops and no background packet queue. Idle credit has a fixed upper bound.
    """
    def __init__(self, bitrate_mbps, fps=60, profile=None, budget=None):
        self.rate = bitrate_mbps * 1_000_000 * 1.08
        self.credit = max(.008, min(2 / fps, .032))
        self.profile, self.budget = profile, budget
        self.next_at = self.peak_at = 0.0
        self.lock = asyncio.Lock()
        self.rtx_lock = asyncio.Lock()
        self.frames = {}

    async def send(self, data, original, *, retransmit=False):
        queued = time.perf_counter()
        async with (self.rtx_lock if retransmit else self.lock):
            now = time.perf_counter()
            rate = self.budget.media_bps * 1.08 if self.budget else self.rate
            peak = rate * (1 if self.budget and self.budget.congested else 1.25)
            self.next_at = max(self.next_at, now - self.credit) + len(data) * 8 / rate
            self.peak_at = max(self.peak_at, now - .004) + len(data) * 8 / peak
            due = max(self.next_at, self.peak_at)
            if due > now:
                await asyncio.to_thread(wait_until, max(due, now + .002))
            if self.profile:
                self.profile.elapsed('rtp_pacer_wait', queued)
                self.profile.observe('rtp_budget_debt', max(0, self.next_at - time.perf_counter() + self.credit) * 1000)
                self.profile.count('rtx_packets' if retransmit else 'video_packets')
                self.profile.count('rtp_bytes', len(data))
            sent = time.perf_counter()
            result = await original(data)
            if self.profile:
                self.profile.elapsed('rtp_send_call', sent)
                if len(data) >= 12 and not retransmit:
                    stamp, ssrc = struct.unpack_from('!II', data, 4)
                    frame = self.frames.get(ssrc)
                    if frame is None or frame[0] != stamp:
                        frame = self.frames[ssrc] = [stamp, queued, 0]
                    frame[2] += len(data)
                    if data[1] & 128:
                        duration = (time.perf_counter() - frame[1]) * 1000
                        self.profile.observe('rtp_frame_send', duration)
                        if duration > 25:
                            self.profile.event('slow_frame_send', milliseconds=round(duration, 3), bytes=frame[2])
            return result


def install_rtp_pacer(pc, bitrate_mbps: int, profile=None, *, fps=60, budget=None) -> int:
    """Pace average media rate while permitting bounded short-term bursts.

    aiortc sends all RTP packets belonging to one encoded frame back to back.
    That is harmless on a wired LAN but can overflow a Wi-Fi or Tailscale
    queue even when the average bitrate is below the link capacity.  Wrap the
    DTLS transport's async RTP send path with a small token schedule so the
    configured bitrate remains the priority while packets leave evenly.
    """
    if type(bitrate_mbps) is not int or bitrate_mbps <= 0:
        return 0
    transports = set()
    audio_ssrcs, rtx_ssrcs = set(), set()
    for transceiver in pc.getTransceivers():
        sender = getattr(transceiver, 'sender', None)
        transport = getattr(sender, 'transport', None)
        if getattr(sender, 'kind', None) == 'audio':
            audio_ssrcs.add(sender._ssrc)
        elif hasattr(sender, '_rtx_ssrc'):
            rtx_ssrcs.add(sender._rtx_ssrc)
        if transport is not None:
            transports.add(transport)
    installed = 0
    for transport in transports:
        if getattr(transport, '_elink_rtp_pacer', None) is not None:
            state = transport._elink_rtp_pacer
            state.rate = bitrate_mbps * 1_000_000 * 1.08
            state.credit = max(.008, min(2 / fps, .032))
            continue
        original = getattr(transport, '_send_rtp', None)
        if original is None:
            continue
        state = RtpPacer(bitrate_mbps, fps, profile, budget)

        async def paced(data, *, _original=original, _state=state):
            # RTCP shares this aiortc hook; delaying it also delays loss/RTT
            # feedback. Audio is low-volume and must not wait on video debt.
            from aiortc.rtp import is_rtcp
            ssrc = struct.unpack_from('!I', data, 8)[0] if len(data) >= 12 else None
            if is_rtcp(data) or ssrc in audio_ssrcs:
                if profile:
                    profile.count('priority_packets')
                return await _original(data)
            return await _state.send(data, _original, retransmit=ssrc in rtx_ssrcs)

        transport._send_rtp = paced
        transport._elink_rtp_pacer = state
        installed += 1
    return installed
