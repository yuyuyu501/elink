"""Automatically discovered computers and their connection actions."""

from __future__ import annotations

import platform
import socket

from PySide6.QtCore import QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (
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
        port = int(peer.get("srt_port") or 0)
    except (ValueError, TypeError):
        return False
    return bool(peer.get("hosting") and peer.get("ip") and 0 < port <= 65535)


class ComputerName(ElidedLabel):
    def __init__(self, text: str = "") -> None:
        super().__init__(text)
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

        self.open_button = QPushButton()
        self.open_button.setObjectName("openComputer")
        self.open_button.setMinimumHeight(62)
        name = str(peer.get("name") or "未命名设备")
        self.open_button.setAccessibleName(f"本机 {name}" if local else f"打开 {name}")
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        content = QHBoxLayout(self.open_button)
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
        self.open_button.clicked.connect(on_info if local else on_open)
        row.addWidget(self.open_button, 1)

        self.info_button = _icon_button("info", f"查看 {name} 信息")
        self.info_button.clicked.connect(on_info)
        row.addWidget(self.info_button)
        if local:
            space = QWidget()
            space.setFixedWidth(34)
            row.addWidget(space)
        else:
            self.arrow_button = _icon_button("chevron-right", f"进入 {name}")
            self.arrow_button.clicked.connect(on_open)
            row.addWidget(self.arrow_button)


class DevicesPage(QWidget):
    connect_peer = Signal(dict, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._peers: list[dict] = []
        self._hardware: list[dict] = []
        self._selected_key = ""
        self._selected_peer: dict = {}
        self._discovery_enabled = True
        self._discovery_message = ""
        self._local_hosting = False
        self._local_srt = ""
        self._local_control_port = 7000
        self._local_srt_ready = True
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
        heading = QHBoxLayout()
        heading.addWidget(title("全部设备"), 1)
        self.search = QLineEdit()
        self.search.setObjectName("deviceSearch")
        self.search.setPlaceholderText("搜索设备")
        self.search.setAccessibleName("搜索设备")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(220)
        self.search.addAction(icons.icon("search", "#596778"), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._render_rows)
        heading.addWidget(self.search)
        layout.addLayout(heading)
        self.discovery_hint = hint("")
        layout.addWidget(self.discovery_hint)
        layout.addSpacing(10)
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
        self.empty_label = hint("没有匹配的设备")
        layout.addWidget(self.empty_label)
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
            ("terminal", "终端"), ("cable", "端口映射"),
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
        self.search.setFocus(Qt.FocusReason.OtherFocusReason)

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

    def set_discovery_state(self, enabled: bool, message: str = "") -> None:
        self._discovery_enabled = enabled
        self._discovery_message = message
        self._update_discovery_hint()
        self._update_detail()

    def set_local_state(
        self, hosting: bool, srt_text: str = "", control_port: int = 7000, *, srt_ready: bool = True,
    ) -> None:
        self._local_hosting = hosting
        self._local_srt = srt_text
        self._local_control_port = control_port
        self._local_srt_ready = srt_ready

    def set_devices(self, items: list[dict]) -> None:
        self._hardware = [dict(item) for item in items]

    def set_peers(self, peers: list[dict]) -> None:
        self._peers = list({_peer_key(peer): dict(peer) for peer in peers if _peer_key(peer)}.values())
        self._render_rows()
        self._update_discovery_hint()
        self._update_detail()

    def _update_discovery_hint(self) -> None:
        if not self._discovery_enabled:
            text = "设备发现已关闭 · 可在设置中开启"
        elif self._peers:
            text = f"已发现 {len(self._peers)} 台设备 · 自动更新"
        else:
            text = "正在自动发现网络中的 Elink 设备"
        if self._discovery_enabled and any(
            word in self._discovery_message for word in ("失败", "不可用", "未连接")
        ):
            text += f" · {self._discovery_message}"
        self.discovery_hint.setText(text)
        self.discovery_hint.setToolTip(self._discovery_message)

    def _render_rows(self) -> None:
        while self.row_layout.count():
            widget = self.row_layout.takeAt(0).widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        self.rows = []
        query = self.search.text().strip().casefold()
        local = {"name": socket.gethostname() or "本机", "platform": platform.platform()}
        candidates = [(local, True)] + [(peer, False) for peer in self._peers]
        for peer, is_local in candidates:
            addresses = " ".join(item["ip"] for item in peer.get("addresses", []))
            searchable = f"{peer.get('name', '')} {peer.get('ip', '')} {addresses} {'本机' if is_local else ''}"
            if query and query not in searchable.casefold():
                continue
            row = ComputerRow(
                peer,
                local=is_local,
                on_open=lambda checked=False, key=_peer_key(peer): self.open_peer(key),
                on_info=lambda checked=False, data=dict(peer), local=is_local: self._show_info(data, local),
            )
            self.rows.append(row)
            self.row_layout.addWidget(row)
        self.group_button.setText(f"电脑  {len(self.rows)}")
        self.empty_label.setVisible(not self.rows)
        if query:
            self.group_button.setChecked(True)
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
            message = "设备已离线，重新发现后可连接。" if self._discovery_enabled else "请先在设置中开启设备发现。"
        elif not ready:
            message = "对方尚未开放桌面连接。当前版本需在对方的「高级连接 → 主机配置」中开启串流。"
        else:
            message = ""
        self.connection_hint.setText(message)
        self.connection_hint.setVisible(bool(message))
        self.watch_button.setToolTip(message or "仅观看画面，不接管对方键鼠")
        self.enter_button.setToolTip(message or "连接并接管键鼠；封面为默认壁纸")
        self.enter_button.update()

    def show_selected_info(self) -> None:
        if self._selected_peer:
            self._show_info(self._selected_peer, False)

    def _show_info(self, peer: dict, local: bool) -> None:
        name = str(peer.get("name") or "未命名设备")
        lines = [f"系统：{peer.get('platform') or '未知'}"]
        if local:
            lines.append(f"设备发现：{'已开启' if self._discovery_enabled else '已关闭'}")
            ready = self._local_hosting and self._local_srt_ready
            lines.append(f"桌面连接：{'已开放' if ready else '未开放'}")
            if ready:
                lines.append(f"串流地址：{self._local_srt}")
                lines.append(f"控制端口：{self._local_control_port}")
            for item in self._hardware:
                lines.append(str(item.get("name") or item.get("id") or ""))
        else:
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
