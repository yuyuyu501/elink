"""Windows display modes and per-monitor DPI for the captured display.

DPI uses the Windows DisplayConfig relative-DPI device packets (-3/-4).
Those packets are not a stable public contract: unsupported drivers/Windows
builds must report unavailable, never guess or modify registry values.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import os

from ..models import ValidationError


class DevMode(C.Structure):
    _fields_ = [('device', W.WCHAR * 32), ('spec', W.WORD), ('driver', W.WORD),
                ('size', W.WORD), ('extra', W.WORD), ('fields', W.DWORD),
                ('x', W.LONG), ('y', W.LONG), ('orientation', W.DWORD), ('fixed', W.DWORD),
                ('color', W.SHORT), ('duplex', W.SHORT), ('yres', W.SHORT), ('tt', W.SHORT),
                ('collate', W.SHORT), ('form', W.WCHAR * 32), ('logpixels', W.WORD),
                ('bits', W.DWORD), ('width', W.DWORD), ('height', W.DWORD), ('flags', W.DWORD),
                ('frequency', W.DWORD), ('icm', W.DWORD), ('intent', W.DWORD), ('media', W.DWORD),
                ('dither', W.DWORD), ('reserved1', W.DWORD), ('reserved2', W.DWORD),
                ('panning_width', W.DWORD), ('panning_height', W.DWORD)]


class DisplayDevice(C.Structure):
    _fields_ = [('size', W.DWORD), ('name', W.WCHAR * 32), ('description', W.WCHAR * 128),
                ('flags', W.DWORD), ('id', W.WCHAR * 128), ('key', W.WCHAR * 128)]


class Luid(C.Structure):
    _fields_ = [('low', W.DWORD), ('high', W.LONG)]


class Source(C.Structure):
    _fields_ = [('adapter', Luid), ('id', W.UINT), ('mode', W.UINT), ('flags', W.UINT)]


class Rational(C.Structure):
    _fields_ = [('numerator', W.UINT), ('denominator', W.UINT)]


class Target(C.Structure):
    _fields_ = [('adapter', Luid), ('id', W.UINT), ('mode', W.UINT), ('technology', W.UINT),
                ('rotation', W.UINT), ('scaling', W.UINT), ('refresh', Rational),
                ('scan', W.UINT), ('available', W.BOOL), ('flags', W.UINT)]


class DisplayPath(C.Structure):
    _fields_ = [('source', Source), ('target', Target), ('flags', W.UINT)]


class Header(C.Structure):
    _fields_ = [('type', W.UINT), ('size', W.UINT), ('adapter', Luid), ('id', W.UINT)]


class SourceName(C.Structure):
    _fields_ = [('header', Header), ('name', W.WCHAR * 32)]


class DpiGet(C.Structure):
    _fields_ = [('header', Header), ('minimum', W.INT), ('current', W.INT), ('maximum', W.INT)]


class DpiSet(C.Structure):
    _fields_ = [('header', Header), ('relative', W.INT)]


SCALES = (100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500)


def dpi_choices(minimum, current, maximum):
    base = -minimum
    if not (0 <= base < len(SCALES) and minimum <= current <= maximum
            and 0 <= base + current < len(SCALES) and 0 <= base + maximum < len(SCALES)):
        raise ValidationError('Windows 返回了无法识别的缩放范围。')
    return list(SCALES[:base + maximum + 1]), SCALES[base + current], SCALES[base]


def stream_size(width, height):
    """Fit actual display aspect into the current 1080p encoder budget."""
    ratio = min(1, 1920 / width, 1080 / height)
    return max(2, int(width * ratio) // 2 * 2), max(2, int(height * ratio) // 2 * 2)


class WindowsDisplay:
    def __init__(self):
        if os.name != 'nt':
            raise ValidationError('只有 Windows 被控端支持显示设置。')
        self.user = C.WinDLL('user32', use_last_error=True)
        self.user.EnumDisplayDevicesW.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(DisplayDevice), W.DWORD]
        self.user.EnumDisplaySettingsExW.argtypes = [W.LPCWSTR, W.DWORD, C.POINTER(DevMode), W.DWORD]
        self.user.ChangeDisplaySettingsExW.argtypes = [W.LPCWSTR, C.POINTER(DevMode), W.HWND, W.DWORD, C.c_void_p]
        self.user.ChangeDisplaySettingsExW.restype = W.LONG
        self.user.GetDisplayConfigBufferSizes.argtypes = [W.UINT, C.POINTER(W.UINT), C.POINTER(W.UINT)]
        self.user.QueryDisplayConfig.argtypes = [W.UINT, C.POINTER(W.UINT), C.POINTER(DisplayPath),
                                                C.POINTER(W.UINT), C.c_void_p, C.c_void_p]
        self.user.DisplayConfigGetDeviceInfo.argtypes = [C.POINTER(Header)]
        self.user.DisplayConfigSetDeviceInfo.argtypes = [C.POINTER(Header)]

    def primary(self):
        for index in range(64):
            device = DisplayDevice(size=C.sizeof(DisplayDevice))
            if not self.user.EnumDisplayDevicesW(None, index, C.byref(device), 0):
                break
            if device.flags & 1 and device.flags & 4:
                return device.name
        raise ValidationError('未找到活动的主显示器。')

    def mode(self, device, index=0xffffffff):
        value = DevMode(size=C.sizeof(DevMode))
        if not self.user.EnumDisplaySettingsExW(device, index, C.byref(value), 0):
            return None
        return value

    def modes(self, device):
        modes = []
        for index in range(4096):
            mode = self.mode(device, index)
            if mode is None:
                break
            if mode.bits >= 24 and mode.width >= 320 and mode.height >= 200 and not mode.flags & 2:
                modes.append(mode)
        return modes

    def dpi(self, device):
        for _ in range(3):
            paths, modes = W.UINT(), W.UINT()
            if self.user.GetDisplayConfigBufferSizes(2, C.byref(paths), C.byref(modes)):
                break
            if not (0 < paths.value <= 128 and modes.value <= 512):
                break
            path_buffer = (DisplayPath * paths.value)()
            mode_buffer = (C.c_uint64 * (8 * max(1, modes.value)))()  # DISPLAYCONFIG_MODE_INFO: 64 bytes, 8-aligned.
            result = self.user.QueryDisplayConfig(2, C.byref(paths), path_buffer, C.byref(modes), mode_buffer, None)
            if result == 122:
                continue
            if result:
                break
            for path in path_buffer[:paths.value]:
                name = SourceName(Header(1, C.sizeof(SourceName), path.source.adapter, path.source.id))
                if self.user.DisplayConfigGetDeviceInfo(C.byref(name.header)) or name.name != device:
                    continue
                packet = DpiGet(Header(0xfffffffd, C.sizeof(DpiGet), path.source.adapter, path.source.id))
                if self.user.DisplayConfigGetDeviceInfo(C.byref(packet.header)):
                    break
                choices, current, recommended = dpi_choices(packet.minimum, packet.current, packet.maximum)
                return packet, choices, current, recommended
            break
        raise ValidationError('此显示器或 Windows 版本不支持读取系统缩放。')

    def snapshot(self, device=None):
        device = device or self.primary()
        current = self.mode(device)
        if current is None:
            raise ValidationError('显示器已断开或无法读取当前分辨率。')
        modes = self.modes(device)
        resolutions = {(m.width, m.height) for m in modes}
        resolutions.add((current.width, current.height))
        display_modes = sorted({(m.width, m.height, int(m.frequency)) for m in modes
                                if m.frequency > 0})
        if (current.width, current.height, int(current.frequency)) not in display_modes:
            display_modes.append((current.width, current.height, int(current.frequency)))
        refresh_rates = sorted({rate for width, height, rate in display_modes
                                if [width, height] == [current.width, current.height]})
        result = dict(device=device, current=[current.width, current.height],
                      resolutions=[list(size) for size in sorted(resolutions, key=lambda x: (-x[0] * x[1], -x[0]))],
                      display_modes=[list(mode) for mode in display_modes],
                      current_refresh=int(current.frequency), refresh_rates=refresh_rates,
                      scales=[], scale=None, recommended_scale=None, scale_error='')
        try:
            _, result['scales'], result['scale'], result['recommended_scale'] = self.dpi(device)
        except ValidationError as exc:
            result['scale_error'] = str(exc)
        return result

    def set_scale(self, device, scale):
        packet, choices, _, _ = self.dpi(device)
        if type(scale) is not int or scale not in choices:
            raise ValidationError('该显示器不支持所选缩放比例，请重新读取设置。')
        value = DpiSet(Header(0xfffffffc, C.sizeof(DpiSet), packet.header.adapter, packet.header.id),
                       choices.index(scale) + packet.minimum)
        code = self.user.DisplayConfigSetDeviceInfo(C.byref(value.header))
        if code:
            raise ValidationError(f'Windows 拒绝修改缩放（{code}）。')

    def apply(self, change, device=None):
        before = self.snapshot(device)
        if not isinstance(change, dict) or change.get('device') != before['device']:
            raise ValidationError('显示器已变化，请重新打开画质设置。')
        resolution = change.get('resolution', before['current'])
        refresh = change.get('refresh', before.get('current_refresh'))
        scale = change.get('scale', before['scale'])
        if (not isinstance(resolution, list) or len(resolution) != 2
                or any(type(x) is not int for x in resolution) or resolution not in before['resolutions']):
            raise ValidationError('该显示器不支持所选分辨率，请重新读取设置。')
        if type(refresh) is not int or refresh <= 0:
            raise ValidationError('该显示器不支持所选刷新率，请重新读取设置。')
        if scale != before['scale'] and (type(scale) is not int or scale not in before['scales']):
            raise ValidationError('该显示器不支持所选系统缩放。')
        device = before['device']
        original = self.mode(device)
        if original is None:
            raise ValidationError('显示器已断开，请重新读取显示设置。')
        changed_mode = resolution != before['current']
        changed_refresh = refresh != before.get('current_refresh')
        changed_scale = scale != before['scale']
        if not changed_mode and not changed_refresh and not changed_scale:
            return before
        # Windows may adjust DPI when a mode changes; retain the explicitly
        # selected percentage even when it matched the old current value.
        apply_scale = changed_scale or (changed_mode and type(scale) is int)
        selected = None
        if changed_mode or changed_refresh:
            candidates = [m for m in self.modes(device) if [m.width, m.height] == resolution]
            if not candidates:
                raise ValidationError('分辨率已不可用，请重新读取设置。')
            exact = [m for m in candidates if int(m.frequency) == refresh]
            if not exact:
                raise ValidationError('该分辨率不支持所选刷新率，请重新读取设置。')
            selected = min(exact, key=lambda m: (m.orientation != original.orientation,
                                                  abs(m.frequency - original.frequency)))
            if self.user.ChangeDisplaySettingsExW(device, C.byref(selected), None, 2, None):
                raise ValidationError('显卡拒绝此显示模式。原显示设置未改变。')
        try:
            if changed_mode or changed_refresh:
                code = self.user.ChangeDisplaySettingsExW(device, C.byref(selected), None, 0, None)
                if code:
                    raise ValidationError(f'Windows 修改分辨率失败（{code}）。')
            if apply_scale:
                self.set_scale(device, scale)
            after = self.snapshot(device)
            if (after['current'] != resolution or after.get('current_refresh') != refresh
                    or (apply_scale and after['scale'] != scale)):
                raise ValidationError('Windows 未应用所选显示设置。')
            return after
        except Exception as exc:
            failures = []
            if (changed_mode or changed_refresh) and self.user.ChangeDisplaySettingsExW(device, C.byref(original), None, 0, None):
                failures.append('分辨率')
            if apply_scale:
                try:
                    self.set_scale(device, before['scale'])
                except Exception:
                    failures.append('缩放')
            try:
                restored = self.snapshot(device)
                if restored['current'] != before['current'] and '分辨率' not in failures:
                    failures.append('分辨率')
                if restored.get('current_refresh') != before.get('current_refresh') and '刷新率' not in failures:
                    failures.append('刷新率')
                if apply_scale and restored['scale'] != before['scale'] and '缩放' not in failures:
                    failures.append('缩放')
            except Exception:
                failures.append('无法确认恢复结果')
            suffix = '；恢复失败：' + '、'.join(failures) if failures else '；已恢复原显示设置'
            raise ValidationError(str(exc) + suffix) from exc
