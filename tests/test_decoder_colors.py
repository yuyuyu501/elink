from types import SimpleNamespace

import av
import numpy as np
import pytest

from elink.core.codecs import CodecPolicy, DesktopDecoder
from elink.core.media import bgr_pixels


def decoder_with_downloaded_frame(monkeypatch, frame, *, space, color_range):
    monkeypatch.setattr('elink.core.codecs.ctypes.util.find_library', lambda _: 'mock-hardware-runtime')
    context = SimpleNamespace(colorspace=space, color_range=color_range,
                              flags=0, decode=lambda packet: [frame])
    monkeypatch.setattr('elink.core.codecs.av.CodecContext',
                        SimpleNamespace(create=lambda *args, **kwargs: context))
    return DesktopDecoder(CodecPolicy(decoder='hardware')), context


@pytest.mark.parametrize('space', [1, 5])
@pytest.mark.parametrize('color_range', [1, 2])
def test_downloaded_frame_keeps_highlights_shadows_and_matrix(monkeypatch, space, color_range):
    frame = av.VideoFrame(8, 8, 'nv12')
    frame.planes[0].update(bytes([245 if color_range == 2 else 226]) * frame.planes[0].buffer_size)
    frame.planes[1].update(bytes([128]) * frame.planes[1].buffer_size)
    assert int(frame.colorspace) == 2 and int(frame.color_range) == 0
    decoder, _ = decoder_with_downloaded_frame(monkeypatch, frame, space=space, color_range=color_range)
    decoded = decoder.decode(SimpleNamespace(data=b'packet', timestamp=0))[0]
    assert (int(decoded.colorspace), int(decoded.color_range)) == (space, color_range)
    assert np.max(np.abs(bgr_pixels(decoded)[4, 4].astype(int) - 245)) <= 2


def test_frame_metadata_wins_and_unknown_stream_range_is_not_guessed(monkeypatch):
    frame = av.VideoFrame(8, 8, 'nv12')
    frame.colorspace, frame.color_range = 5, 1
    decoder, context = decoder_with_downloaded_frame(monkeypatch, frame, space=1, color_range=2)
    packet = SimpleNamespace(data=b'packet', timestamp=0)
    decoder.decode(packet)
    assert (int(frame.colorspace), int(frame.color_range)) == (5, 1)
    frame.colorspace, frame.color_range = 2, 0
    context.colorspace, context.color_range = 2, 0
    decoder.decode(packet)
    assert (int(frame.colorspace), int(frame.color_range)) == (2, 0)


def test_stream_metadata_updates_after_a_new_sequence_header(monkeypatch):
    frame = av.VideoFrame(8, 8, 'nv12')
    decoder, context = decoder_with_downloaded_frame(monkeypatch, frame, space=5, color_range=1)
    packet = SimpleNamespace(data=b'packet', timestamp=0)
    decoder.decode(packet)
    assert (int(frame.colorspace), int(frame.color_range)) == (5, 1)
    frame.colorspace, frame.color_range = 2, 0
    context.colorspace, context.color_range = 1, 2
    decoder.decode(packet)
    assert (int(frame.colorspace), int(frame.color_range)) == (1, 2)
