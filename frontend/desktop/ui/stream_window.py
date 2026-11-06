"""独立全屏串流窗口。

参考 ``E:\\elink`` 的远控播放器：串流画面占满屏幕，顶部悬浮一条控制栏；
鼠标移动时显示，静止 3 秒后自动隐藏并隐藏光标。Esc / F11 退出全屏，
F10 切换统计，F8 释放输入。这个窗口只负责显示与快捷操作，会话仍由主窗口管理。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .preview import PreviewWidget
from .widgets import StatusPill, label


class StreamWindow(QWidget):
    closed = Signal()
    release_requested = Signal()
    disconnect_requested = Signal()
    stats_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(None)
        self.setWindowTitle("elink · 全屏串流")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setObjectName("streamWindow")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.preview = PreviewWidget(self)
        self.preview.set_edge_padding(0)
        self.preview.setMouseTracking(True)
        self.preview.installEventFilter(self)
        root.addWidget(self.preview, 1)

        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("streamToolbar")
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(14, 8, 10, 8)
        bar.setSpacing(8)
        self.title = label("elink  /  远端画面", "streamTitle")
        bar.addWidget(self.title)
        self.pill = StatusPill("运行中", "ok")
        bar.addWidget(self.pill)
        bar.addStretch(1)
        self.stats_button = QPushButton("统计")
        self.stats_button.setObjectName("streamButton")
        self.stats_button.setCheckable(True)
        self.stats_button.setToolTip("F10")
        self.stats_button.toggled.connect(self.stats_toggled.emit)
        bar.addWidget(self.stats_button)
        release = QPushButton("释放输入")
        release.setObjectName("streamButton")
        release.setToolTip("F8")
        release.clicked.connect(self.release_requested.emit)
        bar.addWidget(release)
        disconnect = QPushButton("断开")
        disconnect.setObjectName("streamButtonDanger")
        disconnect.clicked.connect(self.disconnect_requested.emit)
        bar.addWidget(disconnect)
        exit_button = QPushButton("退出全屏  F11")
        exit_button.setObjectName("streamButton")
        exit_button.clicked.connect(self.close)
        bar.addWidget(exit_button)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(3000)
        self._hide_timer.timeout.connect(self._hide_toolbar)

    # ---- 数据 ----
    def set_frame(self, frame) -> None:
        self.preview.set_frame(frame)

    def set_stats_lines(self, lines: list[str]) -> None:
        self.preview.set_stats_lines(lines)

    def set_stats_visible(self, visible: bool) -> None:
        self.preview.set_stats_visible(visible)
        self.stats_button.blockSignals(True)
        self.stats_button.setChecked(visible)
        self.stats_button.blockSignals(False)

    def set_status(self, text: str, tone: str = "ok") -> None:
        self.pill.set(text, tone)

    # ---- 窗口 ----
    def show_fullscreen(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())
        self.showFullScreen()
        self.setFocus()
        self._show_toolbar()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        hint = self.toolbar.sizeHint()
        width = min(max(560, hint.width()), max(560, self.width() - 32))
        self.toolbar.setGeometry((self.width() - width) // 2, 16, width, hint.height())

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._hide_timer.stop()
        self.closed.emit()
        super().closeEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_F11):
            self.close()
            event.accept()
            return
        if event.key() == Qt.Key.Key_F10:
            self.stats_button.toggle()
            event.accept()
            return
        if event.key() == Qt.Key.Key_F8:
            self.release_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if watched is self.preview and event.type() in (QEvent.Type.MouseMove, QEvent.Type.Enter):
            self._show_toolbar()
        return super().eventFilter(watched, event)

    def _show_toolbar(self) -> None:
        self.toolbar.setVisible(True)
        self.toolbar.raise_()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._hide_timer.start()

    def _hide_toolbar(self) -> None:
        if self.isFullScreen():
            self.toolbar.hide()
            self.setCursor(Qt.CursorShape.BlankCursor)
