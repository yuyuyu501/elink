import os
import sys
from fractions import Fraction

import av

_ENCODER_CANDIDATES = [
    ('h264_nvenc', {'preset': 'p1', 'tune': 'll', 'rc': 'cbr', 'zerolatency': '1'}),
]
if sys.platform == 'win32':
    _ENCODER_CANDIDATES.append(
        ('h264_amf', {'usage': 'lowlatency', 'quality': 'speed', 'rc': 'cbr'}),
    )
_ENCODER_CANDIDATES.extend([
    ('h264_qsv',   {'preset': 'veryfast', 'rc': 'cbr'}),
    ('libx264',    {'preset': 'ultrafast', 'tune': 'zerolatency',
                    'crf': '23', 'threads': str(min(os.cpu_count() or 4, 8))}),
])

_DECODER_CANDIDATES = ['h264_cuvid', 'h264_dxva2', 'h264_qsv', 'h264']


def get_encoder_name():
    for name, _ in _ENCODER_CANDIDATES:
        try:
            if av.Codec(name, 'w') is not None:
                return name
        except Exception:
            continue
    return 'libx264'


def get_decoder_name():
    for name in _DECODER_CANDIDATES:
        try:
            if av.Codec(name, 'r') is not None:
                return name
        except Exception:
            continue
    return 'h264'


def create_encoder(width, height, bitrate=8_000_000, fps=60):
    name = get_encoder_name()
    ctx = av.CodecContext.create(name, 'w')
    ctx.width = width
    ctx.height = height
    ctx.pix_fmt = 'yuv420p'
    ctx.framerate = fps
    ctx.time_base = Fraction(1, fps)

    opts = {}
    for cname, copts in _ENCODER_CANDIDATES:
        if cname == name:
            opts = dict(copts)
            break
    opts['bitrate'] = str(bitrate)
    if 'nvenc' in name:
        opts['maxrate'] = str(bitrate)
        opts['bufsize'] = str(bitrate // 2)
    ctx.options = opts
    ctx.open()
    return ctx


def create_decoder():
    name = get_decoder_name()
    ctx = av.CodecContext.create(name, 'r')
    if 'cuvid' in name:
        ctx.options = {'gpu': '0'}
    ctx.open()
    return ctx
