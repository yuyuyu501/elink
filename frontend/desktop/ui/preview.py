"""画面预览控件：把后端 ``Frame``（BGRA）画成等比缩放的视频区。

要点：

- ``Frame.data`` 是 BGRA，小端下正好对应 ``Format_ARGB32``，可以直接零转换构造
  ``QImage``；但必须 ``copy()``，否则 Qt 会继续引用那块（可能已被复用的）内存；
- 只在 resized/paint 时缩放，不做每帧双三次插值，30fps 下 CPU 占用可接受；
- 没有画面时画一个带柔光的占位，而不是纯黑，避免让人以为程序卡住；
- 左上角显示来源/状态，左下角显示分辨率与帧号。

这是纯绘制代码，不持有会话状态。
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import QFrame, QSizePolicy, QVBoxLayout, QWidget

from backend.media.frame import Frame

PLACEHOLDER_TITLE = "等待画面"
PLACEHOLDER_HINT = "正在等待远端画面"
HUD_MARGIN = 14


class PreviewWidget(QWidget):
    """等比缩放显示帧，并叠加状态与 HUD。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(QSize(320, 180))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._image: QImage | None = None
        self._info = ""
        self._badge = ""
        self._stats_lines: list[str] = []
        self._stats_visible = False
        self._edge_padding = 12
        self._frames = 0
        self._source_size = QSize(0, 0)

    # ---- 数据 ----
    def set_frame(self, frame: Frame) -> None:
        if frame.width <= 0 or frame.height <= 0:
            return
        payload = bytes(frame.data)
        image = QImage(
            payload,
            frame.width,
            frame.height,
            frame.linesize or frame.width * 4,
            QImage.Format.Format_ARGB32,
        )
        # QImage 不持有 payload 的所有权，必须深拷贝后再交给绘制线程
        self._image = image.copy()
        self._source_size = QSize(frame.width, frame.height)
        self._frames += 1
        self._info = f"{frame.width}×{frame.height} · 第 {self._frames} 帧"
        self.update()

    def clear(self) -> None:
        self._image = None
        self._info = ""
        self._frames = 0
        self._source_size = QSize(0, 0)
        self.update()

    def set_info(self, text: str) -> None:
        self._info = text
        self.update()

    def set_badge(self, text: str) -> None:
        self._badge = text
        self.update()

    def set_stats_lines(self, lines: list[str]) -> None:
        self._stats_lines = [str(line) for line in lines if line]
        self.update()

    def set_stats_visible(self, visible: bool) -> None:
        self._stats_visible = bool(visible)
        self.update()

    def set_edge_padding(self, padding: int) -> None:
        self._edge_padding = max(0, int(padding))
        self.update()

    @property
    def stats_visible(self) -> bool:
        return self._stats_visible

    @property
    def has_frame(self) -> bool:
        return self._image is not None

    @property
    def source_size(self) -> QSize:
        return self._source_size

    def save_snapshot(self, path: str) -> bool:
        """保存当前画面（截图按钮用）。"""
        if self._image is None:
            return False
        return bool(self._image.save(path, "PNG"))

    # ---- 绘制 ----
    def _target_rect(self) -> QRect:
        if self._image is None:
            return QRect()
        pad = self._edge_padding
        area = self.rect().adjusted(pad, pad, -pad, -pad)
        image_ratio = self._image.width() / max(1, self._image.height())
        area_ratio = area.width() / max(1, area.height())
        if image_ratio > area_ratio:
            width = area.width()
            height = int(width / image_ratio)
        else:
            height = area.height()
            width = int(height * image_ratio)
        left = area.left() + (area.width() - width) // 2
        top = area.top() + (area.height() - height) // 2
        return QRect(left, top, width, height)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor("#0B1220"))

        if self._image is None:
            self._paint_placeholder(painter)
            self._paint_badges(painter, self.rect().adjusted(6, 6, -6, -6), with_info=False)
            return

        target = self._target_rect()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(target), 8, 8)
        painter.save()
        painter.setClipPath(path)
        painter.drawImage(target, self._image)
        painter.restore()

        self._paint_badges(painter, target, with_info=True)
        if self._stats_visible and self._stats_lines:
            self._paint_stats(painter, target)

    def _paint_stats(self, painter: QPainter, target: QRect) -> None:
        font = QFont("Cascadia Mono")
        font.setPointSizeF(9)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        line_height = metrics.height() + 2
        text_width = max(metrics.horizontalAdvance(line) for line in self._stats_lines)
        width = text_width + 22
        height = line_height * len(self._stats_lines) + 12
        box = QRect(target.left() + HUD_MARGIN, target.top() + HUD_MARGIN, min(width, target.width() - 24), height)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(8, 13, 24, 205))
        painter.drawRoundedRect(box, 8, 8)
        painter.setPen(QPen(QColor(58, 78, 108, 160), 1))
        painter.drawRoundedRect(box, 8, 8)
        painter.setPen(QPen(QColor("#C7D5E8")))
        y = box.top() + 6
        for index, line in enumerate(self._stats_lines):
            painter.setPen(QPen(QColor("#7FD8FF") if index == 0 else QColor("#C7D5E8")))
            painter.drawText(
                QRect(box.left() + 10, y, box.width() - 20, line_height),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                line,
            )
            y += line_height

    def _paint_placeholder(self, painter: QPainter) -> None:
        rect = QRectF(self.rect())
        base = QLinearGradient(rect.topLeft(), rect.bottomRight())
        base.setColorAt(0.0, QColor("#0B1220"))
        base.setColorAt(1.0, QColor("#101B31"))
        painter.fillRect(rect, base)

        glow = QRadialGradient(
            QPointF(rect.width() * 0.28, rect.height() * 0.18), max(160.0, rect.width() * 0.42)
        )
        glow.setColorAt(0.0, QColor(34, 195, 232, 26))
        glow.setColorAt(1.0, QColor(34, 195, 232, 0))
        painter.fillRect(rect, glow)

        painter.setPen(QPen(QColor(255, 255, 255, 10), 1))
        step = 32
        for x in range(0, int(rect.width()), step):
            painter.drawLine(x, 0, x, int(rect.height()))
        for y in range(0, int(rect.height()), step):
            painter.drawLine(0, y, int(rect.width()), y)

        # 中心显示器轮廓
        center = rect.center()
        width = 88
        height = 58
        box = QRectF(center.x() - width / 2, center.y() - height / 2 - 20, width, height)
        painter.setPen(QPen(QColor("#3A4C69"), 1.4))
        painter.setBrush(QColor(255, 255, 255, 8))
        painter.drawRoundedRect(box, 8, 8)
        painter.drawLine(
            QPointF(box.center().x(), box.bottom()), QPointF(box.center().x(), box.bottom() + 12)
        )
        painter.drawLine(
            QPointF(box.center().x() - 20, box.bottom() + 12),
            QPointF(box.center().x() + 20, box.bottom() + 12),
        )

        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#D6E1F0")))
        title_rect = QRectF(rect.left(), box.bottom() + 30, rect.width(), 26)
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, PLACEHOLDER_TITLE)

        font.setPointSizeF(9)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#7387A3")))
        hint_rect = QRectF(rect.left(), box.bottom() + 56, rect.width(), 22)
        painter.drawText(hint_rect, Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter, PLACEHOLDER_HINT)

    def _paint_badges(self, painter: QPainter, target: QRect, *, with_info: bool = True) -> None:
        font = QFont("Microsoft YaHei UI")
        font.setPointSizeF(8.5)
        painter.setFont(font)
        metrics = painter.fontMetrics()

        if self._badge and not self._stats_visible:
            text_width = metrics.horizontalAdvance(self._badge)
            box = QRect(
                target.left() + HUD_MARGIN,
                target.top() + HUD_MARGIN,
                text_width + 26,
                24,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(11, 18, 32, 190))
            painter.drawRoundedRect(box, 6, 6)
            painter.setPen(QPen(QColor("#5EE0B0")))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, "● " + self._badge)

        if with_info and self._info:
            text_width = metrics.horizontalAdvance(self._info)
            box = QRect(
                target.left() + HUD_MARGIN,
                target.bottom() - HUD_MARGIN - 24,
                text_width + 22,
                24,
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(11, 18, 32, 190))
            painter.drawRoundedRect(box, 6, 6)
            painter.setPen(QPen(QColor("#C7D5E8")))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, self._info)


class PreviewFrame(QFrame):
    """带圆角黑底的预览容器（把 PreviewWidget 塞进去）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("previewFrame")
        self.setMinimumHeight(240)
        self.preview = PreviewWidget(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.preview)


def hud_point(widget: QWidget) -> QPoint:
    """HUD 定位辅助（保留给外部叠加控件用）。"""
    return QPoint(widget.width() // 2, widget.height() // 2)
