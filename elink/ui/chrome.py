"""Compact Qt window chrome using Windows' system move/resize operations."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget
from .icons import icon, icon_label


TITLE_HEIGHT = 40
MAIN_TITLE_HEIGHT = 50
RESIZE_MARGIN = 5


def window_icon(kind):
    return icon({'minimize': 'minus', 'maximize': 'square', 'restore': 'copy'}.get(kind, 'x'))


class MainTitleBar(QFrame):
    def __init__(self, window):
        super().__init__(window)
        self.owner = window
        self.setObjectName('mainTitleBar')
        self.setFixedHeight(MAIN_TITLE_HEIGHT)
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(icon_label('link', 20, '#2855eb'))
        title = QLabel('Elink')
        title.setObjectName('mainWindowTitle')
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(title)
        row.addStretch()
        for name, kind, callback in (
                ('最小化', 'minimize', window.showMinimized),
                ('最大化', 'maximize', self.toggle_maximized),
                ('关闭', 'close', window.close)):
            control = QPushButton()
            control.setObjectName('mainClose' if kind == 'close' else 'mainCaptionButton')
            control.setToolTip(name)
            control.setAccessibleName(name)
            control.setIcon(window_icon(kind))
            control.setIconSize(QSize(16, 16))
            control.setFixedSize(48, MAIN_TITLE_HEIGHT)
            control.clicked.connect(callback)
            row.addWidget(control)
            setattr(self, 'close_button' if kind == 'close' else kind, control)
        row.setSpacing(0)
        self.update_state()

    def update_state(self):
        maximized = self.owner.isMaximized()
        self.maximize.setIcon(window_icon('restore' if maximized else 'maximize'))
        name = '还原' if maximized else '最大化'
        self.maximize.setAccessibleName(name)
        self.maximize.setToolTip(name)

    def toggle_maximized(self):
        if self.owner.isMaximized():
            self.owner.showNormal()
        else:
            self.owner.showMaximized()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.owner.windowHandle():
            self.owner.windowHandle().startSystemMove()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximized()
        event.accept()


class StreamTitleBar(QFrame):
    def __init__(self, player):
        super().__init__(player)
        self.player = player
        self.setObjectName('streamToolbar')
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(icon_label('link', 18, '#2855eb'))
        self.title = QLabel('Elink  /  远程桌面')
        self.title.setObjectName('streamTitle')
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.title)
        row.addStretch()
        self.controls = QPushButton('控制中心  F8')
        self.controls.setObjectName('titleControl')
        self.controls.setIcon(icon('sliders-horizontal'))
        self.controls.setIconSize(QSize(16, 16))
        self.controls.setToolTip('打开控制中心并释放远端输入')
        self.controls.clicked.connect(player.open_controls)
        row.addWidget(self.controls)
        self.minimize = self.window_button('最小化', 'minimize', player.minimize_window)
        self.maximize = self.window_button('最大化', 'maximize', player.toggle_maximized)
        self.close_button = self.window_button('关闭远控', 'close', player.close)
        self.close_button.setObjectName('titleClose')
        window_buttons = QHBoxLayout()
        window_buttons.setSpacing(0)
        for button in (self.minimize, self.maximize, self.close_button):
            window_buttons.addWidget(button)
        row.addLayout(window_buttons)

    def window_button(self, label, icon, callback):
        button = QPushButton()
        button.setObjectName('titleButton')
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setIcon(window_icon(icon))
        button.setIconSize(QSize(15, 15))
        button.setFixedSize(46, TITLE_HEIGHT)
        button.clicked.connect(callback)
        return button

    def update_state(self):
        restore = self.player.isMaximized() or self.player.isFullScreen()
        label = '退出全屏' if self.player.isFullScreen() else '还原' if restore else '最大化'
        self.maximize.setIcon(window_icon('restore' if restore else 'maximize'))
        self.maximize.setAccessibleName(label)
        self.maximize.setToolTip(label)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.player.release()
            if not self.player.isFullScreen() and self.player.windowHandle():
                self.player.windowHandle().startSystemMove()
        event.accept()  # Window chrome must never become a remote mouse click.

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.player.toggle_maximized()
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()


class ResizeHandle(QWidget):
    def __init__(self, player, edges):
        super().__init__(player)
        self.player, self.edges = player, edges
        self.setObjectName('windowResizeHandle')
        left, right, top, bottom = (Qt.Edge.LeftEdge, Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.Edge.BottomEdge)
        diagonal = edges in (left | top, right | bottom)
        if edges in (left, right):
            cursor = Qt.CursorShape.SizeHorCursor
        elif edges in (top, bottom):
            cursor = Qt.CursorShape.SizeVerCursor
        else:
            cursor = Qt.CursorShape.SizeFDiagCursor if diagonal else Qt.CursorShape.SizeBDiagCursor
        self.setCursor(cursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.player.windowHandle():
            release = getattr(self.player, 'release', None)
            if release:
                release()
            self.player.windowHandle().startSystemResize(self.edges)
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()


def make_resize_handles(player):
    left, right, top, bottom = (Qt.Edge.LeftEdge, Qt.Edge.RightEdge, Qt.Edge.TopEdge, Qt.Edge.BottomEdge)
    return [ResizeHandle(player, edges) for edges in
            (top, bottom, left, right, left | top, right | top, left | bottom, right | bottom)]


def place_resize_handles(player):
    m, w, h = RESIZE_MARGIN, player.width(), player.height()
    rectangles = [(m, 0, w - 2*m, m), (m, h-m, w-2*m, m), (0, m, m, h-2*m),
                  (w-m, m, m, h-2*m), (0, 0, m, m), (w-m, 0, m, m), (0, h-m, m, m), (w-m, h-m, m, m)]
    visible = not getattr(player, 'captured', False) and not player.isFullScreen() and not player.isMaximized()
    for handle, rect in zip(player.resize_handles, rectangles):
        handle.setGeometry(*rect)
        handle.setVisible(visible)
        handle.raise_()
