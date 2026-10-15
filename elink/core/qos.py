"""Best-effort Windows QoS marking for the WebRTC ICE sockets."""
from __future__ import annotations

import asyncio
import socket
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


def install_rtp_pacer(pc, bitrate_mbps: int, profile=None) -> int:
    """Smooth RTP packet bursts at the configured media rate.

    aiortc sends all RTP packets belonging to one encoded frame back to back.
    That is harmless on a wired LAN but can overflow a Wi-Fi or Tailscale
    queue even when the average bitrate is below the link capacity.  Wrap the
    DTLS transport's async RTP send path with a small token schedule so the
    configured bitrate remains the priority while packets leave evenly.
    """
    if type(bitrate_mbps) is not int or bitrate_mbps <= 0:
        return 0
    rate = bitrate_mbps * 1_000_000 * 1.08
    transports = set()
    for transceiver in pc.getTransceivers():
        transport = getattr(getattr(transceiver, 'sender', None), 'transport', None)
        if transport is not None:
            transports.add(transport)
    installed = 0
    for transport in transports:
        if getattr(transport, '_elink_rtp_pacer', None) is not None:
            continue
        original = getattr(transport, '_send_rtp', None)
        if original is None:
            continue
        state = {'next_at': 0.0, 'lock': asyncio.Lock()}

        async def paced(data, *, _original=original, _state=state):
            queued = time.perf_counter()
            async with _state['lock']:
                # A bounded 4 ms packet burst amortizes Python/crypto overhead.
                # Beyond that budget wait to a QPC deadline in a worker, not a
                # Windows event-loop timer. No unbounded background send queue.
                now = time.perf_counter()
                charged_at = max(_state['next_at'], now - 0.004) + len(data) * 8 / rate
                if charged_at > now:
                    await asyncio.to_thread(wait_until, max(charged_at, now + 0.002))
                # Retain up to 4 ms of refill credit after an oversleep. Resetting
                # to the wakeup time loses capacity on every delayed wake and
                # makes the configured bitrate unattainable near saturation.
                _state['next_at'] = charged_at
                if profile:
                    profile.elapsed('rtp_pacer_wait', queued)
                sent = time.perf_counter()
                result = await _original(data)
                if profile:
                    profile.elapsed('rtp_send_call', sent)
                return result

        transport._send_rtp = paced
        transport._elink_rtp_pacer = state
        installed += 1
    return installed
