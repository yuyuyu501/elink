"""界面主题。

视觉语言参考网易 UU 远程新版桌面端（4.x）：

* 冷白底色（#F4F7FA）+ 纯白卡片 + 1px 浅灰描边；
* 左侧窄导航栏按「设备 / 功能」分组，选中项白底 + 3px 主色指示条；
* 主色亮蓝（#2B6BFF）配青色渐变，状态用绿/橙/红低饱和底色；
* 标题 22px / 分区 15px / 正文 13px，界面克制、留白充足。

QSS 里的 ``@RES@`` 会被替换成资源目录（打包后路径会变），
控件全部通过 objectName / 动态属性选择样式，界面代码不写颜色。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

from .paths import resource_dir

# ---- 调色板 --------------------------------------------------------------
TEXT = "#1D2129"
TEXT_SECONDARY = "#4E5969"
MUTED = "#86909C"
SUBTLE = "#A9B4C4"
BACKGROUND = "#F4F7FA"
SIDEBAR = "#F1F5F9"
PANEL = "#FFFFFF"
SURFACE_SOFT = "#F7FAFD"
BORDER = "#E4E9F0"
BORDER_STRONG = "#D5DEE9"
ACCENT = "#2B6BFF"
ACCENT_HOVER = "#1E5AF0"
ACCENT_SOFT = "#EAF1FF"
CYAN = "#22C3E8"
SUCCESS = "#00B42A"
SUCCESS_SOFT = "#E8F8ED"
WARNING = "#FF7D00"
WARNING_SOFT = "#FFF4E6"
DANGER = "#F53F3F"
DANGER_SOFT = "#FFECEC"
PREVIEW_BG = "#0B1220"


STYLE = """
QWidget {
    color: #1D2129;
    font-family: 'Microsoft YaHei UI';
    font-size: 14px;
}
QMainWindow, QDialog { background: #F4F7FA; }
QLabel { background: transparent; }

/* ---- 文字层级 ---- */
QLabel#brandTitle { font-size: 17px; font-weight: 700; }
QLabel#brandSub { color: #86909C; font-size: 12px; }
QLabel#pageTitle { font-size: 23px; font-weight: 700; }
QLabel#pageSubtitle { color: #86909C; font-size: 13px; }
QLabel#sectionTitle { font-size: 16px; font-weight: 600; }
QLabel#cardTitle { font-size: 15px; font-weight: 600; }
QLabel#muted { color: #86909C; }
QLabel#caption { color: #A9B4C4; font-size: 12px; }
QLabel#mono { font-family: 'Cascadia Mono'; font-size: 13px; color: #4E5969; }
QLabel#navSection { color: #86909C; font-size: 12px; font-weight: 600; padding-left: 12px; }
QLabel#hint { color: #86909C; font-size: 12px; }
QLabel#statusText { color: #4E5969; font-size: 13px; }

/* ---- 容器 ---- */
QFrame#windowRoot { background: #F4F7FA; }
QFrame#titleBar { background: #FFFFFF; border-bottom: 1px solid #E9EEF4; }
QFrame#sidebar { background: #F1F5F9; border-right: 1px solid #E4E9F0; }
QFrame#footer { background: #FFFFFF; border-top: 1px solid #EDF1F6; }
QFrame#card { background: #FFFFFF; border: 1px solid #E4E9F0; border-radius: 12px; }
QFrame#flatCard { background: #FFFFFF; border: 0; border-radius: 12px; }
QFrame#metricTile { background: #F7FAFD; border: 1px solid #EDF2F7; border-radius: 10px; }
QFrame#metricStrip { background: #FFFFFF; border: 1px solid #E4E9F0; border-radius: 12px; }
QFrame#segmented { background: #EDF2F8; border: 0; border-radius: 9px; }
QFrame#divider { background: #EDF1F6; border: 0; }
QFrame#divider[orientation="h"] { min-height: 1px; max-height: 1px; }
QFrame#divider[orientation="v"] { min-width: 1px; max-width: 1px; }
QFrame#previewFrame { background: #0B1220; border: 1px solid #182234; border-radius: 12px; }
QFrame#heroArt { background: transparent; border: 0; }

QFrame#actionTile { background: #FFFFFF; border: 0; border-radius: 0; }
QFrame#actionTile[hover="true"] { background: #F5F9FE; }
QFrame#actionTile[first="true"] { border-top-left-radius: 0; }
QFrame#quickTile { background: #F7FAFD; border: 1px solid #EDF2F7; border-radius: 10px; }
QFrame#quickTile[hover="true"] { background: #FFFFFF; border-color: #C9DBFF; }
QFrame#deviceCard { background: #FFFFFF; border: 1px solid #E4E9F0; border-radius: 12px; }
QFrame#deviceCard[hover="true"] { border-color: #C9DBFF; background: #FBFDFF; }
QFrame#deviceRow { background: #F7FAFD; border: 1px solid #EDF2F7; border-radius: 10px; }
QFrame#deviceRow[default="true"] { border-color: #D6E4FF; background: #F5F9FF; }

/* ---- 顶部提示条 ---- */
QFrame#noticeBar { background: #FFF7E8; border: 1px solid #F3D6A5; border-radius: 10px; }
QFrame#noticeBar[tone="error"] { background: #FFF1F0; border-color: #F3C4C4; }
QFrame#noticeBar[tone="info"] { background: #EAF1FF; border-color: #C9DBFF; }
QLabel#noticeText { color: #8A4B00; font-size: 12px; }
QFrame#noticeBar[tone="error"] QLabel#noticeText { color: #A8071A; }
QFrame#noticeBar[tone="info"] QLabel#noticeText { color: #2B6BFF; }

/* ---- 状态胶囊 ---- */
QLabel#pill {
    border-radius: 11px; padding: 4px 10px 4px 9px; font-size: 13px; font-weight: 600;
    background: #F0F3F7; color: #6B7785;
}
QLabel#pill[tone="ok"] { background: #1D2129; color: #FFFFFF; }
QLabel#pill[tone="busy"] { background: #FFF4E6; color: #D96A00; }
QLabel#pill[tone="error"] { background: #FFECEC; color: #D92D2D; }
QLabel#pill[tone="accent"] { background: #EAF1FF; color: #2B6BFF; }
QLabel#pill[tone="dark"] { background: #1D2129; color: #FFFFFF; }
QLabel#statusDot { background: #C9D3DF; border-radius: 4px; }
QLabel#statusDot[tone="ok"] { background: #00B42A; }
QLabel#statusDot[tone="busy"] { background: #FF7D00; }
QLabel#statusDot[tone="error"] { background: #F53F3F; }

