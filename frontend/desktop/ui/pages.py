"""总览、主机配置和观看会话页面。

页面只做「收集参数 / 展示数据」；真正的 I/O 都通过信号交给
``MainWindow`` → ``SessionService``。这样界面可以随时重排，而不碰后端。
"""

from __future__ import annotations

import platform
import socket

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import icons
from ..service import HostSettings, SessionStats, ViewerSettings
from .preview import PreviewFrame
from .widgets import (
    ActionTile,
    Card,
    ElidedLabel,
    FormField,
    MetricStrip,
    MetricsGrid,
    QuickTile,
    SegmentedControl,
    StatusPill,
    ToggleSwitch,
    card_title,
    divider,
    hint,
    label,
    title,
)

ENCODERS = ["libx264", "h264_nvenc", "h264_qsv", "h264_amf"]
PRESETS = ["ultrafast", "veryfast", "faster", "medium", "slow"]
RESOLUTIONS = ["1920×1080", "1600×900", "1280×720", "960×540"]

PROFILES = {
    "quality": ("高清", "1080p60 · 8Mbps", "play", "blue", "1920×1080", 60.0, 8000),
    "balanced": ("均衡", "720p30 · 4Mbps", "sliders-horizontal", "green", "1280×720", 30.0, 4000),
    "latency": ("低延迟", "540p60 · 2.5Mbps", "activity", "orange", "960×540", 60.0, 2500),
}

HOST_METRICS = [
    ("fps", "编码帧率"),
    ("bitrate", "码率"),
    ("frames", "采集 / 编码"),
    ("injected", "远端输入"),
]

