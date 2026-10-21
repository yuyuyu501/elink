from PySide6.QtCore import Qt


def remote_key(event, down):
    key = int(event.key())
    special = {
        Qt.Key.Key_Backspace: 8, Qt.Key.Key_Tab: 9, Qt.Key.Key_Return: 13,
        Qt.Key.Key_Enter: 13, Qt.Key.Key_Shift: 16, Qt.Key.Key_Control: 17,
        Qt.Key.Key_Alt: 18, Qt.Key.Key_Meta: 91, Qt.Key.Key_CapsLock: 20,
        Qt.Key.Key_Escape: 27, Qt.Key.Key_PageUp: 33, Qt.Key.Key_PageDown: 34,
        Qt.Key.Key_End: 35, Qt.Key.Key_Home: 36, Qt.Key.Key_Insert: 45,
        Qt.Key.Key_Delete: 46, Qt.Key.Key_NumLock: 144, Qt.Key.Key_ScrollLock: 145,
        Qt.Key.Key_Semicolon: 186, Qt.Key.Key_Equal: 187, Qt.Key.Key_Comma: 188,
        Qt.Key.Key_Minus: 189, Qt.Key.Key_Period: 190, Qt.Key.Key_Slash: 191,
        Qt.Key.Key_QuoteLeft: 192, Qt.Key.Key_BracketLeft: 219,
        Qt.Key.Key_Backslash: 220, Qt.Key.Key_BracketRight: 221, Qt.Key.Key_Apostrophe: 222,
    }
    keypad = bool(event.modifiers() & Qt.KeyboardModifier.KeypadModifier)
    if keypad and 48 <= key <= 57:
        vk = 96 + key - 48
    elif int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
        vk = 112 + key - int(Qt.Key.Key_F1)
    else:
        vk = special.get(key, key if 32 <= key <= 90 else 0)
    return dict(type='key', vk=vk, scan=0, extended=key == Qt.Key.Key_Enter, down=down)
