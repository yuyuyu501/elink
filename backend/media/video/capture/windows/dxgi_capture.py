"""Direct Desktop Duplication on the adapter that owns the selected display."""

from __future__ import annotations

import ctypes as ct
from ctypes import wintypes as wt
from uuid import UUID

from backend.media.frame import Frame
from .dxgi_desktop_duplication import CaptureError

_PTR = ct.c_void_p
_OUT = ct.POINTER(_PTR)
_HR = ct.c_long
_NOT_FOUND = 0x887A0002
_WAIT_TIMEOUT = 0x887A0027


class _OutputDesc(ct.Structure):
    _fields_ = [("name", wt.WCHAR * 32), ("bounds", wt.RECT),
                ("attached", wt.BOOL), ("rotation", wt.UINT), ("monitor", wt.HANDLE)]


class _TextureDesc(ct.Structure):
    _fields_ = [(name, wt.UINT) for name in (
        "width", "height", "mip_levels", "array_size", "format", "sample_count",
        "sample_quality", "usage", "bind_flags", "cpu_access_flags", "misc_flags",
    )]


class _MappedResource(ct.Structure):
    _fields_ = [("data", _PTR), ("row_pitch", wt.UINT), ("depth_pitch", wt.UINT)]


class _FrameInfo(ct.Structure):
    _fields_ = [("present_time", ct.c_int64), ("mouse_time", ct.c_int64),
                ("accumulated", wt.UINT), ("rects_coalesced", wt.BOOL),
                ("protected_content", wt.BOOL), ("pointer_position", wt.POINT),
                ("pointer_visible", wt.BOOL), ("metadata_size", wt.UINT),
                ("pointer_shape_size", wt.UINT)]


def _call(pointer, slot, restype, argtypes=(), *args):
    table = ct.cast(pointer, ct.POINTER(ct.POINTER(_PTR))).contents
    return ct.WINFUNCTYPE(restype, _PTR, *argtypes)(table[slot])(pointer, *args)


def _release(pointer):
    if pointer:
        _call(pointer, 2, wt.ULONG)


def _check(result, operation):
    if result < 0:
        raise CaptureError(f"{operation} 失败 (0x{result & 0xffffffff:08X})")


def _guid(value):
    return (ct.c_ubyte * 16).from_buffer_copy(UUID(value).bytes_le)


def _query(pointer, guid):
    result = _PTR()
    _check(_call(pointer, 0, _HR, (_PTR, _OUT), ct.byref(_guid(guid)),
                 ct.byref(result)), "QueryInterface")
    return result


def _enumerate(pointer, slot, operation):
    index = 0
    while True:
        item = _PTR()
        result = _call(pointer, slot, _HR, (wt.UINT, _OUT), index, ct.byref(item))
        if result & 0xffffffff == _NOT_FOUND:
            break
        _check(result, operation)
        try:
            yield index, item
        finally:
            _release(item)
        index += 1


def _with_output(region, use_output):
    from contextlib import closing

    create = ct.WinDLL("dxgi").CreateDXGIFactory1
    create.restype, create.argtypes = _HR, (_PTR, _OUT)
    factory = _PTR()
    _check(create(ct.byref(_guid("770aae78-f26f-4dba-a829-253c83d1b387")),
                  ct.byref(factory)), "CreateDXGIFactory1")
    try:
        device_index = 0
        with closing(_enumerate(factory, 12, "EnumAdapters1")) as adapters:
            for _, adapter in adapters:
                has_outputs = False
                with closing(_enumerate(adapter, 7, "EnumOutputs")) as outputs:
                    for output_index, output in outputs:
                        has_outputs = True
                        desc = _OutputDesc()
                        _check(_call(output, 7, _HR, (ct.POINTER(_OutputDesc),),
                                     ct.byref(desc)), "IDXGIOutput.GetDesc")
                        bounds = desc.bounds
                        x, y, w, h = region or (0, 0, 1, 1)
                        if (desc.attached and bounds.left <= x and bounds.top <= y
                                and x + w <= bounds.right and y + h <= bounds.bottom):
                            return use_output(adapter, output, desc, device_index, output_index)
                device_index += has_outputs
    finally:
        _release(factory)
    raise CaptureError("找不到采集区域对应的显示器")


