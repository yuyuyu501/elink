import socket
import asyncio
from types import SimpleNamespace


def test_rate_control_options_include_cbr_budget():
    from elink.core.codecs import CodecPolicy, rate_control_options

    assert rate_control_options(CodecPolicy(bitrate=20_000_000)) == {
        "maxrate": "20000000", "bufsize": "666666"
    }


def test_fixed_priority_encoder_ignores_low_remb_feedback():
    from elink.core.codecs import CodecPolicy, DesktopEncoder

    encoder = DesktopEncoder(CodecPolicy(bitrate=30_000_000))
    encoder.target_bitrate = 400_000
    assert encoder.target_bitrate == 30_000_000
    adaptive = DesktopEncoder(CodecPolicy(bitrate=30_000_000, fixed_priority=False))
    adaptive.target_bitrate = 400_000
    assert adaptive.target_bitrate == 400_000


def test_mark_rtc_sockets_sets_ipv4_dscp():
    from elink.core.qos import mark_rtc_sockets

    class FakeSocket:
        family = socket.AF_INET

        def __init__(self):
            self.values = []

        def setsockopt(self, level, option, value):
            self.values.append((level, option, value))

    sock = FakeSocket()
    protocol = SimpleNamespace(transport=SimpleNamespace(get_extra_info=lambda name: sock))
    connection = SimpleNamespace(_protocols=[protocol])
    pc = SimpleNamespace(sctp=SimpleNamespace(transport=SimpleNamespace(transport=SimpleNamespace(_connection=connection))))

    assert mark_rtc_sockets(pc) == 1
    assert sock.values == [(socket.IPPROTO_IP, socket.IP_TOS, 34 << 2)]


def test_rtp_pacer_wraps_each_sender_transport_once():
    from elink.core.qos import install_rtp_pacer

    sent = []

    class Transport:
        async def _send_rtp(self, data):
            sent.append(data)

    transport = Transport()
    pc = SimpleNamespace(getTransceivers=lambda: [SimpleNamespace(sender=SimpleNamespace(transport=transport))])
    assert install_rtp_pacer(pc, 20) == 1
    assert install_rtp_pacer(pc, 20) == 0
    asyncio.run(transport._send_rtp(b'packet'))
    assert sent == [b'packet']


def test_pacer_enforces_rate_with_bounded_bursts_and_recovers_after_idle(monkeypatch):
    from elink.core import qos
    now, sent, waits = [10.0], [], []
    monkeypatch.setattr(qos, 'time', SimpleNamespace(perf_counter=lambda: now[0]))
    def wait(deadline):
        waits.append(deadline - now[0])
        now[0] = deadline
    monkeypatch.setattr(qos, 'wait_until', wait)
    class Transport:
        async def _send_rtp(self, data):
            sent.append((now[0], len(data)))
    transport = Transport()
    pc = SimpleNamespace(getTransceivers=lambda: [SimpleNamespace(sender=SimpleNamespace(transport=transport))])
    qos.install_rtp_pacer(pc, 20)
    async def run():
        for _ in range(100):
            await transport._send_rtp(b'x' * 1200)
        # Debt cannot build up while the source is idle.
        now[0] += 1
        start = now[0]
        await transport._send_rtp(b'x' * 1200)
        assert now[0] == start
    asyncio.run(run())
    packet_seconds = 1200 * 8 / (20_000_000 * 1.08)
    assert waits and max(waits) <= .002 + 1e-9
    for i, (stamp, size) in enumerate(sent[:100]):
        assert stamp - 10 >= (i + 1) * packet_seconds - .004 - 1e-9
        assert size == 1200


def test_vbv_budget_tracks_frame_rate_and_has_encoder_floor():
    from elink.core.codecs import CodecPolicy, rate_control_options
    assert rate_control_options(CodecPolicy(fps=90, bitrate=20_000_000))['bufsize'] == '444444'
    assert rate_control_options(CodecPolicy(fps=240, bitrate=5_000_000))['bufsize'] == '300000'


def test_pacer_delayed_wakes_do_not_permanently_reduce_available_rate(monkeypatch):
    from elink.core import qos
    now = [10.0]
    monkeypatch.setattr(qos, 'time', SimpleNamespace(perf_counter=lambda: now[0]))
    def wait(deadline):
        now[0] = deadline + .001  # Worker scheduling occasionally wakes late.
    monkeypatch.setattr(qos, 'wait_until', wait)
    class Transport:
        async def _send_rtp(self, data):
            pass
    transport = Transport()
    pc = SimpleNamespace(getTransceivers=lambda: [SimpleNamespace(sender=SimpleNamespace(transport=transport))])
    qos.install_rtp_pacer(pc, 20)
    async def run():
        for _ in range(1000):
            await transport._send_rtp(b'x' * 1200)
    asyncio.run(run())
    budget = 1000 * 1200 * 8 / (20_000_000 * 1.08)
    # Keep the bounded initial credit; do not charge every late wake twice.
    assert budget - .004 - 1e-9 <= now[0] - 10 <= budget + .004
