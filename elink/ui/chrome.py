"""Compact Qt window chrome using Windows' system move/resize operations."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QWidget


TITLE_HEIGHT = 40
RESIZE_MARGIN = 5


def window_icon(kind):
    pixmap = QPixmap(32, 32)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setPen(QPen(QColor('#182743'), 1))
    if kind == 'minimize':
        painter.drawLine(3, 8, 13, 8)
    elif kind == 'maximize':
        painter.drawRect(3, 3, 10, 10)
    elif kind == 'restore':
        painter.drawRect(5, 3, 8, 8)
        painter.fillRect(3, 5, 8, 8, QColor('#edf2fa'))
        painter.drawRect(3, 5, 8, 8)
    else:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.drawLine(3, 3, 13, 13)
        painter.drawLine(3, 13, 13, 3)
    painter.end()
    return QIcon(pixmap)


class StreamTitleBar(QFrame):
    def __init__(self, player):
        super().__init__(player)
        self.player = player
        self.setObjectName('streamToolbar')
        row = QHBoxLayout(self)
        row.setContentsMargins(14, 0, 0, 0)
        row.setSpacing(0)
        self.title = QLabel('ELINK  /  远程桌面')
        self.title.setObjectName('streamTitle')
        self.title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.title)
        row.addStretch()
        self.controls = QPushButton('控制中心  F8  ▾')
        self.controls.setObjectName('titleControl')
        self.controls.setToolTip('打开控制中心并释放远端输入')
        self.controls.clicked.connect(player.open_controls)
        row.addWidget(self.controls)
        self.minimize = self.window_button('最小化', 'minimize', player.minimize_window)
        self.maximize = self.window_button('最大化', 'maximize', player.toggle_maximized)
        self.close_button = self.window_button('关闭远控', 'close', player.close)
        self.close_button.setObjectName('titleClose')
        for button in (self.minimize, self.maximize, self.close_button):
            row.addWidget(button)

    def window_button(self, label, icon, callback):
        button = QPushButton()
        button.setObjectName('titleButton')
        button.setToolTip(label)
        button.setAccessibleName(label)
        button.setIcon(window_icon(icon))
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
            self.player.release()
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
    visible = not player.captured and not player.isFullScreen() and not player.isMaximized()
    for handle, rect in zip(player.resize_handles, rectangles):
        handle.setGeometry(*rect)
        handle.setVisible(visible)
        handle.raise_()
