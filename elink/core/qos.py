"""Best-effort Windows QoS marking for the WebRTC ICE sockets."""
from __future__ import annotations

import socket


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
