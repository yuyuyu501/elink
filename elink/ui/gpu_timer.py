"""Nonblocking OpenGL elapsed queries, owned by the current render context."""
import ctypes as C


class ElapsedQuery:
    def __init__(self):
        library = C.WinDLL('opengl32')
        library.wglGetProcAddress.argtypes = [C.c_char_p]
        library.wglGetProcAddress.restype = C.c_void_p
        def function(name, *arguments):
            address = library.wglGetProcAddress(name.encode('ascii'))
            if not address or address in (1, 2, 3, C.c_void_p(-1).value):
                raise RuntimeError(f'OpenGL timer unavailable: {name}')
            return C.WINFUNCTYPE(None, *arguments)(address)
        self.generate = function('glGenQueries', C.c_int, C.POINTER(C.c_uint))
        self.delete = function('glDeleteQueries', C.c_int, C.POINTER(C.c_uint))
        self.start = function('glBeginQuery', C.c_uint, C.c_uint)
        self.finish = function('glEndQuery', C.c_uint)
        self.get = function('glGetQueryObjectuiv', C.c_uint, C.c_uint, C.POINTER(C.c_uint))
        self.identifier = C.c_uint()
        self.generate(1, C.byref(self.identifier))
        self.active = False

    def begin(self):
        self.start(0x88BF, self.identifier)  # GL_TIME_ELAPSED
        self.active = True

    def end(self):
        self.finish(0x88BF)
        self.active = False

    def ready(self):
        value = C.c_uint()
        self.get(self.identifier, 0x8867, C.byref(value))  # GL_QUERY_RESULT_AVAILABLE
        return bool(value.value)

    def nanoseconds(self):
        value = C.c_uint()
        self.get(self.identifier, 0x8866, C.byref(value))  # GL_QUERY_RESULT
        return value.value

    def close(self):
        if self.identifier.value:
            if self.active:
                self.end()
            self.delete(1, C.byref(self.identifier))
            self.identifier.value = 0
