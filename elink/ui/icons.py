"""Bundled Lucide icons, tinted once and cached at common display densities."""
from functools import lru_cache
from xml.etree import ElementTree

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel

from ..paths import resource_dir


@lru_cache(maxsize=128)
def icon(name, color='#172b46'):
    root = ElementTree.parse(resource_dir() / 'lucide' / f'{name}.svg').getroot()
    root.set('stroke', color)
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


def icon_label(name, size=20, color='#172b46', parent=None, *, glyph_size=None):
    label = QLabel(parent)
    label.setFixedSize(size, size)
    glyph = QSize(glyph_size or size, glyph_size or size)
    label.setPixmap(icon(name, color).pixmap(glyph, label.devicePixelRatioF()))
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    return label
