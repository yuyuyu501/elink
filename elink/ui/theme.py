from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QProxyStyle, QStyle
from ..paths import resource_dir


STYLE = """
QWidget { color: #172b46; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMainWindow, QDialog { background: #f5f7fb; }
QLabel { background: transparent; }
QLabel#brandTitle { font-family: 'Segoe UI Variable Display'; font-size: 26px; font-weight: 700; }
QLabel#pageTitle { font-size: 22px; font-weight: 700; }
QLabel#sectionTitle { font-size: 15px; font-weight: 600; }
QLabel#brandMark, QLabel#deviceIcon { background: #2855eb; border-radius: 8px; }
QLabel#muted, QLabel#versionLabel { color: #6c7b90; }
QLabel#versionLabel { font-family: 'Segoe UI'; font-size: 12px; }
QLabel#deviceName { font-size: 14px; font-weight: 600; }
QLabel#hostStatus { color: #168462; }
QLabel#emptyState { color: #6c7b90; padding: 28px; }
QLabel#notice { padding: 12px; background: #eaf0ff; color: #3155a0; border-radius: 6px; }
QLabel#errorNotice { padding: 12px; background: #fff0ee; color: #a53232; border: 1px solid #f0c4bd; border-radius: 6px; }
QFrame#notice { background: #eaf0ff; border-radius: 6px; }
QFrame#notice QLabel { color: #3155a0; }
QFrame#errorNotice { background: #fff0ee; border: 1px solid #f0c4bd; border-radius: 6px; }
QFrame#errorNotice QLabel { color: #a53232; }
QFrame#localDevice { background: #f0f4fa; border: 1px solid #dce4ef; border-radius: 8px; }
QFrame#sectionHeading { border: 0; border-bottom: 1px solid #dce4ef; }
QFrame#optionRow { border: 0; border-bottom: 1px solid #e5ebf3; background: #ffffff; }
QFrame#optionRow:hover { background: #f0f4fc; }
QFrame#connectAction { background: #2855eb; border-radius: 6px; }
QLabel#deviceAction { color: #ffffff; font-weight: 600; }
QPushButton, QToolButton { min-height: 20px; border: 1px solid #d5dfed; background: #ffffff;
    border-radius: 6px; padding: 8px 14px; }
QPushButton:hover, QToolButton:hover { background: #edf2ff; border-color: #9db2ee; }
QPushButton:pressed, QToolButton:pressed { background: #e2eaff; }
QPushButton:focus, QToolButton:focus { border-color: #2855eb; }
QPushButton:disabled, QToolButton:disabled { color: #929daf; border-color: #e1e7f0; background: #f0f3f8; }
QPushButton#primary { background: #2855eb; border-color: #2855eb; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover { background: #1c45d4; border-color: #1c45d4; }
QPushButton#primary:disabled { background: #a7b8ed; border-color: #a7b8ed; }
QPushButton#danger { color: #d33c51; }
QPushButton#deviceCard { background: #ffffff; border: 1px solid #dce4ef; border-radius: 8px;
    padding: 0; text-align: left; min-height: 88px; }
QPushButton#deviceCard:hover, QPushButton#deviceCard:focus { background: #f5f8ff; border-color: #8fa8f0; }
QPushButton#deviceCard:disabled { background: #f0f3f8; border-color: #e1e7f0; }
QPushButton#deviceCard QLabel:disabled { color: #929daf; }
QPushButton#deviceCard QFrame#connectAction:disabled { background: #e1e7f0; }
QPushButton#noticeClose { min-height: 0; padding: 0; border: 0; background: transparent; border-radius: 4px; }
QPushButton#noticeClose:hover { background: #f4d5d0; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #ffffff; border: 1px solid #d5dfed;
    border-radius: 6px; min-height: 20px; padding: 8px 12px; selection-background-color: #2855eb; }
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover { border-color: #a6b7d1; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color: #2855eb; }
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled { color: #929daf; background: #f0f3f8; }
QComboBox { padding-right: 30px; }
QComboBox::drop-down { width: 28px; border: 0; }
QComboBox::down-arrow { image: url('@RES@/chevron-down.svg'); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: #ffffff; border: 1px solid #d5dfed; outline: 0;
    border-radius: 6px; selection-background-color: #eaf0ff; selection-color: #2855eb; padding: 4px; }
QComboBox QAbstractItemView::item { min-height: 28px; padding: 4px 10px; border: 0; border-radius: 4px; }
QComboBox QAbstractItemView::item:selected { color: #2855eb; background: #eaf0ff; }
QComboBox QAbstractItemView::item:hover { background: #f0f4fc; }
QSpinBox, QDoubleSpinBox { padding-right: 30px; }
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 24px; background: #f0f4fa; border: 0; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url('@RES@/chevron-up.svg'); width: 10px; height: 10px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url('@RES@/chevron-down.svg'); width: 10px; height: 10px; }
QListWidget { background: #ffffff; border: 1px solid #dce4ef; border-radius: 6px; }
QListWidget::item { padding: 12px; border-bottom: 1px solid #e5ebf3; }
QListWidget::item:selected { background: #eaf0ff; color: #2855eb; }
QPlainTextEdit { background: #f5f7fb; color: #536781; font-family: 'Cascadia Mono';
    font-size: 12px; border: 1px solid #dce4ef; border-radius: 6px; padding: 8px; }
QScrollArea { background: transparent; border: 0; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }
QScrollBar::handle:vertical { background: #c3cddc; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: #9baec8; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QCheckBox { spacing: 9px; min-height: 26px; }
QCheckBox::indicator { width: 18px; height: 18px; border: 1px solid #9eadc1; border-radius: 4px; background: #ffffff; }
QCheckBox::indicator:checked { background: #2855eb; border-color: #2855eb; image: url('@RES@/lucide/check.svg'); }
QCheckBox::indicator:hover { border-color: #2855eb; }
QCheckBox::indicator:disabled { border-color: #c8d1de; background: #edf1f7; }
QCheckBox::indicator:checked:disabled { background: #a7b8ed; }
QCheckBox:focus { color: #2855eb; }
QProgressBar { height: 8px; border: 0; background: #e1e7f0; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background: #2855eb; border-radius: 4px; }
QTabWidget::pane { border: 0; border-top: 1px solid #dce4ef; top: -1px; }
QTabBar::tab { padding: 13px 24px; margin-right: 6px; background: transparent; color: #6c7b90; border-bottom: 3px solid transparent; }
QTabBar::tab:selected { color: #2855eb; border-bottom-color: #2855eb; font-weight: 600; }
QTabBar::tab:hover { color: #2855eb; background: #edf2ff; }
QFrame#streamToolbar { background: #f0f4fa; border: 0; border-bottom: 1px solid #dce4ef; }
QFrame#controlCenter, QFrame#mouseSubmenu { background: #ffffff; border: 1px solid #dce4ef; border-radius: 8px; }
QFrame#controlCenter QPushButton { text-align: left; min-height: 20px; border: 1px solid transparent; border-radius: 4px; padding: 7px 10px; }
QFrame#controlCenter QPushButton:checked, QFrame#controlCenter QPushButton:focus { background: #eaf0ff; border-color: #cbd7fa; }
QFrame#controlCenter QFrame#optionRow { border: 0; border-radius: 4px; }
QFrame#mouseSubmenu QRadioButton { min-height: 28px; spacing: 8px; }
QSlider::groove:horizontal { height: 4px; border: 0; border-radius: 2px; background: #dce4ef; }
QSlider::sub-page:horizontal { background: #2855eb; border-radius: 2px; }
QSlider::handle:horizontal { width: 16px; height: 16px; margin: -6px 0; background: #2855eb;
    border: 2px solid #ffffff; border-radius: 8px; }
QSlider::handle:horizontal:hover, QSlider::handle:horizontal:focus { background: #1c45d4; border-color: #cbd7fa; }
QToolButton#resetSensitivity { padding: 0; min-height: 0; border: 0; background: transparent; }
QToolButton#resetSensitivity:hover { background: #eaf0ff; }
QFrame#menuSeparator { background: #e1e7f0; min-height: 1px; max-height: 1px; border: 0; }
QWidget#windowResizeHandle { background: transparent; }
QFrame#streamToolbar QPushButton { min-height: 0; max-height: 40px; border: 0; border-radius: 0; padding: 0; background: transparent; }
QFrame#streamToolbar QPushButton#titleControl { padding: 0 16px; min-height: 40px; }
QFrame#streamToolbar QPushButton:hover, QFrame#streamToolbar QPushButton:focus { background: #e2eaff; }
QFrame#streamToolbar QPushButton#titleClose:hover { background: #f9dce2; }
QLabel#streamTitle { font-family: 'Microsoft YaHei UI'; font-size: 12px; font-weight: 600; }
QMenu { background: #ffffff; border: 1px solid #dce4ef; border-radius: 6px; padding: 6px; }
QMenu::item { padding: 9px 32px 9px 24px; border-radius: 4px; }
QMenu::item:selected { background: #eaf0ff; color: #2855eb; }
QMenu::item:disabled { color: #929daf; }
QMenu::separator { height: 1px; background: #e1e7f0; margin: 5px 8px; }
QToolTip { background: #172b46; color: #ffffff; border: 0; padding: 7px; }
"""


class AppStyle(QProxyStyle):
    def __init__(self):
        super().__init__('Fusion')

    def styleHint(self, hint, option=None, widget=None, returnData=None):
        # List popups open below the field; menu popups cover the selected value.
        if hint == QStyle.StyleHint.SH_ComboBox_Popup:
            return 0
        return super().styleHint(hint, option, widget, returnData)


def apply_theme(app: QApplication) -> None:
    app.setStyle(AppStyle())
    app.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo, False)
    app.setFont(QFont('Microsoft YaHei UI', 10))
    palette = app.palette()
    palette.setColor(QPalette.ColorRole.Highlight, QColor('#2855eb'))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor('#ffffff'))
    palette.setColor(QPalette.ColorRole.Link, QColor('#2855eb'))
    app.setPalette(palette)
    app.setStyleSheet(STYLE.replace('@RES@', resource_dir().as_posix()))
