"""Session statistics with explicit units and interval-based receive counters."""
from dataclasses import dataclass
import ipaddress


@dataclass(frozen=True)
class StreamStats:
    rx_kbps: float | None = None
    loss_percent: float | None = None
    jitter_ms: float | None = None
    decode_ms: float | None = None
    decoder: str = "--"
    route: str = "连接中"
    target_mbps: int = 0
    sampled_at: float = 0.0


def is_tailnet(address):
    try:
        ip = ipaddress.ip_address(address)
        return ip in ipaddress.ip_network('100.64.0.0/10') if ip.version == 4 else ip in ipaddress.ip_network('fd7a:115c:a1e0::/48')
    except ValueError:
        return False


def selected_route(pc):
    """aiortc 1.14 / aioice 0.10 selected pair; an overlay is not proof of P2P."""
    try:
        pairs = list(pc.sctp.transport.transport._connection._nominated.values())
        if not pairs:
            return "连接中", None
        pair = pairs[0]
        local, remote = pair.local_candidate, pair.remote_candidate
        if local.type == 'relay' or remote.type == 'relay':
            return "UDP · ICE 中继", None
        if local.host == remote.host:
            return "UDP · 本机回环", None
        if is_tailnet(remote.host) or is_tailnet(local.host):
            return "Tailscale · 路径待测", remote.host
        return "UDP · P2P", None
    except (AttributeError, IndexError):
        return "路径未知", None


class ReceiveSampler:
    def __init__(self):
        self.previous_time = None
        self.transports = {}
        self.streams = {}

    def sample(self, report, now):
        transports, streams, jitters = {}, {}, []
        for item in report.values():
            if item.type == 'transport':
                transports[item.id] = item.bytesReceived
            elif item.type == 'inbound-rtp' and item.kind == 'video':
                streams[(item.id, item.ssrc)] = (item.packetsReceived, item.packetsLost)
                jitters.append(item.jitter / 90.0)  # H.264 RTP clock is 90 kHz.
        rx = loss = None
        if self.previous_time is not None and now > self.previous_time:
            # A transport shared by audio/video is counted once. This is the
            # encrypted transport receive rate, not the encoder target bitrate.
            byte_delta = sum(max(0, count - self.transports[key]) for key, count in transports.items()
                             if key in self.transports)
            if transports and transports.keys() == self.transports.keys():
                rx = byte_delta * 8 / (now - self.previous_time) / 1000
            received = lost = 0
            for key, (count, missing) in streams.items():
                if key in self.streams and count >= self.streams[key][0]:
                    received += count - self.streams[key][0]
                    # Late packets may reduce cumulative loss; never show negative loss.
                    lost += max(0, missing - self.streams[key][1])
            if received + lost:
                loss = 100 * lost / (received + lost)
        self.previous_time, self.transports, self.streams = now, transports, streams
        return rx, loss, max(jitters) if jitters else None


def overlay_lines(snapshot, fps, rtt_ms, connected=True, fresh=True):
    lines = [snapshot.route if connected else '已断开',
             f'{fps:.0f} fps' if connected else '-- fps']
    def number(value, suffix, digits=1):
        return f'{value:.{digits}f} {suffix}' if connected and fresh and value is not None else f'-- {suffix}'
    rx_mbps = snapshot.rx_kbps / 1000 if snapshot.rx_kbps is not None else None
    lines += [number(rx_mbps, 'Mbps'), number(rtt_ms, 'ms RTT'),
              number(snapshot.decode_ms, 'ms decode'),
              snapshot.decoder if connected and fresh and snapshot.decoder else '--',
              number(snapshot.loss_percent, '% loss', 2)]
    return lines
