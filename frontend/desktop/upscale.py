"""FSR 1 EASU/RCAS 超分辨率（离屏 OpenGL，CPU 回读）。

E:\\elink 在 QOpenGLWidget 上直接显示 GPU 纹理；本项目的预览是 CPU QImage，
所以这里用离屏 QOpenGLContext 跑同样的 FSR 1 shader，再把结果读回 BGRA。
这样不用重写预览控件，也不会把 GPU 纹理和 UI 生命周期绑死。

限制：
- 需要 OpenGL 4.2（Windows + 独显/核显一般满足）；不满足时服务端回退到原帧。
- 这是 FSR 1，不是 NVIDIA RTX VSR；RTX VSR 需要 E:\\elink 的 D3D11 原生管线。
"""

from __future__ import annotations

import ctypes
import logging
import struct
import sys
from functools import lru_cache

import numpy as np

from PySide6.QtGui import QImage, QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLFramebufferObject,
    QOpenGLFramebufferObjectFormat,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVertexArrayObject,
)
from shiboken6 import VoidPtr

from backend.media.frame import Frame, PixelFormat

from .paths import resource_dir

logger = logging.getLogger(__name__)

FSR_OUTPUTS = {
    "1080p": (1920, 1080),
    "1440p": (2560, 1440),
    "4K": (3840, 2160),
}

VERTEX = """#version 420 compatibility
in vec2 position;
in vec2 texcoord;
out vec2 uv;
void main() { gl_Position=vec4(position,0,1); uv=texcoord; }
"""


@lru_cache(maxsize=3)
def _shader_source(stage: str) -> str:
    root = resource_dir() / "fsr"
    common = "#version 420 compatibility\n#define A_GPU 1\n#define A_GLSL 1\n"
    common += (root / "ffx_a.h").read_text(encoding="utf-8")
    common += (
        "\nuniform sampler2D sourceTex;\n"
        "uniform vec2 sourceSize;\nuniform vec2 targetSize;\n"
        "in vec2 uv;\nout vec4 fragColor;\n"
    )
    if stage == "easu":
        common += "#define FSR_EASU_F 1\n"
        common += (root / "ffx_fsr1.h").read_text(encoding="utf-8")
        common += """
vec4 FsrEasuRF(vec2 p) { return textureGather(sourceTex, p, 0); }
vec4 FsrEasuGF(vec2 p) { return textureGather(sourceTex, p, 1); }
vec4 FsrEasuBF(vec2 p) { return textureGather(sourceTex, p, 2); }
void main() {
    uvec4 c0, c1, c2, c3;
    FsrEasuCon(c0, c1, c2, c3, sourceSize.x, sourceSize.y,
               sourceSize.x, sourceSize.y, targetSize.x, targetSize.y);
    vec3 color;
    FsrEasuF(color, uvec2(gl_FragCoord.xy), c0, c1, c2, c3);
    fragColor = vec4(color, 1.0);
}
"""
    elif stage == "rcas":
        common += "#define FSR_RCAS_F 1\n#define FSR_RCAS_DENOISE 1\n"
        common += (root / "ffx_fsr1.h").read_text(encoding="utf-8")
        common += """
vec4 FsrRcasLoadF(ivec2 p) {
    return texelFetch(sourceTex, clamp(p, ivec2(0), ivec2(targetSize)-1), 0);
}
void FsrRcasInputF(inout float r, inout float g, inout float b) {}
void main() {
    uvec4 con;
    FsrRcasCon(con, 1.0);
    vec3 color;
    uvec2 p = uvec2(gl_FragCoord.xy);
    FsrRcasF(color.r, color.g, color.b, p, con);
    fragColor = vec4(clamp(color, 0.0, 1.0), 1.0);
}
"""
    else:
        raise ValueError(f"unknown FSR pass: {stage}")
    return common


