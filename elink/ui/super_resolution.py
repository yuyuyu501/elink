"""FSR 1 EASU/RCAS on GPU, using the unmodified AMD MIT-licensed headers."""
from functools import lru_cache

from PySide6.QtGui import QVector2D
from PySide6.QtOpenGL import (QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat,
                              QOpenGLShader, QOpenGLShaderProgram, QOpenGLTimeMonitor)

from ..paths import resource_dir


@lru_cache(maxsize=3)
def shader_source(stage):
    root = resource_dir() / 'fsr'
    common = '#version 420 compatibility\n#define A_GPU 1\n#define A_GLSL 1\n'
    common += (root / 'ffx_a.h').read_text(encoding='utf-8')
    common += '\nuniform sampler2D sourceTex;\nuniform vec2 sourceSize;\nuniform vec2 targetSize;\nin vec2 uv;\nout vec4 fragColor;\n'
    if stage == 'easu':
        common += '#define FSR_EASU_F 1\n'
        common += (root / 'ffx_fsr1.h').read_text(encoding='utf-8')
        common += '''
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
'''
    elif stage == 'rcas':
        common += '#define FSR_RCAS_F 1\n#define FSR_RCAS_DENOISE 1\n'
        common += (root / 'ffx_fsr1.h').read_text(encoding='utf-8')
        common += '''
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
'''
    elif stage == 'display':
        common += 'void main() { fragColor=texture(sourceTex,vec2(uv.x,1.0-uv.y)); }\n'
    else:
        raise ValueError('Unknown FSR pass')
    return common


VERTEX = '''#version 420 compatibility
in vec2 position;
in vec2 texcoord;
out vec2 uv;
void main() { gl_Position=vec4(position,0,1); uv=texcoord; }
'''


class FsrUpscaler:
    def __init__(self, surface, output_size):
        self.surface, self.output_size = surface, tuple(output_size)
        self.programs = []
        self.buffers = []
        self.timers, self.pending_timers = [], []
        self.key = None
        fmt = surface.context().format()
        if (fmt.majorVersion(), fmt.minorVersion()) < (4, 2):
            raise RuntimeError('FSR 1 requires OpenGL 4.2')
        for stage in ('easu', 'rcas', 'display'):
            program = QOpenGLShaderProgram(surface)
            self.programs.append(program)
            for kind, source in ((QOpenGLShader.ShaderTypeBit.Vertex, VERTEX),
                                 (QOpenGLShader.ShaderTypeBit.Fragment, shader_source(stage))):
                if not program.addShaderFromSourceCode(kind, source):
                    raise RuntimeError(program.log())
            program.bindAttributeLocation('position', 0)
            program.bindAttributeLocation('texcoord', 1)
            if not program.link():
                raise RuntimeError(program.log())
        for _ in range(4):
            monitor = QOpenGLTimeMonitor(surface)
            monitor.setSampleCount(2)
            if monitor.create():
                self.timers.append(monitor)

    def render(self, viewport, profile, new_frame):
        surface, frame = self.surface, self.surface.frame
        gl = surface.context().functions()
        while self.pending_timers and self.pending_timers[0].isResultAvailable():
            timer = self.pending_timers.pop(0)
            intervals = timer.waitForIntervals()
            if profile and intervals:
                profile.observe('fsr_gpu', intervals[0] / 1_000_000)
            timer.reset()
            self.timers.append(timer)
        timer = self.timers.pop() if new_frame and self.timers else None
        if timer:
            timer.recordSample()
        key = frame.width, frame.height, *self.output_size
        if key != self.key:
            self.buffers.clear()
            fmt = QOpenGLFramebufferObjectFormat()
            fmt.setInternalTextureFormat(0x8058)  # RGBA8, display-referred color.
            for width, height in ((frame.width, frame.height), self.output_size, self.output_size):
                buffer = QOpenGLFramebufferObject(width, height, fmt)
                if not buffer.isValid():
                    raise RuntimeError('Cannot allocate FSR framebuffer')
                self.buffers.append(buffer)
            self.key = key
            new_frame = True
        if new_frame:
            self.buffers[0].bind()
            gl.glViewport(0, 0, frame.width, frame.height)
            surface.draw_video()
            self.buffers[1].bind()
            gl.glViewport(0, 0, *self.output_size)
            self._pass(0, self.buffers[0].texture())
            self.buffers[2].bind()
            self._pass(1, self.buffers[1].texture())
        # QOpenGLWidget renders to its own FBO, not framebuffer zero.
        gl.glBindFramebuffer(0x8D40, surface.defaultFramebufferObject())
        gl.glViewport(*viewport)
        self._pass(2, self.buffers[2].texture())
        if timer:
            timer.recordSample()
            self.pending_timers.append(timer)
        if profile:
            profile.describe(upscaler='FSR1-EASU-RCAS', upscale_width=self.output_size[0], upscale_height=self.output_size[1])

    def _pass(self, index, texture):
        surface = self.surface
        gl = surface.context().functions()
        program = self.programs[index]
        if not program.bind():
            raise RuntimeError('Cannot bind FSR shader')
        gl.glActiveTexture(0x84C0)
        gl.glBindTexture(0x0DE1, texture)
        gl.glTexParameteri(0x0DE1, 0x2802, 0x812F)
        gl.glTexParameteri(0x0DE1, 0x2803, 0x812F)
        gl.glTexParameteri(0x0DE1, 0x2801, 0x2601)  # Linear when fitting the output to the window.
        gl.glTexParameteri(0x0DE1, 0x2800, 0x2601)
        gl.glUniform1i(program.uniformLocation('sourceTex'), 0)
        if program.uniformLocation('sourceSize') >= 0:
            program.setUniformValue('sourceSize', QVector2D(surface.frame.width, surface.frame.height))
        if program.uniformLocation('targetSize') >= 0:
            program.setUniformValue('targetSize', QVector2D(*self.output_size))
        surface.draw_quad(program)
        program.release()

    def cleanup(self):
        self.buffers.clear()
        self.key = None
        for timer in self.timers + self.pending_timers:
            timer.destroy()
            timer.deleteLater()
        self.timers.clear()
        self.pending_timers.clear()
        for program in self.programs:
            program.removeAllShaders()
            program.deleteLater()
        self.programs.clear()
