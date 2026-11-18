"""YUV texture presentation adapted from the previous desktop player."""

import struct

from shiboken6 import VoidPtr
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPainter, QSurfaceFormat
from PySide6.QtOpenGL import (
    QOpenGLBuffer, QOpenGLPixelTransferOptions, QOpenGLShader,
    QOpenGLShaderProgram, QOpenGLTexture, QOpenGLVertexArrayObject,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget

VERTEX = """#version 120
attribute vec2 position;
attribute vec2 texcoord;
varying vec2 uv;
void main() { gl_Position = vec4(position, 0.0, 1.0); uv = texcoord; }
"""
FRAGMENT = """#version 120
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
"""


def color_parameters(frame):
    space = int(frame.colorspace)
    if space not in (1, 2, 5, 6):
        raise ValueError(f"Unsupported YUV matrix: {space}")
    full = int(frame.color_range) == 2
    coefficients = ((1.5748, -.187324, -.468124, 1.8556) if space == 1
                    else (1.402, -.344136, -.714136, 1.772))
    return (0., 1., 1., coefficients) if full else (16 / 255, 255 / 219, 255 / 224, coefficients)


class VideoOverlay(QWidget):
    def __init__(self, preview):
        super().__init__(preview)
        self.preview = preview
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def paintEvent(self, event):
        painter = QPainter(self)
        self.preview._paint_overlays(painter, self.preview._target_rect())
        painter.end()


class YuvSurface(QOpenGLWidget):
    failed = Signal(str)

    def __init__(self, preview):
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CompatibilityProfile)
        fmt.setSwapInterval(0)
        super().__init__(preview)
        self.setFormat(fmt)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.preview = preview
        self.frame = None
        self.pending = False
        self.broken = False
        self.program = self.vbo = self.vao = None
        self.textures = []
        self.layout_key = None

    def initializeGL(self):
        try:
            self.context().aboutToBeDestroyed.connect(self.cleanup)
            self.program = QOpenGLShaderProgram(self)
            for kind, source in ((QOpenGLShader.ShaderTypeBit.Vertex, VERTEX),
                                 (QOpenGLShader.ShaderTypeBit.Fragment, FRAGMENT)):
                if not self.program.addShaderFromSourceCode(kind, source):
                    raise RuntimeError(self.program.log())
            self.program.bindAttributeLocation("position", 0)
            self.program.bindAttributeLocation("texcoord", 1)
            if not self.program.link():
                raise RuntimeError(self.program.log())
            self.vao = QOpenGLVertexArrayObject(self)
            self.vao.create()
            self.vao.bind()
            self.vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            self.vbo.create()
            self.vbo.bind()
            vertices = struct.pack("16f", -1, -1, 0, 1, 1, -1, 1, 1, -1, 1, 0, 0, 1, 1, 1, 0)
            self.vbo.allocate(vertices, len(vertices))
            self.vbo.release()
            self.vao.release()
        except Exception as exc:
            self.fail(exc)

    def fail(self, exc):
        if not self.broken:
            self.broken = True
            self.failed.emit(str(exc))

    def set_frame(self, frame):
        color_parameters(frame)
        self.frame = frame
        self.pending = True
        self.update()

    def upload(self):
        frame = self.frame
        key = frame.width, frame.height, frame.format.name
        if key != self.layout_key:
            for texture in self.textures:
                texture.destroy()
            self.textures = []
            for index, plane in enumerate(frame.planes):
                two = frame.format.name == "nv12" and index == 1
                texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
                texture.setFormat(QOpenGLTexture.TextureFormat.RG8_UNorm if two
                                  else QOpenGLTexture.TextureFormat.R8_UNorm)
                texture.setSize(plane.width, plane.height)
                texture.setMipLevels(1)
                texture.allocateStorage()
                texture.setMinMagFilters(QOpenGLTexture.Filter.Linear, QOpenGLTexture.Filter.Linear)
                texture.setWrapMode(QOpenGLTexture.WrapMode.ClampToEdge)
                self.textures.append(texture)
            self.layout_key = key
        for index, (plane, texture) in enumerate(zip(frame.planes, self.textures)):
            two = frame.format.name == "nv12" and index == 1
            transfer = QOpenGLPixelTransferOptions()
            transfer.setAlignment(1)
            transfer.setRowLength(plane.line_size // (2 if two else 1))
            texture.setData(QOpenGLTexture.PixelFormat.RG if two else QOpenGLTexture.PixelFormat.Red,
                            QOpenGLTexture.PixelType.UInt8, VoidPtr(plane.buffer_ptr, plane.buffer_size), transfer)

    def paintGL(self):
        if self.broken or self.frame is None or self.program is None:
            return
        try:
            gl = self.context().functions()
            for capability in (0x0BE2, 0x0B71, 0x0B90, 0x0C11, 0x0B44):
                gl.glDisable(capability)
            gl.glClearColor(0, 0, 0, 1)
            gl.glClear(0x4000)
            if self.pending:
                self.upload()
            rect = self.preview._target_rect()
            ratio = self.devicePixelRatioF()
            gl.glViewport(round(rect.x() * ratio), round((self.height() - rect.bottom() - 1) * ratio),
                          round(rect.width() * ratio), round(rect.height() * ratio))
            self.program.bind()
            for index, texture in enumerate(self.textures):
                texture.bind(index)
            for name, index in (("ytex", 0), ("utex", 1), ("vtex", 2)):
                gl.glUniform1i(self.program.uniformLocation(name), index)
            offset, gain, cgain, coefficients = color_parameters(self.frame)
            gl.glUniform1i(self.program.uniformLocation("semiplanar"), int(self.frame.format.name == "nv12"))
            gl.glUniform1f(self.program.uniformLocation("yoffset"), offset)
            gl.glUniform1f(self.program.uniformLocation("ygain"), gain)
            gl.glUniform1f(self.program.uniformLocation("cgain"), cgain)
            gl.glUniform4f(self.program.uniformLocation("coefficients"), *coefficients)
            self.vao.bind()
            self.vbo.bind()
            for index, offset in ((0, 0), (1, 8)):
                self.program.enableAttributeArray(index)
                self.program.setAttributeBuffer(index, 0x1406, offset, 2, 16)
            gl.glDrawArrays(0x0005, 0, 4)
            self.vbo.release()
            self.vao.release()
            self.program.release()
            error = gl.glGetError()
            if error:
                raise RuntimeError(f"Video OpenGL error: {error}")
            if self.pending:
                self.preview.presented.emit()
            self.pending = False
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
        if self.vao:
            self.vao.destroy()
        if self.program:
            self.program.removeAllShaders()
        self.doneCurrent()
