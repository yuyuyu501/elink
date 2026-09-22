"""Composite the Windows cursor omitted by DXcam's desktop duplication frames."""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W

import numpy as np


class CursorInfo(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("flags", W.DWORD), ("hCursor", W.HANDLE),
                ("ptScreenPos", W.POINT)]


class IconInfo(C.Structure):
    _fields_ = [("fIcon", W.BOOL), ("xHotspot", W.DWORD), ("yHotspot", W.DWORD),
                ("hbmMask", W.HANDLE), ("hbmColor", W.HANDLE)]


class Bitmap(C.Structure):
    _fields_ = [("bmType", W.LONG), ("bmWidth", W.LONG), ("bmHeight", W.LONG),
                ("bmWidthBytes", W.LONG), ("bmPlanes", W.WORD),
                ("bmBitsPixel", W.WORD), ("bmBits", C.c_void_p)]


class BitmapHeader(C.Structure):
    _fields_ = [("size", W.DWORD), ("width", W.LONG), ("height", W.LONG),
                ("planes", W.WORD), ("bits", W.WORD), ("compression", W.DWORD),
                ("image_size", W.DWORD), ("xppm", W.LONG), ("yppm", W.LONG),
                ("used", W.DWORD), ("important", W.DWORD)]


def cursor_rect(shape, position, hotspot, size, origin=(0, 0)):
    """Destination bounds and matching source offset, in physical output pixels."""
    x, y = (position[i] - origin[i] - hotspot[i] for i in range(2))
    left, top = max(0, x), max(0, y)
    right, bottom = min(shape[1], x + size[0]), min(shape[0], y + size[1])
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom, left - x, top - y


class WindowsCursor:
    def __init__(self):
        self.user = C.WinDLL("user32", use_last_error=True)
        self.gdi = C.WinDLL("gdi32", use_last_error=True)
        signatures = [
            (self.user.GetCursorInfo, [C.POINTER(CursorInfo)], W.BOOL),
            (self.user.GetPhysicalCursorPos, [C.POINTER(W.POINT)], W.BOOL),
            (self.user.GetIconInfo, [W.HANDLE, C.POINTER(IconInfo)], W.BOOL),
            (self.user.DrawIconEx, [W.HDC, C.c_int, C.c_int, W.HANDLE, C.c_int,
                                  C.c_int, W.UINT, W.HBRUSH, W.UINT], W.BOOL),
            (self.gdi.GetObjectW, [W.HANDLE, C.c_int, C.c_void_p], C.c_int),
            (self.gdi.CreateCompatibleDC, [W.HDC], W.HDC),
            (self.gdi.CreateDIBSection, [W.HDC, C.c_void_p, W.UINT,
                                       C.POINTER(C.c_void_p), W.HANDLE, W.DWORD], W.HBITMAP),
            (self.gdi.SelectObject, [W.HDC, W.HANDLE], W.HANDLE),
            (self.gdi.DeleteObject, [W.HANDLE], W.BOOL),
            (self.gdi.DeleteDC, [W.HDC], W.BOOL),
            (self.gdi.GdiFlush, [], W.BOOL),
        ]
        for function, args, result in signatures:
            function.argtypes, function.restype = args, result

    def visible(self):
        info = CursorInfo(cbSize=C.sizeof(CursorInfo))
        if not self.user.GetCursorInfo(C.byref(info)):
            return None
        return bool(info.flags & 1)

    def composite(self, pixels, origin=(0, 0)):
        info = CursorInfo(cbSize=C.sizeof(CursorInfo))
        if not self.user.GetCursorInfo(C.byref(info)) or not info.flags & 1:
            return pixels  # Includes intentionally hidden game cursors.
        point = info.ptScreenPos
        self.user.GetPhysicalCursorPos(C.byref(point))
        return self.draw(pixels, info.hCursor, (point.x, point.y), origin)

    def draw(self, pixels, handle, position, origin=(0, 0)):
        icon = IconInfo()
        if not self.user.GetIconInfo(handle, C.byref(icon)):
            return pixels
        dc = dib = previous = None
        try:
            bitmap = Bitmap()
            if not self.gdi.GetObjectW(icon.hbmColor or icon.hbmMask, C.sizeof(bitmap), C.byref(bitmap)):
                return pixels
            width, height = bitmap.bmWidth, bitmap.bmHeight
            if not icon.hbmColor:
                height //= 2  # Monochrome cursor has stacked AND/XOR masks.
            if not (0 < width <= 1024 and 0 < height <= 1024):
                return pixels
            bounds = cursor_rect(pixels.shape, position, (icon.xHotspot, icon.yHotspot),
                                 (width, height), origin)
            if bounds is None:
                return pixels
            left, top, right, bottom, sx, sy = bounds
            dc = self.gdi.CreateCompatibleDC(None)
            header = BitmapHeader(size=C.sizeof(BitmapHeader), width=width,
                                  height=-height, planes=1, bits=32)
            bits = C.c_void_p()
            dib = self.gdi.CreateDIBSection(dc, C.byref(header), 0, C.byref(bits), None, 0)
            if not dc or not dib:
                return pixels
            previous = self.gdi.SelectObject(dc, dib)
            surface = np.ctypeslib.as_array((C.c_ubyte * (width * height * 4)).from_address(bits.value))
            surface = surface.reshape(height, width, 4)
            surface.fill(0)
            patch = surface[sy:sy + bottom - top, sx:sx + right - left, :3]
            patch[:] = pixels[top:bottom, left:right]
            # GDI handles alpha and monochrome inversion against the actual background.
            if not self.user.DrawIconEx(dc, 0, 0, handle, width, height, 0, None, 3):
                return pixels
            self.gdi.GdiFlush()
            result = pixels.copy()  # Never paint on the cached, cursor-free DXGI image.
            result[top:bottom, left:right] = patch
            return result
        finally:
            if previous:
                self.gdi.SelectObject(dc, previous)
            for obj in (dib, icon.hbmColor, icon.hbmMask):
                if obj:
                    self.gdi.DeleteObject(obj)
            if dc:
                self.gdi.DeleteDC(dc)