/* ---- 按钮 ---- */
QPushButton, QToolButton {
    background: #FFFFFF; border: 1px solid #DCE4EF; border-radius: 8px;
    padding: 7px 14px; color: #1D2129;
}
QPushButton:hover, QToolButton:hover { background: #F6F9FD; border-color: #C9D6E6; }
QPushButton:pressed, QToolButton:pressed { background: #EEF3FA; }
QPushButton:focus { border-color: #2B6BFF; }
QPushButton:disabled, QToolButton:disabled {
    color: #A9B4C4; background: #F5F7FA; border-color: #E9EEF4;
}
QPushButton#primary { background: #2B6BFF; border-color: #2B6BFF; color: #FFFFFF; font-weight: 600; }
QPushButton#primary:hover { background: #1E5AF0; border-color: #1E5AF0; }
QPushButton#primary:pressed { background: #1749CC; border-color: #1749CC; }
QPushButton#primary:disabled { background: #A9C1FF; border-color: #A9C1FF; color: #FFFFFF; }
QPushButton#danger { color: #D92D2D; border-color: #F3C9C9; background: #FFFFFF; }
QPushButton#danger:hover { background: #FFF3F3; border-color: #EDA6A6; }
QPushButton#ghost { background: transparent; border: 0; padding: 6px 10px; color: #4E5969; }
QPushButton#ghost:hover { background: #EEF3F9; }
QPushButton#ghost:disabled { background: transparent; color: #A9B4C4; }
QPushButton#linkButton { background: transparent; border: 0; padding: 0 2px; color: #2B6BFF; font-size: 13px; }
QPushButton#linkButton:hover { color: #1E5AF0; text-decoration: underline; }
QPushButton#navItem {
    text-align: left; border: 0; border-left: 3px solid transparent; border-radius: 0 8px 8px 0;
    background: transparent; padding: 0 12px; min-height: 38px; max-height: 38px;
    color: #4E5969; font-size: 14px;
}
QPushButton#navItem:hover { background: #E7EEF6; color: #1D2129; }
QPushButton#navItem:checked {
    background: #FFFFFF; border-left: 3px solid #2B6BFF; color: #1D2129; font-weight: 600;
}
QPushButton#navItem:disabled { background: transparent; color: #A9B4C4; }
QPushButton#windowButton, QPushButton#windowClose {
    border: 0; border-radius: 0; background: transparent; min-width: 46px; max-width: 46px;
    min-height: 44px; max-height: 44px; padding: 0;
}
QPushButton#windowButton:hover { background: #F1F5FA; }
QPushButton#windowClose:hover { background: #FFECEC; }
QPushButton#segment {
    border: 0; border-radius: 7px; background: transparent; padding: 5px 12px;
    min-height: 24px; color: #4E5969; font-size: 13px;
}
QPushButton#segment:hover { color: #1D2129; }
QPushButton#segment:checked { background: #FFFFFF; color: #1D2129; font-weight: 600; }
QPushButton#chip {
    background: #F3F6FA; border: 1px solid transparent; border-radius: 14px;
    padding: 4px 10px; font-size: 13px; color: #4E5969;
}
QPushButton#chip:hover { background: #EAF1FF; color: #2B6BFF; }
QPushButton#chip:checked { background: #EAF1FF; border-color: #C9DBFF; color: #2B6BFF; font-weight: 600; }
QPushButton#iconButton {
    border: 0; background: transparent; border-radius: 8px; padding: 5px;
    min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px;
}
QPushButton#iconButton:hover { background: #EEF3F9; }

