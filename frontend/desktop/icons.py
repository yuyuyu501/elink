"""SVG 图标（按主题色着色后缓存）。

做法与桌面端惯例一致：读取 SVG 源码 → 改 ``stroke`` → ``QSvgRenderer`` 渲染到
多倍图 → ``QIcon``。这样一套图标可以在浅色/深色、正常/危险色之间复用，
不必为每种颜色准备一份文件。
"""

from __future__ import annotations

from functools import lru_cache
from xml.etree import ElementTree

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel

from .paths import resource_dir
from .theme import MUTED, TEXT

SVG_NS = "http://www.w3.org/2000/svg"


@lru_cache(maxsize=128)
def icon(name: str, color: str = TEXT) -> QIcon:
    path = resource_dir() / "lucide" / f"{name}.svg"
    if not path.is_file():
        return QIcon()
    root = ElementTree.parse(path).getroot()
    root.set("stroke", color)
    renderer = QSvgRenderer(QByteArray(ElementTree.tostring(root)))

    result = QIcon()
    for size in (16, 20, 24, 32):
        pixmap = QPixmap(size * 2, size * 2)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        result.addPixmap(pixmap)
    return result


def icon_label(
    name: str,
    size: int = 20,
    color: str = TEXT,
    parent=None,
    *,
    glyph_size: int | None = None,
) -> QLabel:
    label = QLabel(parent)
    label.setFixedSize(size, size)
    glyph = QSize(glyph_size or size, glyph_size or size)
    got = icon(name, color)
    if not got.isNull():
        label.setPixmap(got.pixmap(glyph, label.devicePixelRatioF()))
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    return label


def clear_cache() -> None:
    icon.cache_clear()


__all__ = ["icon", "icon_label", "clear_cache", "MUTED"]
