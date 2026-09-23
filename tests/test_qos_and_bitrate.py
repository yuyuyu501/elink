import socket
from types import SimpleNamespace


def test_rate_control_options_include_cbr_budget():
    from elink.core.codecs import CodecPolicy, rate_control_options

    assert rate_control_options(CodecPolicy(bitrate=20_000_000)) == {
        "maxrate": "20000000", "bufsize": "10000000"
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
