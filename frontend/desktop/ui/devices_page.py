"""Automatically discovered computers and their connection actions."""

from __future__ import annotations

import platform

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from ..paths import resource_dir
from ..settings import normalize_device_name
from .widgets import ElidedLabel, StatusPill, divider, hint, label, title


def _icon_button(name: str, tooltip: str) -> QPushButton:
    button = QPushButton()
    button.setObjectName("deviceIconButton")
    button.setIcon(icons.icon(name, "#455569"))
    button.setIconSize(QSize(18, 18))
    button.setFixedSize(34, 34)
    button.setToolTip(tooltip)
    button.setAccessibleName(tooltip)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _peer_key(peer: dict) -> str:
    return str(peer.get("id") or peer.get("ip") or "")


def _connectable(peer: dict) -> bool:
    try:
        port = int(peer.get("session_port") or 0)
    except (ValueError, TypeError):
        return False
    return bool(peer.get("ip") and not peer.get("busy") and 0 < port <= 65535)


class ComputerName(ElidedLabel):
    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def sizeHint(self) -> QSize:
        metrics = self.fontMetrics()
        return QSize(min(380, metrics.horizontalAdvance(self._full_text) + 4), metrics.height() + 4)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.sizeHint().height())

    def set_full_text(self, text: str) -> None:
        super().set_full_text(text)
        self.updateGeometry()


