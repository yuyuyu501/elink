from types import SimpleNamespace

import av
import pytest

from elink.ui import rtx_video


def library(calls, *, create=True):
    return SimpleNamespace(
        elink_video_create=lambda *args: calls.append(('create', args)) or (123 if create else None),
        elink_video_error=lambda: b'RTX driver inactive',
        elink_video_effect=lambda _: .5,
        elink_video_render=lambda *args: calls.append(('render', args)) or 1,
        elink_video_gpu_ms=lambda _: .8,
        elink_video_destroy=lambda handle: calls.append(('destroy', handle)))


@pytest.mark.parametrize('format', ['nv12', 'yuv420p', 'yuvj420p'])
def test_padded_yuv_layout_and_lifetime(monkeypatch, format):
    calls=[]
    monkeypatch.setattr(rtx_video, 'native_library', lambda: library(calls))
    processor=rtx_video.RtxProcessor(642,362,(1920,1080))
    frame=av.VideoFrame(642,362,format)
    frame.colorspace,frame.color_range=1,2
    assert processor.render(frame)==.8
    args=calls[-1][1]
    assert args[0]==123 and args[1:3]==(frame.planes[0].buffer_ptr,frame.planes[0].line_size)
    assert args[3:5]==(frame.planes[1].buffer_ptr,frame.planes[1].line_size)
    assert args[-3:]==(format=='nv12',1,True)
    assert args[5]==(None if format=='nv12' else frame.planes[2].buffer_ptr)
    with pytest.raises(ValueError,match='dimensions'):
        processor.render(av.VideoFrame(640,360,'nv12'))
    processor.close()
    processor.close()
    assert calls.count(('destroy',123))==1
    with pytest.raises(ValueError,match='closed'):
        processor.render(frame)


def test_inactive_driver_is_an_error_not_an_active_rtx_label(monkeypatch):
    monkeypatch.setattr(rtx_video, 'native_library', lambda: library([],create=False))
    with pytest.raises(RuntimeError,match='inactive'):
        rtx_video.RtxProcessor(960,540,(1920,1080))
