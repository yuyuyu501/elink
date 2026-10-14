"""Experimental YUV texture presenter; CPU planes, explicitly not zero-copy."""
import struct
import time
from shiboken6 import VoidPtr

from PySide6.QtCore import QRect, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QSurfaceFormat
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLPixelTransferOptions,
                              QOpenGLShader, QOpenGLShaderProgram, QOpenGLTexture,
                              QOpenGLVertexArrayObject)
from PySide6.QtOpenGLWidgets import QOpenGLWidget


VERTEX = '''#version 120
attribute vec2 position;
attribute vec2 texcoord;
varying vec2 uv;
void main() { gl_Position = vec4(position, 0.0, 1.0); uv = texcoord; }
'''
FRAGMENT = '''#version 120
uniform sampler2D ytex;
uniform sampler2D utex;
uniform sampler2D vtex;
uniform int semiplanar;
uniform float yoffset;
uniform float ygain;
uniform float cgain;
uniform vec4 coefficients;
varying vec2 uv;
void main() {
    float y = (texture2D(ytex, uv).r - yoffset) * ygain;
    vec2 c = vec2(texture2D(utex, uv).r,
                 semiplanar == 1 ? texture2D(utex, uv).g : texture2D(vtex, uv).r);
    c = (c - vec2(128.0 / 255.0)) * cgain;
    gl_FragColor = vec4(y + coefficients.x*c.y,
                       y + coefficients.y*c.x + coefficients.z*c.y,
                       y + coefficients.w*c.x, 1.0);
}
'''


def color_parameters(frame):
    # Unspecified FFmpeg YUV uses the same 601 fallback as the existing swscale
    # path. Do not guess 709 solely from image dimensions.
    space = int(frame.colorspace)
    if space not in (1, 2, 5, 6):
        raise ValueError(f'Unsupported YUV color matrix: {space}')
    full = int(frame.color_range) == 2 or frame.format.name == 'yuvj420p'
    coeff = (1.5748, -.187324, -.468124, 1.8556) if space == 1 else (1.402, -.344136, -.714136, 1.772)
    return (0.0, 1.0, 1.0, coeff) if full else (16 / 255, 255 / 219, 255 / 224, coeff)