class DesktopButton(QPushButton):
    """Wallpaper is a connection cover, never a remote screen capture."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("deviceDesktopButton")
        self.setFixedHeight(310)
        self.setMinimumWidth(320)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("进入桌面")
        self.setToolTip("连接桌面；封面为默认壁纸")
        self._wallpaper = QPixmap(str(resource_dir() / "device-wallpaper.jpg"))

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor("#0961B1"))
        if not self._wallpaper.isNull():
            scaled = self._wallpaper.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(
                (self.width() - scaled.width()) // 2,
                (self.height() - scaled.height()) // 2,
                scaled,
            )
        band = QRect(0, self.height() - 112, self.width(), 112)
        painter.fillRect(band, QColor(3, 45, 91, 170 if self.underMouse() else 145))
        font = self.font()
        font.setPixelSize(16)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        text = "进入桌面" if self.isEnabled() else "桌面暂不可连接"
        width = painter.fontMetrics().horizontalAdvance(text)
        left = (self.width() - width - 34) // 2
        painter.drawText(
            QRect(left, band.y(), width, band.height()),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
        painter.drawPixmap(
            left + width + 12, band.center().y() - 10,
            icons.icon("arrow-right", "#FFFFFF").pixmap(QSize(20, 20)),
        )
        if self.hasFocus():
            painter.setPen(QColor("#FFFFFF"))
            painter.drawRect(self.rect().adjusted(3, 3, -4, -4))


class ComputerRow(QFrame):
    def __init__(self, peer: dict, *, local: bool, on_open, on_info) -> None:
        super().__init__()
        self.peer = peer
        self.setObjectName("computerRow")
        self.setFixedHeight(66)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 10, 0)
        row.setSpacing(5)

        name = str(peer.get("name") or "未命名设备")
        if local:
            body = QWidget()
            body.setObjectName("localComputer")
        else:
            self.open_button = body = QPushButton()
            body.setObjectName("openComputer")
            body.setAccessibleName(f"打开 {name}")
            body.setCursor(Qt.CursorShape.PointingHandCursor)
            body.clicked.connect(on_open)
        body.setMinimumHeight(62)
        content = QHBoxLayout(body)
        content.setContentsMargins(0, 0, 8, 0)
        content.setSpacing(14)
        symbol = icons.icon_label("monitor", 34, "#FFFFFF", glyph_size=22)
        symbol.setObjectName("computerSymbol")
        content.addWidget(symbol)
        name_label = ComputerName(name)
        name_label.setObjectName("computerName")
        name_label.setToolTip(name)
        name_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        content.addWidget(name_label, 0 if local else 1)
        if local:
            badge = label("本机", "computerLocal")
            content.addWidget(badge, 0, Qt.AlignmentFlag.AlignVCenter)
            content.addStretch(1)
        else:
            online = label("●  在线", "computerOnline")
            content.addWidget(online)
        for index in range(content.count()):
            widget = content.itemAt(index).widget()
            if widget is not None:
                widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(body, 1)

        if not local:
            self.info_button = _icon_button("info", f"查看 {name} 信息")
            self.info_button.clicked.connect(on_info)
            row.addWidget(self.info_button)
            self.arrow_button = _icon_button("chevron-right", f"进入 {name}")
            self.arrow_button.clicked.connect(on_open)
            row.addWidget(self.arrow_button)
        for button in self.findChildren(QPushButton):
            button.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.setProperty("focused", any(button.hasFocus() for button in self.findChildren(QPushButton)))
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()
        return super().eventFilter(watched, event)


class DevicesPage(QWidget):
    connect_peer = Signal(dict, bool)
    device_name_changed = Signal(str)

    def __init__(self, parent: QWidget | None = None, *, local_name: str = "本机") -> None:
        super().__init__(parent)
        self._peers: list[dict] = []
        self._hardware: list[dict] = []
        self._selected_key = ""
        self._selected_peer: dict = {}
        self._discovery_enabled = True
        self._local_name = local_name
        self._local_hosting = False
        self._local_ready = True
        self.rows: list[ComputerRow] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_list())
        self.stack.addWidget(self._build_detail())
        root.addWidget(self.stack, 1)
        self.set_peers([])

    def _build_list(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 8, 6, 0)
        layout.setSpacing(10)
        layout.addWidget(title("全部设备"))
        self.group_button = QPushButton()
        self.group_button.setObjectName("computerGroup")
        self.group_button.setCheckable(True)
        self.group_button.setChecked(True)
        self.group_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.group_button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.group_button.toggled.connect(self._toggle_group)
        layout.addWidget(self.group_button)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.row_holder = QWidget()
        self.row_layout = QVBoxLayout(self.row_holder)
        self.row_layout.setContentsMargins(0, 0, 6, 0)
        self.row_layout.setSpacing(8)
        self.row_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.row_holder)
        layout.addWidget(self.scroll, 1)
        return page

    def _build_detail(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 4, 6, 0)
        layout.setSpacing(18)
        header = QHBoxLayout()
        header.setSpacing(10)
        self.back_button = _icon_button("log-out", "返回设备列表")
        self.back_button.setIcon(QIcon(
            icons.icon("log-out", "#455569").pixmap(QSize(36, 36))
            .transformed(QTransform().scale(-1, 1))
        ))
        self.back_button.clicked.connect(self.show_list)
        header.addWidget(self.back_button)
        header.addStretch(1)
        self.online_pill = StatusPill("在线", "ok")
        header.addWidget(self.online_pill)
        self.detail_name = ComputerName("")
        self.detail_name.setObjectName("deviceDetailName")
        self.detail_name.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        header.addWidget(self.detail_name)
        self.menu_button = _icon_button("ellipsis-vertical", "设备菜单")
        self.menu = QMenu(self.menu_button)
        self.menu.addAction(icons.icon("info"), "设备信息", self.show_selected_info)
        self.menu_button.clicked.connect(
            lambda: self.menu.popup(self.menu_button.mapToGlobal(QPoint(0, 34)))
        )
        header.addWidget(self.menu_button)
        layout.addLayout(header)

        surface = QFrame()
        surface.setObjectName("deviceSurface")
        surface_layout = QVBoxLayout(surface)
        surface_layout.setContentsMargins(1, 1, 1, 1)
        surface_layout.setSpacing(0)
        self.enter_button = DesktopButton()
        self.enter_button.clicked.connect(lambda: self._request_connection(True))
        surface_layout.addWidget(self.enter_button)
        tools = QWidget()
        tools.setObjectName("deviceTools")
        toolbar = QHBoxLayout(tools)
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(0)
        self.watch_button = None
        for index, (name, text) in enumerate((
            ("folder-up", "文件传输"), ("eye", "观看模式"),
        )):
            if index:
                toolbar.addWidget(divider(vertical=True, length=20))
            button = QPushButton(f"  {text}")
            button.setObjectName("deviceTool")
            button.setIcon(icons.icon(name, "#596778"))
            button.setIconSize(QSize(18, 18))
            button.setFixedHeight(48)
            if name == "eye":
                self.watch_button = button
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.clicked.connect(lambda: self._request_connection(False))
            else:
                button.setEnabled(False)
                button.setToolTip(f"{text}暂未开放")
            toolbar.addWidget(button, 1)
        toolbar.addWidget(divider(vertical=True, length=20))
        info_button = _icon_button("layout-grid", "设备信息")
        info_button.setFixedSize(48, 48)
        info_button.clicked.connect(self.show_selected_info)
        toolbar.addWidget(info_button)
        surface_layout.addWidget(tools)
        layout.addWidget(surface)
        self.connection_hint = hint("")
        self.connection_hint.setWordWrap(True)
        layout.addWidget(self.connection_hint)
        layout.addStretch(1)
        return page

    def show_list(self) -> None:
        self.stack.setCurrentIndex(0)
        self.group_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def open_peer(self, key: str) -> None:
        peer = next((peer for peer in self._peers if _peer_key(peer) == key), None)
        if peer is None:
            return
        self._selected_key = key
        self._selected_peer = dict(peer)
        self._update_detail()
        self.stack.setCurrentIndex(1)
        self.back_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _request_connection(self, control: bool) -> None:
        peer = next((peer for peer in self._peers if _peer_key(peer) == self._selected_key), None)
        if self._discovery_enabled and peer is not None and _connectable(peer):
            self.connect_peer.emit(dict(peer), control)

    def set_discovery_state(self, enabled: bool) -> None:
        self._discovery_enabled = enabled
        self._update_detail()

    def set_local_state(self, hosting: bool, *, ready: bool = True) -> None:
        self._local_hosting = hosting
        self._local_ready = ready

    def set_devices(self, items: list[dict]) -> None:
        self._hardware = [dict(item) for item in items]

    def set_local_name(self, name: str) -> None:
        self._local_name = name
        self._render_rows()

    def set_peers(self, peers: list[dict]) -> None:
        self._peers = list({_peer_key(peer): dict(peer) for peer in peers if _peer_key(peer)}.values())
        self._render_rows()
        self._update_detail()

    def _render_rows(self) -> None:
        while self.row_layout.count():
            widget = self.row_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.rows = []
        local = {"name": self._local_name}
        candidates = [(local, True)] + [(peer, False) for peer in self._peers]
        for peer, is_local in candidates:
            row = ComputerRow(
                peer,
                local=is_local,
                on_open=lambda checked=False, key=_peer_key(peer): self.open_peer(key),
                on_info=lambda checked=False, data=dict(peer): self._show_info(data),
            )
            self.rows.append(row)
            self.row_layout.addWidget(row)
        self.group_button.setText(f"电脑  {len(self.rows)}")
        self._toggle_group(self.group_button.isChecked())

    def _toggle_group(self, expanded: bool) -> None:
        self.group_button.setIcon(icons.icon("chevron-down" if expanded else "chevron-right"))
        self.scroll.setVisible(expanded)

    def _update_detail(self) -> None:
        if not self._selected_key:
            return
        peer = next((peer for peer in self._peers if _peer_key(peer) == self._selected_key), None)
        online = self._discovery_enabled and peer is not None
        if peer is not None:
            self._selected_peer = dict(peer)
        name = str(self._selected_peer.get("name") or "未命名设备")
        self.detail_name.set_full_text(name)
        self.detail_name.setToolTip(name)
        self.online_pill.set("在线" if online else "离线", "ok" if online else "idle")
        ready = online and _connectable(peer)
        self.enter_button.setEnabled(ready)
        self.watch_button.setEnabled(ready)
        if not online:
            message = "设备已离线，重新发现后可连接。" if self._discovery_enabled else "设备发现暂不可用。"
        elif not ready:
            message = "设备正在使用中。" if peer.get("busy") else "对方连接监听尚未就绪。"
        else:
            message = ""
        self.connection_hint.setText(message)
        self.connection_hint.setVisible(bool(message))
        self.watch_button.setToolTip(message or "仅观看画面，不接管对方键鼠")
        self.enter_button.setToolTip(message or "连接并接管键鼠；封面为默认壁纸")
        self.enter_button.update()

    def show_selected_info(self) -> None:
        if self._selected_peer:
            self._show_info(self._selected_peer)

    def show_local_info(self) -> None:
        dialog = QDialog(self.window())
        dialog.setWindowTitle("设备信息")
        dialog.setMinimumWidth(360)
        layout = QVBoxLayout(dialog)
        name = QLineEdit(self._local_name)
        name.setAccessibleName("设备名称")
        layout.addWidget(label("设备名称"))
        layout.addWidget(name)
        ready = self._local_ready
        state = "等待连接" if ready else "监听未就绪"
        if self._local_hosting:
            state = "会话运行中"
        lines = [
            f"系统：{platform.platform()}",
            f"设备发现：{'已开启' if self._discovery_enabled else '已关闭'}",
            f"桌面连接：{state}",
        ]
        lines.extend(str(item.get("name") or item.get("id") or "") for item in self._hardware)
        details = label("\n".join(lines), wrap=True)
        details.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(details)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        save = buttons.button(QDialogButtonBox.StandardButton.Save)
        save.setText("保存")
        save.setEnabled(bool(normalize_device_name(name.text())))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        name.textChanged.connect(lambda text: save.setEnabled(bool(normalize_device_name(text))))
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        name.selectAll()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.device_name_changed.emit(normalize_device_name(name.text()))
        dialog.deleteLater()

    def _show_info(self, peer: dict) -> None:
        name = str(peer.get("name") or "未命名设备")
        lines = [f"系统：{peer.get('platform') or '未知'}"]
        online = self._discovery_enabled and any(
            _peer_key(item) == _peer_key(peer) for item in self._peers
        )
        lines.append(f"连接地址：{peer.get('ip') or '未知'}")
        for address in peer.get("addresses", []):
            lines.append(f"{address.get('interface') or '网络'}：{address['ip']}")
        lines.append(f"版本：{peer.get('version') or '未知'}")
        lines.append(f"设备状态：{'在线' if online else '离线'}")
        state = "设备离线" if not online else ("已开放" if _connectable(peer) else "未开放")
        lines.append(f"桌面连接：{state}")
        dialog = QMessageBox(QMessageBox.Icon.Information, "设备信息", name, parent=self)
        dialog.setInformativeText("\n".join(lines))
        dialog.setTextFormat(Qt.TextFormat.PlainText)
        dialog.setStandardButtons(QMessageBox.StandardButton.Ok)
        dialog.button(QMessageBox.StandardButton.Ok).setText("完成")
        dialog.exec()
