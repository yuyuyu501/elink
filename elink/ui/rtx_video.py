"""Driver-backed RTX VSR; decoded YUV is uploaded without a CPU RGB roundtrip."""
import ctypes as C
import logging
import os
import sys
import threading
import time
import weakref
from pathlib import Path
from shiboken6 import delete

from PySide6.QtOpenGL import QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture

from ..paths import bundle_dir
from .gpu_video import color_parameters


log = logging.getLogger(__name__)
_processor_lock = threading.RLock()
_processors = weakref.WeakSet()


def rtx_available():
    """Verify generated pixels, without probing beside an active VSR processor."""
    with _processor_lock:
        if any(p.enabled and p.effect_difference >= .01 for p in _processors):
            return True
        processor = None
        try:
            processor = RtxProcessor(960, 540, (1920, 1080))
            return processor.effect_difference >= .01
        except Exception as exc:
            log.info('RTX VSR unavailable during capability detection: %s', exc)
            return False
        finally:
            if processor:
                processor.close()


def native_library():
    if os.name != 'nt':
        raise RuntimeError('RTX VSR requires Windows and an NVIDIA RTX GPU')
    path = (bundle_dir() / 'elink_video.dll' if getattr(sys, 'frozen', False) else
            Path(__file__).resolve().parents[2] / '.artifacts' / 'windows' / 'video-build' / 'elink_video.dll')
    library = C.CDLL(str(path))
    library.elink_video_error.restype = C.c_char_p
    library.elink_video_error.argtypes = []
    library.elink_video_create.argtypes = [C.c_uint] * 5 + [C.c_int]
    library.elink_video_create.restype = C.c_void_p
    library.elink_video_destroy.argtypes = [C.c_void_p]
    library.elink_video_destroy.restype = None
    library.elink_video_render.argtypes = [C.c_void_p, C.c_void_p, C.c_uint, C.c_void_p, C.c_uint,
                                          C.c_void_p, C.c_uint, C.c_int, C.c_int, C.c_int]
    library.elink_video_render.restype = C.c_int
    library.elink_video_gpu_ms.argtypes = [C.c_void_p]
    library.elink_video_gpu_ms.restype = C.c_double
    library.elink_video_effect.argtypes = [C.c_void_p]
    library.elink_video_effect.restype = C.c_double
    library.elink_video_download.argtypes = [C.c_void_p, C.c_void_p, C.c_uint]
    library.elink_video_download.restype = C.c_int
    return library


class RtxProcessor:
    """One processor per input/output size and GL context. No DLL search via PATH."""

    def __init__(self, width, height, output_size, texture=0, *, enabled=True):
        self.input_size = width, height
        self.enabled = enabled
        with _processor_lock:
            self.library = native_library()
            self.handle = self.library.elink_video_create(width, height, *output_size, texture, enabled)
            if not self.handle:
                raise RuntimeError(self.error())
            self.effect_difference = self.library.elink_video_effect(self.handle)
            _processors.add(self)

    def error(self):
        return self.library.elink_video_error().decode('utf-8', errors='replace')

    def render(self, frame):
        if not self.handle or (frame.width, frame.height) != self.input_size:
            raise ValueError('RTX input dimensions changed or processor is closed')
        color_parameters(frame)
        if frame.format.name not in ('yuv420p', 'nv12', 'yuvj420p'):
            raise ValueError('Unsupported RTX video pixel format')
        y, u = frame.planes[:2]
        v = frame.planes[2] if len(frame.planes) == 3 else None
        full = int(frame.color_range) == 2 or frame.format.name == 'yuvj420p'
        if not self.library.elink_video_render(self.handle, y.buffer_ptr, y.line_size,
                u.buffer_ptr, u.line_size, v.buffer_ptr if v else None, v.line_size if v else 0,
                frame.format.name == 'nv12', int(frame.colorspace), full):
            raise RuntimeError(self.error())
        return self.library.elink_video_gpu_ms(self.handle)

    def download(self, pixels):
        if not self.library.elink_video_download(self.handle, pixels.ctypes.data, pixels.strides[0]):
            raise RuntimeError(self.error())

    def close(self):
        with _processor_lock:
            if self.handle:
                handle, self.handle = self.handle, None
                _processors.discard(self)
                self.library.elink_video_destroy(handle)


class RtxUpscaler:
    def __init__(self, surface, output_size):
        self.surface, self.output_size = surface, tuple(output_size)
        self.processor = self.texture = self.program = None
        self.key = None
        self.program = QOpenGLShaderProgram(surface)
        from .gpu_video import VERTEX
        fragment = '''#version 120
uniform sampler2D sourceTex;
varying vec2 uv;
void main() {gl_FragColor=texture2D(sourceTex,uv);}
'''
        for kind, source in ((QOpenGLShader.ShaderTypeBit.Vertex, VERTEX),
                             (QOpenGLShader.ShaderTypeBit.Fragment, fragment)):
            if not self.program.addShaderFromSourceCode(kind, source):
                raise RuntimeError(self.program.log())
        self.program.bindAttributeLocation('position', 0)
        self.program.bindAttributeLocation('texcoord', 1)
        if not self.program.link():
            raise RuntimeError(self.program.log())

    def render(self, viewport, profile, new_frame):
        frame = self.surface.frame
        key = frame.width, frame.height
        if key != self.key:
            self._release_texture()
            self.texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            if not self.texture.create():
                raise RuntimeError('Cannot create RTX shared texture')
            self.processor = RtxProcessor(*key, self.output_size, self.texture.textureId())
            self.key = key
            new_frame = True
        if new_frame:
            started = time.perf_counter()
            gpu_ms = self.processor.render(frame)
            if profile:
                profile.elapsed('rtx_submit', started)
                if gpu_ms >= 0:
                    profile.observe('rtx_gpu', gpu_ms)
        gl = self.surface.context().functions()
        gl.glBindFramebuffer(0x8D40, self.surface.defaultFramebufferObject())
        gl.glViewport(*viewport)
        if not self.program.bind():
            raise RuntimeError('Cannot bind RTX display shader')
        self.texture.bind(0)
        gl.glTexParameteri(0x0DE1, 0x2801, 0x2601)
        gl.glTexParameteri(0x0DE1, 0x2800, 0x2601)
        gl.glTexParameteri(0x0DE1, 0x2802, 0x812F)
        gl.glTexParameteri(0x0DE1, 0x2803, 0x812F)
        gl.glUniform1i(self.program.uniformLocation('sourceTex'), 0)
        self.surface.draw_quad(self.program)
        self.program.release()
        if profile:
            profile.describe(upscaler='RTX-VSR', upscale_width=self.output_size[0], upscale_height=self.output_size[1],
                             rtx_probe_mae=self.processor.effect_difference)

    def _release_texture(self):
        if self.processor:
            self.processor.close()
            self.processor = None
        if self.texture:
            self.texture.destroy()
            self.texture = None
        self.key = None

    def cleanup(self):
        self._release_texture()
        if self.program:
            self.program.removeAllShaders()
            delete(self.program)
            self.program = None