class YuvSurface(QOpenGLWidget):
    failed = Signal(str)

    def __init__(self, player):
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 0)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
        fmt.setSwapInterval(0)
        # Qt's top-level composition context uses the default format, not just
        # the child widget format. Request uncapped submission for both.
        QSurfaceFormat.setDefaultFormat(fmt)
        super().__init__(player)
        self.player = player
        self.frame = None
        self.pending = False
        self.broken = False
        self.program = self.vbo = self.vao = None
        self.textures = []
        self.layout_key = None
        self.setFormat(fmt)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def fail(self, exc):
        if not self.broken:
            self.broken = True
            self.failed.emit(str(exc))

    def initializeGL(self):
        try:
            self.context().aboutToBeDestroyed.connect(self.cleanup)
            self.program = QOpenGLShaderProgram(self)
            for kind, source in ((QOpenGLShader.ShaderTypeBit.Vertex, VERTEX),
                                 (QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT)):
                if not self.program.addShaderFromSourceCode(kind, source):
                    raise RuntimeError(self.program.log())
            self.program.bindAttributeLocation('position', 0)
            self.program.bindAttributeLocation('texcoord', 1)
            if not self.program.link():
                raise RuntimeError(self.program.log())
            self.vao = QOpenGLVertexArrayObject(self)
            if not self.vao.create():
                raise RuntimeError('Cannot create video vertex array')
            self.vao.bind()
            self.vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            if not self.vbo.create() or not self.vbo.bind():
                raise RuntimeError('Cannot create video vertex buffer')
            # Texture coordinate origin is at the first (top) scanline.
            vertices = struct.pack('16f', -1, -1, 0, 1, 1, -1, 1, 1,
                                   -1, 1, 0, 0, 1, 1, 1, 0)
            self.vbo.allocate(vertices, len(vertices))
            self.vbo.release()
            self.vao.release()
            self.pending = self.frame is not None
        except Exception as exc:
            self.fail(exc)

    def set_frame(self, frame):
        color_parameters(frame)  # Unsupported color metadata falls back to CPU.
        self.frame = frame  # Own every plane until OpenGL has consumed it.
        self.pending = True
        self.update()

    def upload(self):
        frame = self.frame
        if frame.format.name not in ('yuv420p', 'nv12', 'yuvj420p'):
            raise ValueError('Unsupported GPU plane format')
        key = frame.width, frame.height, frame.format.name
        if key != self.layout_key:
            for texture in self.textures:
                texture.destroy()
            self.textures = []
            for index, plane in enumerate(frame.planes):
                texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
                two = frame.format.name == 'nv12' and index == 1
                texture.setFormat(QOpenGLTexture.TextureFormat.RG8_UNorm if two else QOpenGLTexture.TextureFormat.R8_UNorm)
                texture.setSize(plane.width, plane.height)
                texture.setMipLevels(1)
                texture.allocateStorage()
                texture.setMinMagFilters(QOpenGLTexture.Filter.Linear, QOpenGLTexture.Filter.Linear)
                texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
                if not texture.isStorageAllocated():
                    raise RuntimeError('Cannot allocate video texture')
                self.textures.append(texture)
            self.layout_key = key
        for index, (plane, texture) in enumerate(zip(frame.planes, self.textures)):
            two = frame.format.name == 'nv12' and index == 1
            transfer = QOpenGLPixelTransferOptions()
            transfer.setAlignment(1)
            transfer.setRowLength(plane.line_size // (2 if two else 1))
            texture.setData(QOpenGLTexture.PixelFormat.RG if two else QOpenGLTexture.PixelFormat.Red,
                            QOpenGLTexture.PixelType.UInt8, VoidPtr(plane.buffer_ptr, plane.buffer_size), transfer)

    def paintGL(self):
        if self.broken or not self.program:
            return
        profile = getattr(self.player.client, 'profile', None)
        started = time.perf_counter()
        try:
            gl = self.context().functions()
            for capability in (0x0BE2, 0x0B71, 0x0B90, 0x0C11, 0x0B44):
                gl.glDisable(capability)  # blend, depth, stencil, scissor, cull
            gl.glColorMask(True, True, True, True)
            gl.glClearColor(16 / 255, 23 / 255, 25 / 255, 1)
            gl.glClear(0x4000)
            if self.frame is not None:
                viewport = self.player.video_viewport()
                size = QSize(self.frame.width, self.frame.height).scaled(viewport.size(), Qt.AspectRatioMode.KeepAspectRatio)
                rect = QRect((self.width() - size.width()) // 2,
                             viewport.top() + (viewport.height() - size.height()) // 2, size.width(), size.height())
                self.player.rect_image = rect
                if self.pending:
                    upload_at = time.perf_counter()
                    self.upload()
                    error = gl.glGetError()
                    if error:
                        raise RuntimeError(f"OpenGL upload failed: {error}")
                    if profile:
                        profile.elapsed('texture_upload_call', upload_at)
                ratio = self.devicePixelRatioF()
                gl.glViewport(round(rect.x() * ratio), round((self.height() - rect.y() - rect.height()) * ratio),
                              round(rect.width() * ratio), round(rect.height() * ratio))
                if not self.program.bind():
                    raise RuntimeError('Cannot bind video shader')
                for index, texture in enumerate(self.textures):
                    texture.bind(index)
                for name, index in [('ytex', 0), ('utex', 1), ('vtex', 2)]:
                    gl.glUniform1i(self.program.uniformLocation(name), index)
                offset, gain, cgain, coeff = color_parameters(self.frame)
                gl.glUniform1i(self.program.uniformLocation('semiplanar'), int(self.frame.format.name == 'nv12'))
                gl.glUniform1f(self.program.uniformLocation('yoffset'), offset)
                gl.glUniform1f(self.program.uniformLocation('ygain'), gain)
                gl.glUniform1f(self.program.uniformLocation('cgain'), cgain)
                gl.glUniform4f(self.program.uniformLocation('coefficients'), *coeff)
                error = gl.glGetError()
                if error:
                    raise RuntimeError(f"OpenGL uniforms failed: {error}")
                self.vao.bind()
                if not self.vbo.bind():
                    raise RuntimeError('Cannot bind video vertex buffer')
                for index, offset in ((0, 0), (1, 8)):
                    self.program.enableAttributeArray(index)
                    self.program.setAttributeBuffer(index, 0x1406, offset, 2, 16)
                error = gl.glGetError()
                if error:
                    raise RuntimeError(f'OpenGL attributes failed: {error}')
                gl.glDrawArrays(0x0005, 0, 4)  # GL_TRIANGLE_STRIP
                self.program.disableAttributeArray(0)
                self.program.disableAttributeArray(1)
                self.vbo.release()
                self.vao.release()
                self.program.release()
                error = gl.glGetError()
                if error:
                    raise RuntimeError(f'OpenGL video upload/draw failed: {error}')
                if self.pending:
                    self.player.frame_presented()
                self.pending = False
            painter = QPainter(self)
            self.player.paint_overlay(painter)
            painter.end()
            if profile:
                profile.elapsed('gpu_paint_submit', started)
        except Exception as exc:
            self.fail(exc)

    def cleanup(self):
        if self.context() is None:
            return
        self.makeCurrent()
        for texture in self.textures:
            texture.destroy()
        self.textures = []
        self.layout_key = None
        if self.vbo:
            self.vbo.destroy()
            self.vbo = None
        if self.vao:
            self.vao.destroy()
            self.vao.deleteLater()
            self.vao = None
        if self.program:
            self.program.removeAllShaders()
            self.program.deleteLater()
            self.program = None
        self.doneCurrent()
