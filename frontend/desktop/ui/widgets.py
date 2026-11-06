"""界面基础组件。

这里只做「外观 + 交互外壳」：卡片、状态胶囊、开关、分段选择、指标块、
可点击磁贴、自定义标题栏等。颜色与字体全部走 ``theme.py`` 的 QSS，
所以换配色只需要改主题，不用改页面。
"""

from __future__ import annotations

import html

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from ..theme import ACCENT, CYAN

TONES: dict[str, tuple[str, str]] = {
    "blue": ("#E8F0FF", "#2B6BFF"),
    "green": ("#E8F8ED", "#00A32A"),
    "orange": ("#FFF3E6", "#E07000"),
    "purple": ("#F1EDFF", "#6E4BFF"),
    "cyan": ("#E6F8FC", "#0EA5C6"),
    "red": ("#FFECEC", "#D92D2D"),
    "slate": ("#EEF2F8", "#4E5969"),
}


# --------------------------------------------------------------------------
# 文本工厂
# --------------------------------------------------------------------------
def label(text: str, name: str = "", *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def title(text: str) -> QLabel:
    return label(text, "pageTitle")


def section(text: str) -> QLabel:
    return label(text, "sectionTitle")


def card_title(text: str) -> QLabel:
    return label(text, "cardTitle")


def muted(text: str = "") -> QLabel:
    return label(text, "muted")


def caption(text: str = "") -> QLabel:
    return label(text, "caption")


def hint(text: str = "") -> QLabel:
    return label(text, "hint")


class ElidedLabel(QLabel):
    """单行说明文字：宽度不够时自动显示省略号，避免在紧凑窗口里被裁掉。"""

    def __init__(self, text: str = "", name: str = "hint", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._full_text = text
        if name:
            self.setObjectName(name)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        available = max(0, self.width() - 2)
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, available
        )
        if elided != self.text():
            self.setText(elided)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setText(text)


def divider(*, vertical: bool = False, length: int = 32) -> QFrame:
    line = QFrame()
    line.setObjectName("divider")
    if vertical:
        line.setFixedWidth(1)
        line.setFixedHeight(length)
        line.setProperty("orientation", "v")
        line.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    else:
        line.setFixedHeight(1)
        line.setProperty("orientation", "h")
        line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return line


# --------------------------------------------------------------------------
# 卡片
# --------------------------------------------------------------------------
class Card(QFrame):
    """白底圆角卡片，可带标题行与右上角操作区。"""

    def __init__(
        self,
        title_text: str = "",
        subtitle: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(18, 16, 18, 18)
        self._layout.setSpacing(12)
        self.header_layout: QHBoxLayout | None = None
        self.header: QWidget | None = None

        if title_text:
            header = QWidget(self)
            header_layout = QHBoxLayout(header)
            header_layout.setContentsMargins(0, 0, 0, 2)
            header_layout.setSpacing(8)
            text_box = QVBoxLayout()
            text_box.setSpacing(2)
            text_box.addWidget(card_title(title_text))
            if subtitle:
                text_box.addWidget(hint(subtitle))
            header_layout.addLayout(text_box)
            header_layout.addStretch(1)
            self._layout.addWidget(header)
            self.header = header
            self.header_layout = header_layout

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> None:
        self._layout.addWidget(widget, stretch)

    def add_layout(self, layout) -> None:
        self._layout.addLayout(layout)

    def add_header_action(self, widget: QWidget) -> None:
        if self.header_layout is None:
            return
        widget.setParent(self.header)
        self.header_layout.addWidget(widget)


# --------------------------------------------------------------------------
# 状态
# --------------------------------------------------------------------------
class StatusPill(QLabel):
    """小圆角状态胶囊：``set("运行中", "ok")``。"""

    def __init__(self, text: str = "空闲", tone: str = "idle", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("pill")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set(text, tone)

    def set(self, text: str, tone: str = "idle") -> None:
        if tone == "ok":
            # UU 远程的「在线」胶囊：黑底、白字、绿色圆点
            self.setTextFormat(Qt.TextFormat.RichText)
            self.setText(f'<span style="color:#00B42A;">●</span>&nbsp;{html.escape(text)}')
        else:
            self.setTextFormat(Qt.TextFormat.PlainText)
            dot = {"busy": "● ", "error": "● "}.get(tone, "")
            self.setText(f"{dot}{text}")
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)


class StatusDot(QLabel):
    def __init__(self, tone: str = "idle", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statusDot")
        self.setFixedSize(8, 8)
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        self.setProperty("tone", tone)
        self.style().unpolish(self)
        self.style().polish(self)


# --------------------------------------------------------------------------
# 开关 / 分段选择 / 输入
# --------------------------------------------------------------------------
class ToggleSwitch(QAbstractButton):
    """iOS 风格开关（绿色=开启），带 140ms 过渡。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(40, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._offset = 0.0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = float(value)
        self.update()

    offset = Property(float, _get_offset, _set_offset)  # type: ignore[assignment]

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def setChecked(self, checked: bool) -> None:  # noqa: N802 - Qt 命名
        super().setChecked(checked)
        self._anim.stop()
        self._offset = 1.0 if checked else 0.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        off = QColor("#00B42A")
        on = QColor("#D5DEE9")
        color = QColor(
            int(on.red() + (off.red() - on.red()) * self._offset),
            int(on.green() + (off.green() - on.green()) * self._offset),
            int(on.blue() + (off.blue() - on.blue()) * self._offset),
        )
        if not self.isEnabled():
            color = QColor("#E4E9F0")
        track = QRectF(0, 0, 40, 22)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(track, 11, 11)

        knob_x = 3.0 + self._offset * 18.0
        knob = QRectF(knob_x, 3, 16, 16)
        painter.setBrush(QColor("#FFFFFF") if self.isEnabled() else QColor("#F6F8FB"))
        painter.drawEllipse(knob)


class SegmentedControl(QFrame):
    """胶囊分段选择器，接口对齐 QComboBox 的常用子集。"""

    currentIndexChanged = Signal(int)

    def __init__(self, items: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("segmented")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(3, 3, 3, 3)
        self._layout.setSpacing(2)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QPushButton] = []
        for index, text in enumerate(items):
            button = QPushButton(text)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._group.addButton(button, index)
            self._layout.addWidget(button)
            self._buttons.append(button)
        self._group.idClicked.connect(self._on_clicked)
        if items:
            self._buttons[0].setChecked(True)

    def _on_clicked(self, index: int) -> None:
        self.currentIndexChanged.emit(index)

    def currentIndex(self) -> int:  # noqa: N802 - Qt 命名
        return self._group.checkedId()

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt 命名
        if 0 <= index < len(self._buttons):
            self._buttons[index].setChecked(True)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt 命名
        super().setEnabled(enabled)
        for button in self._buttons:
            button.setEnabled(enabled)


class FormField(QWidget):
    """竖排表单：上方小号说明，下方控件（UU 远程常见的字段样式）。"""

    def __init__(self, caption_text: str, control: QWidget, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(hint(caption_text))
        layout.addWidget(control)
        self.control = control

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 - Qt 命名
        super().setEnabled(enabled)
        self.control.setEnabled(enabled)


# --------------------------------------------------------------------------
# 指标
# --------------------------------------------------------------------------
class MetricTile(QFrame):
    def __init__(self, caption_text: str, value: str = "—", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("metricTile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(3)
        layout.addWidget(hint(caption_text))
        self.value = label(value, "mono")
        font = QFont("Cascadia Mono", 14)
        font.setBold(True)
        self.value.setFont(font)
        self.value.setStyleSheet("color: #1D2129; font-size: 16px;")
        layout.addWidget(self.value)

    def set(self, value: str) -> None:
        self.value.setText(value)


class MetricStrip(QFrame):
    """一行指标条：``MetricStrip([("fps","编码帧率"), ...])``。"""

    def __init__(self, rows: list[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("metricStrip")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(14)
        self._values: dict[str, QLabel] = {}
        for index, (key, caption_text) in enumerate(rows):
            if index:
                layout.addWidget(divider(vertical=True))
            box = QVBoxLayout()
            box.setSpacing(2)
            box.addWidget(hint(caption_text))
            value = label("—", "mono")
            value.setStyleSheet("color: #1D2129; font-size: 15px; font-weight: 600;")
            box.addWidget(value)
            holder = QWidget()
            holder.setLayout(box)
            layout.addWidget(holder, 1)
            self._values[key] = value

    def set(self, key: str, value: str) -> None:
        item = self._values.get(key)
        if item is not None:
            item.setText(value)


class MetricsGrid(QWidget):
    """2 列指标网格。"""

    def __init__(self, rows: list[tuple[str, str]], columns: int = 2, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        self._tiles: dict[str, MetricTile] = {}
        for index, (key, caption_text) in enumerate(rows):
            tile = MetricTile(caption_text)
            self._tiles[key] = tile
            grid.addWidget(tile, index // columns, index % columns)
        for column in range(columns):
            grid.setColumnStretch(column, 1)

    def set(self, key: str, value: str) -> None:
        tile = self._tiles.get(key)
        if tile is not None:
            tile.set(value)


# --------------------------------------------------------------------------
# 可点击磁贴
# --------------------------------------------------------------------------
class ClickableCard(QFrame):
    """整块可点击的卡片（鼠标悬停 / 键盘回车都触发）。"""

    clicked = Signal()

    def __init__(self, object_name: str = "quickTile", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._hover = False

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._hover = True
        self._sync_hover()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._hover = False
        self._sync_hover()
        super().leaveEvent(event)

    def _sync_hover(self) -> None:
        self.setProperty("hover", self._hover)
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ActionTile(ClickableCard):
    """英雄区下方的一格操作（图标 + 文案）。"""

    def __init__(self, icon_name: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__("actionTile", parent)
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 0, 12, 0)
        layout.setSpacing(8)
        layout.addWidget(icons.icon_label(icon_name, 18, "#4E5969"))
        layout.addWidget(label(text, "muted"))
        layout.addStretch(1)


class QuickTile(ClickableCard):
    """快速开始磁贴：彩色图标 + 标题 + 说明 + 右箭头。"""

    def __init__(
        self,
        icon_name: str,
        title_text: str,
        subtitle: str,
        tone: str = "blue",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("quickTile", parent)
        self.setFixedHeight(72)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(12)
        layout.addWidget(IconTile(icon_name, tone, 38))
        text_box = QVBoxLayout()
        text_box.setSpacing(3)
        text_box.addWidget(label(title_text, "cardTitle"))
        text_box.addWidget(ElidedLabel(subtitle))
        layout.addLayout(text_box, 1)
        layout.addWidget(icons.icon_label("chevron-right", 14, "#A9B4C4"))


class IconTile(QWidget):
    """圆角彩色图标块。"""

    def __init__(
        self,
        icon_name: str,
        tone: str = "blue",
        size: int = 36,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._icon = icon_name
        self._tone = tone
        self._size = size
        self.setFixedSize(size, size)

    def set_icon(self, icon_name: str, tone: str | None = None) -> None:
        self._icon = icon_name
        if tone:
            self._tone = tone
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        background, foreground = TONES.get(self._tone, TONES["blue"])
        radius = max(6.0, self._size * 0.28)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(QRectF(0, 0, self._size, self._size), radius, radius)
        glyph = int(self._size * 0.5)
        pixmap = icons.icon(self._icon, foreground).pixmap(glyph, glyph)
        if not pixmap.isNull():
            painter.drawPixmap(
                int((self._size - glyph) / 2),
                int((self._size - glyph) / 2),
                pixmap,
            )


# --------------------------------------------------------------------------
# 品牌 / 标题栏 / 英雄区
# --------------------------------------------------------------------------
class LogoMark(QWidget):
    """蓝→青渐变圆角标，中间是白色链路图标。"""

    def __init__(self, size: int = 26, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        gradient = QLinearGradient(0, 0, self._size, self._size)
        gradient.setColorAt(0.0, QColor(ACCENT))
        gradient.setColorAt(1.0, QColor(CYAN))
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self._size, self._size), 8, 8)
        painter.fillPath(path, gradient)
        glyph = int(self._size * 0.56)
        pixmap = icons.icon("link", "#ffffff").pixmap(glyph, glyph)
        if not pixmap.isNull():
            painter.drawPixmap(
                int((self._size - glyph) / 2),
                int((self._size - glyph) / 2),
                pixmap,
            )


class TitleBar(QFrame):
    """自绘标题栏：品牌区 + 全局操作 + 窗口按钮。"""

    refresh_clicked = Signal()
    minimize_clicked = Signal()
    maximize_clicked = Signal()
    close_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("titleBar")
        self.setFixedHeight(46)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 6, 0)
        layout.setSpacing(10)
        layout.addWidget(LogoMark(24))
        brand = label("elink", "brandTitle")
        brand.setStyleSheet("font-size: 16px;")
        layout.addWidget(brand)
        line = divider(vertical=True)
        line.setFixedHeight(18)
        layout.addWidget(line)
        layout.addWidget(label("远程控制", "brandSub"))
        layout.addStretch(1)

        self.status_hint = label("空闲", "caption")
        layout.addWidget(self.status_hint)

        self.refresh_button = self._icon_button("refresh-cw", "刷新设备列表")
        self.refresh_button.clicked.connect(self.refresh_clicked.emit)
        layout.addWidget(self.refresh_button)
        layout.addSpacing(4)

        self.min_button = self._window_button("minus", "最小化")
        self.max_button = self._window_button("maximize", "最大化")
        self.close_button = self._window_button("x", "关闭", close=True)
        self.min_button.clicked.connect(self.minimize_clicked.emit)
        self.max_button.clicked.connect(self.maximize_clicked.emit)
        self.close_button.clicked.connect(self.close_clicked.emit)
        for button in (self.min_button, self.max_button, self.close_button):
            layout.addWidget(button)

    def _icon_button(self, name: str, tip: str) -> QPushButton:
        button = QPushButton()
        button.setObjectName("iconButton")
        button.setIcon(icons.icon(name, "#4E5969"))
        button.setIconSize(QSize(16, 16))
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _window_button(self, name: str, tip: str, *, close: bool = False) -> QPushButton:
        button = QPushButton()
        button.setObjectName("windowClose" if close else "windowButton")
        button.setIcon(icons.icon(name, "#4E5969"))
        button.setIconSize(QSize(14, 14))
        button.setToolTip(tip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def set_maximized(self, maximized: bool) -> None:
        self.max_button.setIcon(icons.icon("square" if maximized else "maximize", "#4E5969"))
        self.max_button.setToolTip("还原" if maximized else "最大化")

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.maximize_clicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class HeroArt(QWidget):
    """首页英雄区背景：浅蓝渐变 + 柔光 + 右侧桌面示意图。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(204)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())

        gradient = QLinearGradient(rect.topLeft(), QPointF(rect.right(), rect.bottom()))
        gradient.setColorAt(0.0, QColor("#F6FAFF"))
        gradient.setColorAt(0.55, QColor("#E7F1FF"))
        gradient.setColorAt(1.0, QColor("#D9E9FF"))
        painter.fillRect(rect, gradient)

        glow = QRadialGradient(QPointF(rect.width() * 0.08, rect.height() * 0.12), rect.width() * 0.45)
        glow.setColorAt(0.0, QColor(255, 255, 255, 190))
        glow.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.fillRect(rect, glow)

        cyan = QRadialGradient(
            QPointF(rect.width() * 0.72, rect.height() * 0.92), rect.width() * 0.35
        )
        cyan.setColorAt(0.0, QColor(34, 195, 232, 46))
        cyan.setColorAt(1.0, QColor(34, 195, 232, 0))
        painter.fillRect(rect, cyan)

        # 细网格，增加「技术感」但不抢文字
        painter.setPen(QPen(QColor(43, 107, 255, 16), 1))
        step = 28
        for x in range(0, int(rect.width()), step):
            painter.drawLine(x, 0, x, int(rect.height()))
        for y in range(0, int(rect.height()), step):
            painter.drawLine(0, y, int(rect.width()), y)

        self._draw_monitor(painter, rect)

    def _draw_monitor(self, painter: QPainter, rect: QRectF) -> None:
        width = min(rect.width() * 0.34, 340.0)
        height = width * 0.63
        x = rect.width() - width - 44.0
        y = (rect.height() - height) / 2 - 6.0
        if x < rect.width() * 0.5:
            return

        shadow = QRectF(x + 2, y + 10, width, height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(70, 110, 160, 26))
        painter.drawRoundedRect(shadow, 14, 14)

        body = QRectF(x, y, width, height)
        painter.setBrush(QColor(255, 255, 255, 238))
        painter.setPen(QPen(QColor("#CFE0F5"), 1))
        painter.drawRoundedRect(body, 13, 13)

        screen = body.adjusted(9, 9, -9, -9)
        screen_gradient = QLinearGradient(screen.topLeft(), screen.bottomRight())
        screen_gradient.setColorAt(0.0, QColor("#0C1A33"))
        screen_gradient.setColorAt(1.0, QColor("#173B66"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(screen_gradient)
        painter.drawRoundedRect(screen, 9, 9)

        bloom = QRadialGradient(
            QPointF(screen.left() + screen.width() * 0.74, screen.top() + screen.height() * 0.22),
            screen.width() * 0.45,
        )
        bloom.setColorAt(0.0, QColor(34, 195, 232, 90))
        bloom.setColorAt(1.0, QColor(34, 195, 232, 0))
        painter.setBrush(bloom)
        painter.drawRoundedRect(screen, 9, 9)

        # 窗口线条
        painter.setBrush(QColor(255, 255, 255, 26))
        top = screen.top() + 14
        painter.drawRoundedRect(QRectF(screen.left() + 14, top, screen.width() * 0.52, 9), 4, 4)
        painter.setBrush(QColor(255, 255, 255, 16))
        for index in range(4):
            painter.drawRoundedRect(
                QRectF(screen.left() + 14, top + 22 + index * 13, screen.width() * (0.66 - index * 0.07), 6),
                3,
                3,
            )
        painter.setBrush(QColor(34, 195, 232, 70))
        painter.drawRoundedRect(
            QRectF(screen.left() + screen.width() * 0.58, top + 26, screen.width() * 0.3, screen.height() * 0.42),
            8,
            8,
        )

        # 底座
        stand = QRectF(x + width / 2 - 5, y + height, 10, 16)
        painter.setBrush(QColor("#E2ECF8"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(stand, 2, 2)
        base = QRectF(x + width / 2 - 34, y + height + 14, 68, 6)
        painter.setBrush(QColor("#D5E4F6"))
        painter.drawRoundedRect(base, 3, 3)


class EmptyState(QWidget):
    def __init__(self, icon_name: str, title_text: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 28, 0, 28)
        layout.setSpacing(12)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = IconTile(icon_name, "slate", 48)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(label(title_text, "sectionTitle"), 0, Qt.AlignmentFlag.AlignHCenter)
        if subtitle:
            layout.addWidget(label(subtitle, "muted"), 0, Qt.AlignmentFlag.AlignHCenter)


class NoticeBar(QFrame):
    """可关闭 / 可复制的错误或警告条（参考 E:\\elink 的顶部提示条）。"""

    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("noticeBar")
        self.setProperty("tone", "warn")
        self.setVisible(False)
        self._message = ""
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 7, 8, 7)
        row.setSpacing(8)
        self.text = label("", "noticeText")
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.text, 1)
        self.copy_button = QPushButton()
        self.copy_button.setObjectName("iconButton")
        self.copy_button.setIcon(icons.icon("copy", "#8A4B00"))
        self.copy_button.setIconSize(QSize(15, 15))
        self.copy_button.setToolTip("复制这条提示")
        self.copy_button.clicked.connect(self._copy)
        row.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.close_button = QPushButton()
        self.close_button.setObjectName("iconButton")
        self.close_button.setIcon(icons.icon("x", "#8A4B00"))
        self.close_button.setIconSize(QSize(14, 14))
        self.close_button.setToolTip("关闭提示")
        self.close_button.clicked.connect(self.dismiss)
        row.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)

    def show_message(self, message: str, level: str = "warn") -> None:
        self._message = message
        prefix = {"error": "错误", "warn": "警告"}.get(level, "")
        self.text.setText(f"{prefix}：{message}" if prefix else message)
        self.text.setToolTip(message)
        self.setProperty("tone", {"error": "error", "warn": "warn"}.get(level, "info"))
        self.style().unpolish(self)
        self.style().polish(self)
        self.setVisible(True)

    def dismiss(self) -> None:
        self.setVisible(False)
        self.dismissed.emit()

    def _copy(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._message)