/* ---- 输入控件 ---- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #FFFFFF; border: 1px solid #DCE4EF; border-radius: 8px;
    min-height: 30px; padding: 0 10px; selection-background-color: #2B6BFF;
    selection-color: #FFFFFF;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #2B6BFF; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
    color: #A9B4C4; background: #F5F7FA; border-color: #E9EEF4;
}
QLineEdit { padding: 0 11px; }
QComboBox { padding-right: 30px; }
QComboBox#srMode, QComboBox#srOutput { padding-right: 20px; font-size: 12px; }
QComboBox::drop-down { width: 28px; border: 0; }
QComboBox::down-arrow { image: url('@RES@/lucide/chevron-down.svg'); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    background: #FFFFFF; border: 1px solid #DCE4EF; outline: 0; border-radius: 8px;
    selection-background-color: #EAF1FF; selection-color: #2B6BFF; padding: 4px;
}
QComboBox QAbstractItemView::item { min-height: 30px; padding: 2px 10px; border-radius: 6px; }
QSpinBox, QDoubleSpinBox { padding-right: 26px; }
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {
    width: 22px; background: #F2F5FA; border: 0;
}
QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: 8px; }
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-position: bottom right; border-bottom-right-radius: 8px;
}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background: #E4EBF6; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url('@RES@/lucide/chevron-up.svg'); width: 9px; height: 9px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url('@RES@/lucide/chevron-down.svg'); width: 9px; height: 9px; }

QCheckBox { spacing: 8px; min-height: 24px; }
QCheckBox::indicator {
    width: 17px; height: 17px; border: 1px solid #B9C6D8; border-radius: 4px; background: #FFFFFF;
}
QCheckBox::indicator:checked {
    background: #2B6BFF; border-color: #2B6BFF; image: url('@RES@/lucide/check.svg');
}
QCheckBox::indicator:hover { border-color: #2B6BFF; }

/* ---- 文本区域 / 日志 ---- */
QTextEdit#logView, QPlainTextEdit#logView {
    background: #FBFCFE; color: #2E3A4B; border: 1px solid #E7ECF3; border-radius: 10px;
    padding: 10px 12px; font-family: 'Cascadia Mono'; font-size: 13px;
}

/* ---- 滚动条 ---- */
QScrollArea { background: transparent; border: 0; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: #C6D2E1; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #A9B9CD; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 0; }
QScrollBar::handle:horizontal { background: #C6D2E1; border-radius: 4px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QMenu {
    background: #FFFFFF; border: 1px solid #DCE4EF; border-radius: 8px; padding: 6px;
}
QMenu::item { padding: 7px 22px 7px 12px; border-radius: 6px; }
QMenu::item:selected { background: #EAF1FF; color: #2B6BFF; }
QToolTip {
    background: #1D2129; color: #FFFFFF; border: 0; padding: 5px 8px; border-radius: 4px;
}

/* ---- 全屏串流窗口 ---- */
QWidget#streamWindow { background: #05070B; }
QFrame#streamToolbar {
    background: rgba(11, 18, 32, 235); border: 1px solid #2A3B55; border-radius: 12px;
}
QLabel#streamTitle { color: #E6EDF7; font-size: 13px; font-weight: 600; }
QPushButton#streamButton {
    background: #16243A; border: 1px solid #2A3B55; color: #D6E1F0;
    border-radius: 8px; padding: 5px 10px; font-size: 12px;
}
QPushButton#streamButton:hover { background: #1E3352; border-color: #3B5580; }
QPushButton#streamButton:checked { background: #2B6BFF; border-color: #2B6BFF; color: #FFFFFF; }
QPushButton#streamButtonDanger {
    background: #3A1E24; border: 1px solid #6E3941; color: #FFB7B7;
    border-radius: 8px; padding: 5px 10px; font-size: 12px;
}
QPushButton#streamButtonDanger:hover { background: #4B262E; }
"""


def stylesheet() -> str:
    """返回替换过资源路径的样式表。"""
    return STYLE.replace("@RES@", resource_dir().as_posix())


def apply_theme(app: QApplication) -> None:
    """应用 Fusion 风格 + 调色板 + QSS。"""
    app.setStyle(QStyleFactory.create("Fusion"))

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BACKGROUND))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(PANEL))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(SURFACE_SOFT))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(PANEL))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(SUBTLE))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet())

    font = QFont("Microsoft YaHei UI", 10)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontShowIconsInMenus, False)