def dxcam_output(region):
    return _with_output(region, lambda _adapter, _output, desc, device, index: (
        device, index, desc.bounds.left, desc.bounds.top,
    ))


class DxgiCapture:
    def __init__(self, region):
        self.region = region
        self._device, self._context, self._duplication, self._staging = (_PTR() for _ in range(4))
        self._last = None
        self._first_pending = False

    def open(self):
        try:
            _with_output(self.region, self._open_output)
            self._last = self._read(1000)
            if self._last is None:
                raise CaptureError("DXGI 未能取得首帧")
            self._first_pending = True
        except Exception:
            self.close()
            raise

    def _open_output(self, adapter, output, desc, _device_index, _output_index):
        create = ct.WinDLL("d3d11").D3D11CreateDevice
        create.restype = _HR
        create.argtypes = (_PTR, wt.UINT, wt.HMODULE, wt.UINT, _PTR, wt.UINT,
                           wt.UINT, _OUT, _PTR, _OUT)
        _check(create(adapter, 0, None, 0x20, None, 0, 7, ct.byref(self._device),
                      None, ct.byref(self._context)), "D3D11CreateDevice")
        output1 = _query(output, "00cddea8-939b-4b83-a340-a685226666cc")
        try:
            _check(_call(output1, 22, _HR, (_PTR, _OUT), self._device,
                         ct.byref(self._duplication)), "DuplicateOutput")
        finally:
            _release(output1)
        bounds = desc.bounds
        self._rotation = max(0, desc.rotation - 1)
        x, y, self.width, self.height = self.region or (
            bounds.left, bounds.top, bounds.right - bounds.left, bounds.bottom - bounds.top)
        self.offset = x - bounds.left, y - bounds.top

    def _read(self, timeout_ms):
        info, resource = _FrameInfo(), _PTR()
        result = _call(self._duplication, 8, _HR, (wt.UINT, ct.POINTER(_FrameInfo), _OUT),
                       timeout_ms, ct.byref(info), ct.byref(resource))
        if result & 0xffffffff == _WAIT_TIMEOUT:
            return None
        _check(result, "AcquireNextFrame")
        try:
            texture = _query(resource, "6f15aaf2-d208-4e89-9ab4-489535d34f9c")
            try:
                if not self._staging:
                    desc = _TextureDesc()
                    _call(texture, 10, None, (ct.POINTER(_TextureDesc),), ct.byref(desc))
                    self._surface_width, self._surface_height = desc.width, desc.height
                    desc.usage, desc.bind_flags, desc.cpu_access_flags, desc.misc_flags = 3, 0, 0x20000, 0
                    _check(_call(self._device, 5, _HR, (ct.POINTER(_TextureDesc), _PTR, _OUT),
                                 ct.byref(desc), None, ct.byref(self._staging)), "CreateTexture2D")
                _call(self._context, 47, None, (_PTR, _PTR), self._staging, texture)
            finally:
                _release(texture)
            return self._read_staging()
        finally:
            _release(resource)
            _call(self._duplication, 14, _HR)

    def _read_staging(self):
        import numpy as np

        mapped = _MappedResource()
        _check(_call(self._context, 14, _HR, (_PTR, wt.UINT, wt.UINT, wt.UINT,
                                             ct.POINTER(_MappedResource)),
                     self._staging, 0, 1, 0, ct.byref(mapped)), "Map")
        try:
            pixels = np.ndarray(
                (self._surface_height, self._surface_width, 4), dtype=np.uint8,
                buffer=(ct.c_ubyte * (self._surface_height * mapped.row_pitch)).from_address(mapped.data),
                strides=(mapped.row_pitch, 4, 1),
            )
            x, y = self.offset
            pixels = np.rot90(pixels, -self._rotation)[y:y + self.height, x:x + self.width]
            return pixels.tobytes()
        finally:
            _call(self._context, 15, None, (_PTR, wt.UINT), self._staging, 0)

    def read(self):
        if self._first_pending:
            self._first_pending = False
        else:
            data = self._read(0)
            if data is not None:
                self._last = data
        return Frame(data=self._last, width=self.width, height=self.height)

    def close(self):
        for pointer in (self._staging, self._duplication, self._context, self._device):
            _release(pointer)
            pointer.value = None
        self._last = None
        self._first_pending = False