class RtxUpscaler:
    """NVIDIA RTX VSR via the native D3D11 video processor (headless readback).

    输入是 BGRA CPU 帧，先转 NV12，再走 NVIDIA VideoProcessor 扩展；结果读回
    BGRA。只支持 Windows + NVIDIA RTX（驱动 551+）。E:\\elink 的原生实现被
    复用为了 ``elink_video.dll``。
    """

    _probe: bool | None = None

    def __init__(self, output_size: tuple[int, int] = (1920, 1080)) -> None:
        if not sys.platform.startswith("win"):
            raise RuntimeError("RTX VSR 仅支持 Windows")
        self.output_size = (int(output_size[0]), int(output_size[1]))
        self._library = self._load()
        self._handle = None
        self._input_size = (0, 0)
        self._buffer: np.ndarray | None = None
        self._nv12: np.ndarray | None = None

    @staticmethod
    def _load():
        path = resource_dir() / "elink_video.dll"
        if not path.is_file():
            raise RuntimeError("缺少 elink_video.dll")
        library = ctypes.CDLL(str(path))
        library.elink_video_error.restype = ctypes.c_char_p
        library.elink_video_error.argtypes = []
        library.elink_video_create.argtypes = [ctypes.c_uint] * 5 + [ctypes.c_int]
        library.elink_video_create.restype = ctypes.c_void_p
        library.elink_video_destroy.argtypes = [ctypes.c_void_p]
        library.elink_video_destroy.restype = None
        library.elink_video_render.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
        ]
        library.elink_video_render.restype = ctypes.c_int
        library.elink_video_effect.argtypes = [ctypes.c_void_p]
        library.elink_video_effect.restype = ctypes.c_double
        library.elink_video_download.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
        library.elink_video_download.restype = ctypes.c_int
        return library

    @classmethod
    def available(cls) -> bool:
        if cls._probe is not None:
            return cls._probe
        processor = None
        try:
            processor = cls((1280, 720))
            probe = np.full((360, 640, 4), 128, dtype=np.uint8)
            frame = Frame(data=probe.tobytes(), width=640, height=360, pixel_format=PixelFormat.BGRA)
            processor.upscale(frame)
            cls._probe = processor.effect >= 0.01
        except Exception as exc:
            logger.info("RTX VSR 不可用: %s", exc)
            cls._probe = False
        finally:
            if processor is not None:
                processor.close()
        return cls._probe

    @property
    def effect(self) -> float:
        if self._handle is None:
            return 0.0
        return float(self._library.elink_video_effect(self._handle))

    def _error(self) -> str:
        value = self._library.elink_video_error()
        return (value or b"").decode("utf-8", errors="replace")

    def _ensure(self, width: int, height: int, output: tuple[int, int]) -> None:
        if self._handle is not None and self._input_size == (width, height):
            return
        self.close()
        self._input_size = (width, height)
        handle = self._library.elink_video_create(width, height, output[0], output[1], 0, 1)
        if not handle:
            raise RuntimeError(self._error() or "RTX 视频处理器创建失败")
        self._handle = handle
        self._buffer = np.empty(output[0] * output[1] * 4, dtype=np.uint8)
        self._nv12 = np.empty(width * height * 3 // 2, dtype=np.uint8)

    def _to_nv12(self, frame: Frame) -> np.ndarray:
        width, height = frame.width, frame.height
        pixels = np.frombuffer(frame.data, dtype=np.uint8, count=width * height * 4).reshape(
            height, width, 4
        )
        b = pixels[..., 0].astype(np.float32)
        g = pixels[..., 1].astype(np.float32)
        r = pixels[..., 2].astype(np.float32)
        y = (0.2126 * r + 0.7152 * g + 0.0722 * b).clip(0, 255).astype(np.uint8)
        u = (-0.1146 * r - 0.3854 * g + 0.5 * b + 128.0).clip(0, 255).astype(np.uint8)
        v = (0.5 * r - 0.4542 * g - 0.0458 * b + 128.0).clip(0, 255).astype(np.uint8)
        u2 = u.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3)).astype(np.uint8)
        v2 = v.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3)).astype(np.uint8)
        chroma = np.empty((height // 2, width // 2, 2), dtype=np.uint8)
        chroma[..., 0] = u2
        chroma[..., 1] = v2
        return np.ascontiguousarray(np.concatenate((y.reshape(-1), chroma.reshape(-1))))

    def upscale(self, frame: Frame, output_size: tuple[int, int] | None = None) -> Frame:
        target_w, target_h = output_size or self.output_size
        if frame.width >= target_w and frame.height >= target_h:
            return frame
        if frame.width % 2 or frame.height % 2:
            raise ValueError("RTX VSR 需要偶数宽高")
        self._ensure(frame.width, frame.height, (target_w, target_h))
        assert self._handle is not None and self._buffer is not None and self._nv12 is not None
        nv12 = self._to_nv12(frame)
        self._nv12[:] = nv12
        y_ptr = ctypes.c_void_p(self._nv12.ctypes.data)
        chroma_ptr = ctypes.c_void_p(self._nv12.ctypes.data + frame.width * frame.height)
        if not self._library.elink_video_render(
            self._handle,
            y_ptr,
            frame.width,
            chroma_ptr,
            frame.width,
            None,
            0,
            1,
            1,
            1,
        ):
            raise RuntimeError(self._error() or "RTX VSR 渲染失败")
        if not self._library.elink_video_download(
            self._handle, ctypes.c_void_p(self._buffer.ctypes.data), target_w * 4
        ):
            raise RuntimeError(self._error() or "RTX VSR 回读失败")
        return Frame(
            data=self._buffer.tobytes(),
            width=target_w,
            height=target_h,
            pixel_format=PixelFormat.BGRA,
            timebase=frame.timebase,
            pts=frame.pts,
            stream_id=frame.stream_id,
        )

    def close(self) -> None:
        if self._handle is not None:
            self._library.elink_video_destroy(self._handle)
        self._handle = None
        self._buffer = None
        self._nv12 = None
        self._input_size = (0, 0)


class FsrUpscaler:
    """One offscreen GL context per session; all calls must happen on its thread."""

    def __init__(self, output_size: tuple[int, int] = (1920, 1080)) -> None:
        width, height = int(output_size[0]), int(output_size[1])
        if width < 2 or height < 2:
            raise ValueError("FSR output size is invalid")
        self.output_size = (width, height)
        self._context: QOpenGLContext | None = None
        self._surface: QOffscreenSurface | None = None
        self._programs: list[QOpenGLShaderProgram] = []
        self._vao: QOpenGLVertexArrayObject | None = None
        self._vbo: QOpenGLBuffer | None = None
        self._texture: QOpenGLTexture | None = None
        self._texture_size = (0, 0)
        self._fbo_easu: QOpenGLFramebufferObject | None = None
        self._fbo_rcas: QOpenGLFramebufferObject | None = None
        self._fbo_size = (0, 0)
        self._init()

    @classmethod
    def available(cls) -> bool:
        try:
            cls((640, 360)).close()
            return True
        except Exception as exc:
            logger.info("FSR 1 不可用: %s", exc)
            return False

    def _init(self) -> None:
        fmt = QSurfaceFormat()
        fmt.setVersion(4, 2)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
        fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)
        context = QOpenGLContext()
        context.setFormat(fmt)
        if not context.create():
            raise RuntimeError("无法创建 OpenGL 4.2 上下文")
        surface = QOffscreenSurface()
        surface.setFormat(context.format())
        surface.create()
        if not surface.isValid():
            context.deleteLater()
            raise RuntimeError("无法创建离屏 OpenGL 表面")
        self._context, self._surface = context, surface
        if not context.makeCurrent(surface):
            raise RuntimeError("无法激活 OpenGL 上下文")
        try:
            for stage in ("easu", "rcas"):
                program = QOpenGLShaderProgram()
                self._programs.append(program)
                for kind, source in (
                    (QOpenGLShader.ShaderTypeBit.Vertex, VERTEX),
                    (QOpenGLShader.ShaderTypeBit.Fragment, _shader_source(stage)),
                ):
                    if not program.addShaderFromSourceCode(kind, source):
                        raise RuntimeError(program.log())
                program.bindAttributeLocation("position", 0)
                program.bindAttributeLocation("texcoord", 1)
                if not program.link():
                    raise RuntimeError(program.log())
            self._vao = QOpenGLVertexArrayObject()
            if not self._vao.create():
                raise RuntimeError("无法创建 VAO")
            self._vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            if not self._vbo.create():
                raise RuntimeError("无法创建 VBO")
            vertices = struct.pack(
                "16f",
                -1, -1, 0, 1,
                1, -1, 1, 1,
                -1, 1, 0, 0,
                1, 1, 1, 0,
            )
            self._vao.bind()
            self._vbo.bind()
            self._vbo.allocate(vertices, len(vertices))
            self._vao.release()
            self._vbo.release()
        finally:
            context.doneCurrent()

    def _make_current(self) -> None:
        if self._context is None or self._surface is None:
            raise RuntimeError("FSR context is closed")
        if not self._context.makeCurrent(self._surface):
            raise RuntimeError("无法激活 FSR OpenGL 上下文")

    def _upload(self, frame: Frame) -> None:
        image = QImage(frame.data, frame.width, frame.height, frame.width * 4, QImage.Format.Format_ARGB32)
        if self._texture is None or self._texture_size != (frame.width, frame.height):
            if self._texture is not None:
                self._texture.destroy()
            texture = QOpenGLTexture(
                image, QOpenGLTexture.MipMapGeneration.DontGenerateMipMaps
            )
            texture.setMinMagFilters(QOpenGLTexture.Filter.Linear, QOpenGLTexture.Filter.Linear)
            texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
            self._texture = texture
            self._texture_size = (frame.width, frame.height)
        else:
            self._texture.setData(image)

    def _ensure_fbos(self, width: int, height: int) -> None:
        if self._fbo_easu is not None and self._fbo_size == (width, height):
            return
        self._fbo_easu = None
        self._fbo_rcas = None
        fmt = QOpenGLFramebufferObjectFormat()
        fmt.setInternalTextureFormat(0x8058)  # RGBA8
        self._fbo_easu = QOpenGLFramebufferObject(width, height, fmt)
        self._fbo_rcas = QOpenGLFramebufferObject(width, height, fmt)
        if not self._fbo_easu.isValid() or not self._fbo_rcas.isValid():
            raise RuntimeError("无法分配 FSR framebuffer")
        self._fbo_size = (width, height)

    def _draw_quad(self, program: QOpenGLShaderProgram) -> None:
        gl = self._context.functions()
        assert self._vao is not None and self._vbo is not None
        self._vao.bind()
        self._vbo.bind()
        for index, offset in ((0, 0), (1, 8)):
            program.enableAttributeArray(index)
            program.setAttributeBuffer(index, 0x1406, offset, 2, 16)
        gl.glDrawArrays(0x0005, 0, 4)
        program.disableAttributeArray(0)
        program.disableAttributeArray(1)
        self._vbo.release()
        self._vao.release()

    def _pass(
        self,
        program: QOpenGLShaderProgram,
        texture: QOpenGLTexture,
        source_size: tuple[int, int],
        target_size: tuple[int, int],
    ) -> None:
        gl = self._context.functions()
        program.bind()
        texture.bind(0)
        gl.glUniform1i(program.uniformLocation("sourceTex"), 0)
        gl.glUniform2f(program.uniformLocation("sourceSize"), float(source_size[0]), float(source_size[1]))
        gl.glUniform2f(program.uniformLocation("targetSize"), float(target_size[0]), float(target_size[1]))
        self._draw_quad(program)
        program.release()

    def upscale(self, frame: Frame, output_size: tuple[int, int] | None = None) -> Frame:
        target_w, target_h = output_size or self.output_size
        if frame.width >= target_w and frame.height >= target_h:
            return frame
        target_w, target_h = int(target_w), int(target_h)
        self._make_current()
        gl = self._context.functions()
        try:
            self._upload(frame)
            self._ensure_fbos(target_w, target_h)
            assert self._fbo_easu is not None and self._fbo_rcas is not None
            self._fbo_easu.bind()
            gl.glViewport(0, 0, target_w, target_h)
            self._pass(self._programs[0], self._texture, (frame.width, frame.height), (target_w, target_h))
            self._fbo_rcas.bind()
            gl.glViewport(0, 0, target_w, target_h)
            # FBO texture isn't a QOpenGLTexture object; bind it directly.
            program = self._programs[1]
            program.bind()
            gl.glActiveTexture(0x84C0)
            gl.glBindTexture(0x0DE1, self._fbo_easu.texture())
            gl.glUniform1i(program.uniformLocation("sourceTex"), 0)
            gl.glUniform2f(program.uniformLocation("sourceSize"), float(target_w), float(target_h))
            gl.glUniform2f(program.uniformLocation("targetSize"), float(target_w), float(target_h))
            self._draw_quad(program)
            program.release()
            self._fbo_rcas.release()
            image = self._fbo_rcas.toImage().convertToFormat(QImage.Format.Format_ARGB32)
            if image.isNull():
                raise RuntimeError("FSR readback failed")
            # FBO 的像素原点在左下角；界面 QImage 以左上角为原点。
            image = image.mirrored(False, True)
            data = image.bits().tobytes()
            return Frame(
                data=data,
                width=target_w,
                height=target_h,
                pixel_format=PixelFormat.BGRA,
                timebase=frame.timebase,
                pts=frame.pts,
                stream_id=frame.stream_id,
            )
        finally:
            self._context.doneCurrent()

    def close(self) -> None:
        if self._context is None:
            return
        try:
            self._make_current()
            for fbo in (self._fbo_easu, self._fbo_rcas):
                if fbo is not None:
                    fbo.bind()
                    fbo.release()
            self._fbo_easu = self._fbo_rcas = None
            if self._texture is not None:
                self._texture.destroy()
                self._texture = None
            if self._vbo is not None:
                self._vbo.destroy()
                self._vbo = None
            if self._vao is not None:
                self._vao.destroy()
                self._vao = None
            for program in self._programs:
                program.removeAllShaders()
                program.deleteLater()
            self._programs.clear()
        finally:
            self._context.doneCurrent()
            if self._surface is not None:
                self._surface.destroy()
            self._context.deleteLater()
            self._context = None
            self._surface = None
