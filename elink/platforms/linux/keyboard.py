"""Semantic VK compatibility with the existing v2 Windows input protocol."""
VK_KEYSYMS = {
    8: 'BackSpace', 9: 'Tab', 13: 'Return', 16: 'Shift_L', 17: 'Control_L',
    18: 'Alt_L', 19: 'Pause', 20: 'Caps_Lock', 27: 'Escape', 32: 'space',
    33: 'Prior', 34: 'Next', 35: 'End', 36: 'Home', 37: 'Left', 38: 'Up',
    39: 'Right', 40: 'Down', 44: 'Print', 45: 'Insert', 46: 'Delete',
    91: 'Super_L', 92: 'Super_R', 93: 'Menu', 106: 'KP_Multiply',
    107: 'KP_Add', 109: 'KP_Subtract', 110: 'KP_Decimal', 111: 'KP_Divide',
    144: 'Num_Lock', 145: 'Scroll_Lock', 160: 'Shift_L', 161: 'Shift_R',
    162: 'Control_L', 163: 'Control_R', 164: 'Alt_L', 165: 'Alt_R',
    186: 'semicolon', 187: 'equal', 188: 'comma', 189: 'minus',
    190: 'period', 191: 'slash', 192: 'grave', 219: 'bracketleft',
    220: 'backslash', 221: 'bracketright', 222: 'apostrophe',
    **{v: chr(v).lower() for v in range(48, 58)},
    **{v: chr(v).lower() for v in range(65, 91)},
    **{96 + n: f'KP_{n}' for n in range(10)},
    **{112 + n: f'F{n + 1}' for n in range(24)},
}


def keysym_name(event):
    if event['vk'] == 13 and event.get('extended'):
        return 'KP_Enter'
    return VK_KEYSYMS.get(event['vk'])
