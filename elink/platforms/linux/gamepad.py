"""SDL controller polling and kernel uinput gamepads; no device permission changes."""
import ctypes as C
import ctypes.util


class SDLGamepads:
    def __init__(self):
        self.devices = {}
        library = ctypes.util.find_library('SDL2-2.0')
        self.library = C.CDLL(library) if library else None
        if not self.library:
            return
        sdl = self.library
        sdl.SDL_GameControllerOpen.argtypes = [C.c_int]
        sdl.SDL_GameControllerOpen.restype = C.c_void_p
        sdl.SDL_GameControllerGetAttached.argtypes = [C.c_void_p]
        sdl.SDL_GameControllerGetButton.argtypes = [C.c_void_p, C.c_int]
        sdl.SDL_GameControllerGetAxis.argtypes = [C.c_void_p, C.c_int]
        sdl.SDL_GameControllerGetAxis.restype = C.c_int16
        sdl.SDL_GameControllerClose.argtypes = [C.c_void_p]
        sdl.SDL_GameControllerRumble.argtypes = [C.c_void_p, C.c_uint16, C.c_uint16, C.c_uint32]
        if sdl.SDL_InitSubSystem(0x2000 | 0x200):
            self.library = None

    def read(self, index):
        sdl = self.library
        if sdl is None:
            return None
        sdl.SDL_GameControllerUpdate()
        device = self.devices.get(index)
        if device and not sdl.SDL_GameControllerGetAttached(device):
            sdl.SDL_GameControllerClose(device)
            del self.devices[index]
            device = None
        if not device and index < sdl.SDL_NumJoysticks() and sdl.SDL_IsGameController(index):
            device = self.devices[index] = sdl.SDL_GameControllerOpen(index)
        if not device:
            return None
        masks = (0x1000, 0x2000, 0x4000, 0x8000, 0x20, 0, 0x10, 0x40, 0x80, 0x100, 0x200, 1, 2, 4, 8)
        buttons = sum(mask for i, mask in enumerate(masks) if mask and sdl.SDL_GameControllerGetButton(device, i))
        axes = [int(sdl.SDL_GameControllerGetAxis(device, axis)) for axis in range(6)]
        return [buttons, max(0, axes[4]) * 255 // 32767, max(0, axes[5]) * 255 // 32767,
                axes[0], max(-32768, min(32767, -axes[1])), axes[2], max(-32768, min(32767, -axes[3]))]

    def rumble(self, index, left=0, right=0):
        if self.library and self.devices.get(index):
            self.library.SDL_GameControllerRumble(self.devices[index], left, right, 200)

    def close(self):
        if self.library:
            for device in self.devices.values():
                if device:
                    self.library.SDL_GameControllerClose(device)
            self.devices.clear()
            self.library.SDL_QuitSubSystem(0x2000 | 0x200)


class UInputPads:
    def __init__(self):
        self.devices = {}

    def update(self, index, values):
        from evdev import AbsInfo, UInput, ecodes as e
        button_masks = [(0x1000, e.BTN_SOUTH), (0x2000, e.BTN_EAST), (0x4000, e.BTN_WEST),
                        (0x8000, e.BTN_NORTH), (0x100, e.BTN_TL), (0x200, e.BTN_TR),
                        (0x10, e.BTN_START), (0x20, e.BTN_SELECT),
                        (0x40, e.BTN_THUMBL), (0x80, e.BTN_THUMBR)]
        axis_values = [(e.ABS_Z, values[1]), (e.ABS_RZ, values[2]),
                       (e.ABS_X, values[3]), (e.ABS_Y, max(-32768, min(32767, -values[4]))),
                       (e.ABS_RX, values[5]), (e.ABS_RY, max(-32768, min(32767, -values[6]))),
                       (e.ABS_HAT0X, int(bool(values[0] & 8)) - int(bool(values[0] & 4))),
                       (e.ABS_HAT0Y, int(bool(values[0] & 2)) - int(bool(values[0] & 1)))]
        if index not in self.devices:
            ranges = [(0, 255)] * 2 + [(-32768, 32767)] * 4 + [(-1, 1)] * 2
            axes = [(code, AbsInfo(0, low, high, 0, 0, 0)) for (code, _), (low, high) in zip(axis_values, ranges)]
            try:
                self.devices[index] = UInput({e.EV_KEY: [b for _, b in button_masks], e.EV_ABS: axes},
                                              name=f'Elink Gamepad {index + 1}', vendor=0x045e,
                                              product=0x028e, bustype=e.BUS_USB)
            except OSError as exc:
                raise RuntimeError('Cannot open /dev/uinput; grant the current user uinput access before enabling gamepads') from exc
        device = self.devices[index]
        for mask, button in button_masks:
            device.write(e.EV_KEY, button, int(bool(values[0] & mask)))
        for axis, value in axis_values:
            device.write(e.EV_ABS, axis, value)
        device.syn()

    def close(self):
        for device in self.devices.values():
            device.close()
        self.devices.clear()
