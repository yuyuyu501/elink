from types import SimpleNamespace as S

from elink.core.statistics import ReceiveSampler, StreamStats, overlay_lines, selected_route


def report(byte_count, received, lost, ssrc=1):
    return {'t': S(id='transport', type='transport', bytesReceived=byte_count),
            'v': S(id='video', type='inbound-rtp', kind='video', ssrc=ssrc,
                   packetsReceived=received, packetsLost=lost, jitter=450)}


def test_interval_rate_loss_jitter_late_packets_and_stream_reset():
    sampler = ReceiveSampler()
    assert sampler.sample(report(1000, 10, 1), 10) == (None, None, 5)
    rate, loss, jitter = sampler.sample(report(126000, 100, 11), 11)
    assert rate == 1000 and loss == 10 and jitter == 5
    assert sampler.sample(report(126000, 100, 11), 12) == (0, None, 5)
    assert sampler.sample(report(127000, 102, 9), 13)[1] == 0
    assert sampler.sample(report(128000, 5, 0, ssrc=2), 14)[1] is None


def test_route_does_not_mislabel_tailnet_as_direct():
    def pc(host, kind='host'):
        pair = S(local_candidate=S(host='192.168.1.2', type='host'), remote_candidate=S(host=host, type=kind))
        return S(sctp=S(transport=S(transport=S(_connection=S(_nominated={1: pair})))))
    assert selected_route(pc('192.168.1.3')) == ('UDP · P2P', None)
    assert selected_route(pc('100.98.1.3')) == ('Tailscale · 路径待测', '100.98.1.3')
    assert selected_route(pc('1.2.3.4', 'relay')) == ('UDP · ICE 中继', None)


def test_overlay_units_unknown_values_stale_and_disconnect():
    snapshot = StreamStats(976.3, 0.08, 1.2, 3.5, 'D3D11VA', 'UDP · P2P', 30, 20)
    lines = overlay_lines(snapshot, 60, 19614, 13)
    assert lines[:2] == ['05:26:54', 'UDP · P2P']
    assert '976.3 Kbps RX' in lines and '13.0 ms RTT' in lines
    assert '16.7 ms / frame' in lines and '0.08 % loss' in lines and '30 Mbps 目标' in lines
    assert '-- ms / frame' in overlay_lines(snapshot, 0, 0, None)
    assert '-- Kbps RX' in overlay_lines(snapshot, 60, 0, 0, fresh=False)
    assert '已断开' in overlay_lines(snapshot, 60, 0, 13, connected=False)