VIEWER_METRICS = [
    ("fps", "解码帧率"),
    ("frames", "读包 / 解码"),
    ("keyframes", "关键帧"),
    ("elapsed", "运行时长"),
]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _primary(text: str, icon_name: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("primary")
    if icon_name:
        button.setIcon(icons.icon(icon_name, "#ffffff"))
        button.setIconSize(QSize(16, 16))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _plain(text: str, icon_name: str = "", object_name: str = "") -> QPushButton:
    button = QPushButton(text)
    if object_name:
        button.setObjectName(object_name)
    if icon_name:
        button.setIcon(icons.icon(icon_name, "#4E5969"))
        button.setIconSize(QSize(15, 15))
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def _switch_line(title_text: str, switch: ToggleSwitch, note: str = "") -> QWidget:
    """紧凑开关行：一个开关 + 标题 + 单行说明。"""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    row.addWidget(switch, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(card_title(title_text))
    if note:
        note_label = ElidedLabel(note)
        note_label.setToolTip(note)
        note_label.setMaximumWidth(150)
        row.addWidget(note_label, 1)
    row.addStretch(1)
    return holder


def _header(
    title_text: str,
    subtitle: str,
    *,
    back: Signal | None = None,
    pill_text: str = "空闲",
) -> tuple[QWidget, QHBoxLayout, StatusPill]:
    """页面头部：返回键 + 标题 + 状态胶囊 + 副标题，右侧留给操作按钮。"""
    header = QWidget()
    row = QHBoxLayout(header)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(12)

    if back is not None:
        back_button = _plain("← 总览", object_name="ghost")
        back_button.clicked.connect(back.emit)
        row.addWidget(back_button, 0, Qt.AlignmentFlag.AlignTop)

    text_box = QVBoxLayout()
    text_box.setSpacing(3)
    title_row = QHBoxLayout()
    title_row.setSpacing(9)
    title_row.addWidget(title(title_text))
    pill = StatusPill(pill_text, "idle")
    title_row.addWidget(pill, 0, Qt.AlignmentFlag.AlignVCenter)
    title_row.addStretch(1)
    text_box.addLayout(title_row)
    text_box.addWidget(label(subtitle, "pageSubtitle"))
    row.addLayout(text_box, 1)

    actions = QHBoxLayout()
    actions.setSpacing(8)
    row.addLayout(actions)
    return header, actions, pill


def _scroll_column(width: int) -> tuple[QScrollArea, QVBoxLayout]:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setFixedWidth(width)
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 6, 0)
    layout.setSpacing(14)
    area.setWidget(holder)
    return area, layout


def _human_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


# --------------------------------------------------------------------------
# 总览
# --------------------------------------------------------------------------
class OverviewPage(QWidget):
    """本机首页：身份、英雄区、快速开始与实时状态。"""

    navigate = Signal(str)
    start_requested = Signal()
    snapshot_requested = Signal()
    preset_selected = Signal(str)

    def __init__(self, display_count: int, control_port: int, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hostname = socket.gethostname() or "本机"
        self._display_count = display_count

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        header = QWidget()
        head_row = QHBoxLayout(header)
        head_row.setContentsMargins(0, 0, 0, 0)
        head_row.setSpacing(12)

        text_box = QVBoxLayout()
        text_box.setSpacing(4)
        title_row = QHBoxLayout()
        title_row.setSpacing(9)
        self.pill = StatusPill("空闲", "idle")
        title_row.addWidget(self.pill)
        self.identity = title(f"本机 · {self._hostname}")
        title_row.addWidget(self.identity)
        title_row.addStretch(1)
        text_box.addLayout(title_row)
        self.subtitle = label("", "pageSubtitle")
        text_box.addWidget(self.subtitle)
        head_row.addLayout(text_box, 1)

        root.addWidget(header)

        self.update_environment(platform_text=_platform_text(), display_count=display_count,
                                control_port=control_port)

        # ---- 英雄区 ----
        hero = Card()
        hero.body().setContentsMargins(12, 12, 12, 12)
        hero.body().setSpacing(9)

        from .widgets import HeroArt

        art = HeroArt()
        art.setMinimumHeight(204)
        art_layout = QVBoxLayout(art)
        art_layout.setContentsMargins(30, 18, 30, 18)
        art_layout.setSpacing(0)

        chip_row = QHBoxLayout()
        chip_row.setSpacing(8)
        chip = StatusPill("本机推流 · 低延迟", "accent")
        chip_row.addWidget(chip)
        chip_row.addStretch(1)
        art_layout.addLayout(chip_row)
        art_layout.addStretch(1)

        headline = QLabel("把本机桌面，开成一扇窗")
        headline.setStyleSheet("font-size: 24px; font-weight: 700; color: #15304D;")
        art_layout.addWidget(headline)
        art_layout.addSpacing(7)

        blurb = QLabel("采集、编码、SRT 传输与远端键鼠控制都在这里；\n从这台机器开始一条会话，另一端即刻接管。")
        blurb.setStyleSheet("font-size: 13px; color: #5A7391;")
        art_layout.addWidget(blurb)
        art_layout.addSpacing(14)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.start_button = _primary("开始推流", "play")
        self.start_button.clicked.connect(self.start_requested.emit)
        watch_button = _plain("观看别人画面", "eye")
        watch_button.clicked.connect(lambda: self.navigate.emit("viewer"))
        buttons.addWidget(self.start_button)
        buttons.addWidget(watch_button)
        buttons.addStretch(1)
        art_layout.addLayout(buttons)
        art_layout.addSpacing(10)

        tags = QHBoxLayout()
        tags.setSpacing(14)
        for text in ("H.264 编码", "SRT 传输", "键鼠接管", "多显示器"):
            item = QLabel(text)
            item.setStyleSheet("font-size: 12px; color: #7C93AD;")
            tags.addWidget(item)
        tags.addStretch(1)
        art_layout.addLayout(tags)

        hero.add(art)

        action_row = QWidget()
        action_layout = QHBoxLayout(action_row)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.setSpacing(0)
        actions = [
            ("sliders-horizontal", "推流参数", lambda: self.navigate.emit("host")),
            ("mouse", "键鼠接管", lambda: self.navigate.emit("viewer")),
            ("camera", "保存截图", self.snapshot_requested.emit),
            ("cpu", "设备检测", lambda: self.navigate.emit("devices")),
        ]
        for index, (icon_name, text, callback) in enumerate(actions):
            if index:
                action_layout.addWidget(divider(vertical=True))
            tile = ActionTile(icon_name, text)
            tile.clicked.connect(callback)
            action_layout.addWidget(tile, 1)
        hero.add(action_row)
        root.addWidget(hero)

        # ---- 快速开始 + 运行状态 ----
        row = QHBoxLayout()
        row.setSpacing(12)

        quick = Card("快速开始", "一键套用常用推流参数")
        quick_grid = QGridLayout()
        quick_grid.setContentsMargins(0, 2, 0, 0)
        quick_grid.setSpacing(10)
        for index, (key, meta) in enumerate(PROFILES.items()):
            name, detail, icon_name, tone = meta[0], meta[1], meta[2], meta[3]
            tile = QuickTile(icon_name, name, detail, tone)
            tile.clicked.connect(lambda _=False, k=key: self.preset_selected.emit(k))
            quick_grid.addWidget(tile, index // 2, index % 2)
        replay = QuickTile("file-down", "录像回放", "读取本地 .ts 文件", "purple")
        replay.clicked.connect(lambda: self.preset_selected.emit("file"))
        quick_grid.addWidget(replay, 1, 1)
        quick_grid.setColumnStretch(0, 1)
        quick_grid.setColumnStretch(1, 1)
        quick.add_layout(quick_grid)
        row.addWidget(quick, 1)

        status = Card("运行状态", "当前会话的实时指标")
        status.setFixedWidth(320)
        self.metrics = MetricsGrid(
            [("fps", "当前帧率"), ("bitrate", "码率"), ("frames", "帧数 / 输出"), ("elapsed", "运行时长")]
        )
        status.add(self.metrics)
        status.add(divider())
        control_row = QHBoxLayout()
        control_row.addWidget(hint("控制通道"))
        control_row.addStretch(1)
        self.control_value = label("未连接", "mono")
        self.control_value.setStyleSheet("font-size: 12px; color: #86909C;")
        control_row.addWidget(self.control_value)
        status.body().addLayout(control_row)
        row.addWidget(status, 0)

        root.addLayout(row, 1)

    def update_environment(self, *, platform_text: str, display_count: int, control_port: int) -> None:
        self._display_count = display_count
        displays = "未检测到显示器" if display_count <= 0 else f"{display_count} 台显示器"
        self.subtitle.setText(f"{platform_text} · {displays} · 控制端口 {control_port}")

    def set_state(self, state: str, detail: str) -> None:
        tone = {"running": "ok", "idle": "idle", "stopping": "busy", "error": "error"}.get(state, "idle")
        text = {"running": "运行中", "idle": "空闲", "stopping": "停止中", "error": "出错"}.get(state, state)
        self.pill.set(text, tone)
        running = state in {"running", "stopping"}
        self.start_button.setText("打开主机控制台" if running else "开始推流")
        self.start_button.setIcon(icons.icon("monitor" if running else "play", "#ffffff"))

    def set_stats(self, stats: SessionStats) -> None:
        self.metrics.set("fps", f"{stats.fps:.1f} fps")
        self.metrics.set("bitrate", f"{stats.bitrate_kbps:.0f} kbps")
        self.metrics.set("frames", f"{stats.frames_in} / {stats.frames_out}")
        self.metrics.set("elapsed", f"{stats.elapsed_s:.0f} s")

    def set_control(self, text: str) -> None:
        self.control_value.setText(text or "未连接")
        self.control_value.setToolTip(text)


# --------------------------------------------------------------------------
# 主机
# --------------------------------------------------------------------------
class HostPage(QWidget):
    """主机（被控端）。"""

    start_requested = Signal(object)
    stop_requested = Signal()
    back_requested = Signal()
    clipboard_toggled = Signal(bool)
    report_requested = Signal()

    def __init__(self, displays: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._running = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        header, actions, self.pill = _header(
            "主机模式",
            "把本机屏幕编码后推给观看端，并接受对方的键鼠控制。",
            back=self.back_requested,
        )
        self.snapshot_button = _plain("截图", "camera")
        self.snapshot_button.setEnabled(False)
        actions.addWidget(self.snapshot_button)
        self.report_button = _plain("报告", "file-down")
        self.report_button.setToolTip("导出当前会话的编码/解码耗时报告")
        self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self.report_requested.emit)
        actions.addWidget(self.report_button)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(12)
        body.addLayout(left, 1)

        self.preview_frame = PreviewFrame()
        self.preview_frame.preview.set_badge("本机屏幕")
        left.addWidget(self.preview_frame, 1)

        self.metrics = MetricStrip(HOST_METRICS)
        left.addWidget(self.metrics)

        rail, rail_layout = _scroll_column(360)

        params = Card("推流参数")
        params.body().setContentsMargins(16, 12, 16, 12)
        params.body().setSpacing(8)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self._profile_group = QButtonGroup(self)
        self._profile_group.setExclusive(True)
        self._profile_buttons: dict[str, QPushButton] = {}
        for index, (key, meta) in enumerate(PROFILES.items()):
            chip = QPushButton(meta[0])
            chip.setObjectName("chip")
            chip.setCheckable(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            self._profile_group.addButton(chip, index)
            chip.clicked.connect(lambda _=False, k=key: self.apply_profile(k))
            chips.addWidget(chip)
            self._profile_buttons[key] = chip
        self._profile_buttons["balanced"].setChecked(True)
        chips.addStretch(1)
        params.body().addLayout(chips)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.display = QComboBox()
        self.display.addItems(displays or ["（未检测到显示器）"])
        self.resolution = QComboBox()
        self.resolution.addItems(RESOLUTIONS)
        self.resolution.setCurrentText("1920×1080")
        self.fps = QDoubleSpinBox()
        self.fps.setRange(5, 60)
        self.fps.setSingleStep(5)
        self.fps.setValue(30)
        self.fps.setSuffix(" fps")
        self.bitrate = QDoubleSpinBox()
        self.bitrate.setDecimals(3)
        self.bitrate.setRange(0.3, 20)
        self.bitrate.setSingleStep(0.5)
        self.bitrate.setValue(4)
        self.encoder = QComboBox()
        self.encoder.addItems(ENCODERS)
        self.preset = QComboBox()
        self.preset.addItems(PRESETS)
        self.preset.setCurrentText("veryfast")
        self.transport = SegmentedControl(["SRT", "文件"])
        self.srt_url = QLineEdit("srt://0.0.0.0:9000")
        self.srt_url.setPlaceholderText("srt://0.0.0.0:9000")
        self.file_path = QLineEdit("out/host.ts")
        self.file_path.setPlaceholderText("out/host.ts")
        for widget in (self.display, self.resolution, self.fps, self.bitrate, self.encoder, self.preset):
            widget.setMinimumWidth(0)
            widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        self.display_field = FormField("显示器", self.display)
        self.srt_field = FormField("SRT 地址", self.srt_url)
        self.file_field = FormField("文件路径", self.file_path)

        grid.addWidget(self.display_field, 0, 0, 1, 2)
        grid.addWidget(FormField("输出分辨率", self.resolution), 1, 0)
        grid.addWidget(FormField("帧率", self.fps), 1, 1)
        grid.addWidget(FormField("码率（Mbps）", self.bitrate), 2, 0)
        grid.addWidget(FormField("编码器", self.encoder), 2, 1)
        grid.addWidget(FormField("编码预设", self.preset), 3, 0)
        grid.addWidget(FormField("传输方式", self.transport), 3, 1)
        grid.addWidget(self.srt_field, 4, 0, 1, 2)
        grid.addWidget(self.file_field, 5, 0, 1, 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        params.body().addLayout(grid)

        params.add(divider())
        control_title = QHBoxLayout()
        control_title.addWidget(card_title("远端控制"))
        control_title.addStretch(1)
        control_title.addWidget(hint("TCP 控制通道"))
        params.body().addLayout(control_title)

        control_row = QHBoxLayout()
        control_row.setSpacing(12)
        self.control_port = QSpinBox()
        self.control_port.setRange(1024, 65535)
        self.control_port.setValue(7000)
        control_row.addWidget(FormField("控制端口", self.control_port), 0)
        control_row.addStretch(1)
        self.allow_inject = ToggleSwitch()
        self.allow_inject.setChecked(True)
        self.clipboard_switch = ToggleSwitch()
        self.clipboard_switch.setChecked(True)
        switches = QVBoxLayout()
        switches.setSpacing(6)
        switches.addWidget(_switch_line("允许远端键鼠", self.allow_inject, "关闭后只推画面"))
        switches.addWidget(_switch_line("同步剪贴板", self.clipboard_switch, "仅文本，最多 64000 字符"))
        control_row.addLayout(switches, 1)
        params.body().addLayout(control_row)
        self.clipboard_switch.toggled.connect(self.clipboard_toggled.emit)

        rail_layout.addWidget(params)
        rail_layout.addStretch(1)

        self.start_button = _primary("开始推流", "play")
        self.stop_button = _plain("停止", "square", object_name="danger")
        self.stop_button.setEnabled(False)
        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        action_row.addWidget(self.start_button, 1)
        action_row.addWidget(self.stop_button)
        rail_wrap = QVBoxLayout()
        rail_wrap.setSpacing(10)
        rail_wrap.addWidget(rail, 1)
        rail_wrap.addLayout(action_row)
        body.addLayout(rail_wrap, 0)

        self._controls = [
            self.display,
            self.resolution,
            self.fps,
            self.bitrate,
            self.encoder,
            self.preset,
            self.transport,
            self.srt_url,
            self.file_path,
            self.control_port,
            self.allow_inject,
            *self._profile_buttons.values(),
        ]

        self.start_button.clicked.connect(self._emit_start)
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.transport.currentIndexChanged.connect(self._sync_transport)
        self._sync_transport(0)

    def _emit_start(self) -> None:
        self.start_requested.emit(self.settings())

    def _sync_transport(self, index: int) -> None:
        is_srt = index == 0
        self.srt_field.setVisible(is_srt)
        self.file_field.setVisible(not is_srt)
        self.srt_url.setEnabled(is_srt and not self._running)
        self.file_path.setEnabled(not is_srt and not self._running)

    def sync_transport(self) -> None:
        self._sync_transport(self.transport.currentIndex())

    def apply_profile(self, key: str) -> None:
        meta = PROFILES.get(key)
        if meta is None:
            return
        _name, _detail, _icon, _tone, resolution, fps, bitrate = meta
        self.resolution.setCurrentText(resolution)
        self.fps.setValue(fps)
        self.bitrate.setValue(bitrate / 1000)
        for profile_key, button in self._profile_buttons.items():
            button.setChecked(profile_key == key)

    def settings(self) -> HostSettings:
        width, height = (int(part) for part in RESOLUTIONS[self.resolution.currentIndex()].split("×"))
        return HostSettings(
            display_index=self.display.currentIndex(),
            width=width,
            height=height,
            fps=float(self.fps.value()),
            bitrate_kbps=round(self.bitrate.value() * 1000),
            encoder=ENCODERS[self.encoder.currentIndex()],
            preset=PRESETS[self.preset.currentIndex()],
            transport="srt" if self.transport.currentIndex() == 0 else "file",
            srt_url=self.srt_url.text().strip(),
            file_path=self.file_path.text().strip(),
            control_port=int(self.control_port.value()),
            allow_inject=self.allow_inject.isChecked(),
            clipboard_enabled=self.clipboard_switch.isChecked(),
            gamepad_enabled=True,
        )

    def set_state(self, state: str, detail: str) -> None:
        tone = {"running": "ok", "idle": "idle", "stopping": "busy", "error": "error"}.get(state, "idle")
        text = {"running": "运行中", "idle": "空闲", "stopping": "停止中", "error": "出错"}.get(state, state)
        self.pill.set(text, tone)

    def set_running(self, running: bool) -> None:
        self._running = running
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.snapshot_button.setEnabled(running)
        self.report_button.setEnabled(running)
        for widget in self._controls:
            widget.setEnabled(not running)
        self._sync_transport(self.transport.currentIndex())

    def show_stats(self, stats: SessionStats) -> None:
        self.metrics.set("fps", f"{stats.fps:.1f} fps")
        self.metrics.set("bitrate", f"{stats.bitrate_kbps:.0f} kbps")
        self.metrics.set("frames", f"{stats.frames_in} / {stats.frames_out}")
        self.metrics.set("injected", str(stats.injected_events))


# --------------------------------------------------------------------------
# 观看
# --------------------------------------------------------------------------
class ViewerPage(QWidget):
    """观看（控制端）。"""

    start_requested = Signal(object)
    stop_requested = Signal()
    control_toggled = Signal(bool)
    back_requested = Signal()
    stats_toggled = Signal(bool)
    release_requested = Signal()
    clipboard_toggled = Signal(bool)
    gamepad_toggled = Signal(bool)
    report_requested = Signal()
    fullscreen_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._running = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        header, actions, self.pill = _header(
            "观看模式",
            "连接对方主机，实时观看画面并（可选）接管键鼠。",
            back=self.back_requested,
        )
        self.stats_button = QPushButton("统计")
        self.stats_button.setObjectName("chip")
        self.stats_button.setCheckable(True)
        self.stats_button.setEnabled(False)
        self.stats_button.setToolTip("在画面上叠加连接统计（F10）")
        self.stats_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stats_button.toggled.connect(self.stats_toggled.emit)
        actions.addWidget(self.stats_button)
        self.release_button = _plain("释放输入", "keyboard")
        self.release_button.setToolTip("释放远端键鼠控制（F8）")
        self.release_button.setEnabled(False)
        self.release_button.clicked.connect(self.release_requested.emit)
        actions.addWidget(self.release_button)
        self.fullscreen_button = _plain("全屏", "maximize")
        self.fullscreen_button.setToolTip("打开独立全屏播放器（F11）")
        self.fullscreen_button.setEnabled(False)
        self.fullscreen_button.clicked.connect(self.fullscreen_requested.emit)
        actions.addWidget(self.fullscreen_button)
        self.snapshot_button = _plain("截图", "camera")
        self.snapshot_button.setEnabled(False)
        actions.addWidget(self.snapshot_button)
        self.report_button = _plain("报告", "file-down")
        self.report_button.setToolTip("导出当前会话的解码/渲染耗时报告")
        self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self.report_requested.emit)
        actions.addWidget(self.report_button)
        root.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(16)
        root.addLayout(body, 1)

        left = QVBoxLayout()
        left.setSpacing(12)
        body.addLayout(left, 1)

        self.preview_frame = PreviewFrame()
        self.preview_frame.preview.set_badge("远端画面")
        left.addWidget(self.preview_frame, 1)

        self.metrics = MetricStrip(VIEWER_METRICS)
        left.addWidget(self.metrics)

        rail, rail_layout = _scroll_column(360)

        connect = Card("连接主机", "SRT 地址或本地录像")
        self.transport = SegmentedControl(["SRT 拉流", "读取文件"])
        self.srt_url = QLineEdit("srt://127.0.0.1:9000")
        self.srt_url.setPlaceholderText("srt://127.0.0.1:9000")
        self.srt_url.setStyleSheet("font-family: 'Cascadia Mono'; font-size: 13px;")
        self.file_path = QLineEdit("out/host.ts")
        self.file_path.setPlaceholderText("out/host.ts")
        connect.add(FormField("传输方式", self.transport))
        self.srt_field = FormField("SRT 地址", self.srt_url)
        self.file_field = FormField("文件路径", self.file_path)
        connect.add(self.srt_field)
        connect.add(self.file_field)
        connect.add(divider())
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self.control_host = QLineEdit("127.0.0.1")
        self.control_host.setPlaceholderText("127.0.0.1")
        self.control_port = QSpinBox()
        self.control_port.setRange(1024, 65535)
        self.control_port.setValue(7000)
        grid.addWidget(FormField("对方地址", self.control_host), 0, 0)
        grid.addWidget(FormField("控制端口", self.control_port), 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        connect.body().addLayout(grid)
        rail_layout.addWidget(connect)

        capture = Card("输入与画质", "转发键鼠/剪贴板/手柄，或用 FSR 1 提升画面")
        self.control_toggle = ToggleSwitch()
        self.clipboard_switch = ToggleSwitch()
        self.clipboard_switch.setChecked(True)
        self.gamepad_switch = ToggleSwitch()
        self.gamepad_switch.setChecked(True)
        capture_switches = QVBoxLayout()
        capture_switches.setSpacing(6)
        capture_switches.addWidget(_switch_line("转发本机键鼠", self.control_toggle, "连接后生效"))
        capture_switches.addWidget(_switch_line("同步剪贴板", self.clipboard_switch, "仅文本"))
        capture_switches.addWidget(_switch_line("转发手柄", self.gamepad_switch, "XInput / ViGEmBus"))
        capture.body().addLayout(capture_switches)
        self.sr_switch = ToggleSwitch()
        self.sr_mode = QComboBox()
        self.sr_mode.setObjectName("srMode")
        self.sr_mode.addItems(["FSR", "RTX"])
        self.sr_mode.setFixedWidth(82)
        self.sr_mode.setEnabled(False)
        self.sr_mode.setToolTip("RTX VSR 需要 NVIDIA RTX；FSR 1 需要 OpenGL 4.2")
        self.sr_output = QComboBox()
        self.sr_output.setObjectName("srOutput")
        self.sr_output.addItems(["1080p", "1440p", "4K"])
        self.sr_output.setFixedWidth(100)
        self.sr_output.setEnabled(False)
        sr_row = QHBoxLayout()
        sr_row.setSpacing(6)
        sr_row.addWidget(self.sr_switch, 0, Qt.AlignmentFlag.AlignVCenter)
        sr_title = card_title("超分")
        sr_title.setToolTip("观看端重建画面；输出尺寸可选 1080p / 1440p / 4K")
        sr_row.addWidget(sr_title, 0, Qt.AlignmentFlag.AlignVCenter)
        sr_row.addStretch(1)
        sr_row.addWidget(self.sr_mode, 0, Qt.AlignmentFlag.AlignVCenter)
        sr_row.addWidget(self.sr_output, 0, Qt.AlignmentFlag.AlignVCenter)
        capture.body().addLayout(sr_row)
        self.sr_switch.toggled.connect(self.sr_output.setEnabled)
        self.sr_switch.toggled.connect(self.sr_mode.setEnabled)
        self.clipboard_switch.toggled.connect(self.clipboard_toggled.emit)
        self.gamepad_switch.toggled.connect(self.gamepad_toggled.emit)
        rail_layout.addWidget(capture)
        rail_layout.addStretch(1)

        self.start_button = _primary("开始观看", "play")
        self.stop_button = _plain("停止", "square", object_name="danger")
        self.stop_button.setEnabled(False)
        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        action_row.addWidget(self.start_button, 1)
        action_row.addWidget(self.stop_button)
        rail_layout_wrap = QVBoxLayout()
        rail_layout_wrap.addWidget(rail, 1)
        rail_layout_wrap.addLayout(action_row)
        body.addLayout(rail_layout_wrap, 0)

        self.start_button.clicked.connect(lambda: self.start_requested.emit(self.settings()))
        self.stop_button.clicked.connect(self.stop_requested.emit)
        self.control_toggle.toggled.connect(self._on_control_toggled)
        self.transport.currentIndexChanged.connect(self._sync_transport)
        self._sync_transport(0)

    def _on_control_toggled(self, enabled: bool) -> None:
        self.release_button.setEnabled(self._running and enabled)
        self.control_toggled.emit(enabled)

    def set_stats_checked(self, checked: bool) -> None:
        self.stats_button.blockSignals(True)
        self.stats_button.setChecked(checked)
        self.stats_button.blockSignals(False)

    def _sync_transport(self, index: int) -> None:
        is_srt = index == 0
        self.srt_field.setVisible(is_srt)
        self.file_field.setVisible(not is_srt)
        self.srt_url.setEnabled(is_srt and not self._running)
        self.file_path.setEnabled(not is_srt and not self._running)

    def sync_transport(self) -> None:
        self._sync_transport(self.transport.currentIndex())

    def settings(self) -> ViewerSettings:
        return ViewerSettings(
            transport="srt" if self.transport.currentIndex() == 0 else "file",
            srt_url=self.srt_url.text().strip(),
            file_path=self.file_path.text().strip(),
            control_host=self.control_host.text().strip() or "127.0.0.1",
            control_port=int(self.control_port.value()),
            control_enabled=self.control_toggle.isChecked(),
            clipboard_enabled=self.clipboard_switch.isChecked(),
            gamepad_enabled=self.gamepad_switch.isChecked(),
            sr_enabled=self.sr_switch.isChecked(),
            sr_mode="rtx" if self.sr_mode.currentText() == "RTX" else "fsr",
            sr_output=self.sr_output.currentText(),
        )

    def set_state(self, state: str, detail: str) -> None:
        tone = {"running": "ok", "idle": "idle", "stopping": "busy", "error": "error"}.get(state, "idle")
        text = {"running": "运行中", "idle": "空闲", "stopping": "停止中", "error": "出错"}.get(state, state)
        self.pill.set(text, tone)

    def set_running(self, running: bool) -> None:
        self._running = running
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.snapshot_button.setEnabled(running)
        self.report_button.setEnabled(running)
        self.fullscreen_button.setEnabled(running)
        self.stats_button.setEnabled(running)
        self.release_button.setEnabled(running and self.control_toggle.isChecked())
        for widget in (self.transport, self.srt_url, self.file_path, self.control_host, self.control_port):
            widget.setEnabled(not running)
        self.control_toggle.setEnabled(True)
        self.sr_switch.setEnabled(not running)
        self.sr_output.setEnabled(not running and self.sr_switch.isChecked())
        self.sr_mode.setEnabled(not running and self.sr_switch.isChecked())
        self._sync_transport(self.transport.currentIndex())

    def show_stats(self, stats: SessionStats) -> None:
        self.metrics.set("fps", f"{stats.fps:.1f} fps")
        self.metrics.set("frames", f"{stats.frames_in} / {stats.frames_out}")
        self.metrics.set("keyframes", str(stats.key_frames))
        self.metrics.set("elapsed", f"{stats.elapsed_s:.0f} s")


def _platform_text() -> str:
    system = platform.system()
    release = platform.release()
    if system == "Windows":
        return f"Windows {release}".strip()
    return f"{system} {release}".strip()
