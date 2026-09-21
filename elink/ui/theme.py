from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication
from ..paths import resource_dir


STYLE = """
QWidget { color: #182743; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMainWindow, QDialog { background: #f4f7fc; }
QLabel { background: transparent; }
QLabel#pageTitle { font-family: 'Segoe UI Variable Display', 'Microsoft YaHei UI'; font-size: 25px; font-weight: 700; }
QLabel#sectionTitle { font-size: 15px; font-weight: 600; }
QLabel#muted { color: #70809b; }
QLabel#notice { padding: 12px; background: #eaf0ff; color: #3155a0; border-radius: 8px; }
QLabel#errorNotice { padding: 12px; background: #fff0ee; color: #a53232; border: 1px solid #f0c4bd; border-radius: 8px; }
QLabel#emptyState { color: #70809b; line-height: 24px; }
QFrame#localDevice { background: #eaf0fc; border: 1px solid #dfe6f1; border-radius: 10px; padding: 8px; }
QPushButton, QToolButton { min-height: 28px; border: 1px solid #dfe6f1; background: #ffffff;
    border-radius: 7px; padding: 5px 14px; }
QPushButton:hover, QToolButton:hover { background: #edf2ff; border-color: #94abed; }
QPushButton:pressed { background: #dce6ff; }
QPushButton:focus, QToolButton:focus { border: 1px solid #244ede; }
QPushButton:disabled, QToolButton:disabled { color: #9ca9bd; border-color: #e5eaf3; background: #f1f4fa; }
QPushButton#primary { background: #244ede; border-color: #244ede; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #1b3cbb; }
QPushButton#primary:disabled { background: #a2b3e9; border-color: #a2b3e9; }
QPushButton#danger { color: #b33f49; }
QPushButton#deviceCard { background: white; border: 1px solid #dfe6f1; border-radius: 10px; padding: 0; text-align: left; min-height: 88px; }
QPushButton#deviceCard:hover, QPushButton#deviceCard:focus { background: #f0f4ff; border-color: #7596f1; }
QLabel#deviceIcon { background: #3565ed; color: white; font-size: 32px; border-radius: 9px; }
QLabel#deviceAction { color: #244ede; }
QLineEdit, QSpinBox, QComboBox { background: #ffffff; border: 1px solid #dfe6f1;
    border-radius: 7px; min-height: 28px; padding: 4px 10px; selection-background-color: #244ede; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #244ede; }
QComboBox::drop-down { width: 24px; border: 0; }
QComboBox::down-arrow { image: url('@RES@/chevron-down.svg'); width: 12px; height: 12px; }
QSpinBox::up-button, QSpinBox::down-button { width: 24px; background: #edf2fa; border: 0; }
QSpinBox::up-arrow { image: url('@RES@/chevron-up.svg'); width: 10px; height: 10px; }
QSpinBox::down-arrow { image: url('@RES@/chevron-down.svg'); width: 10px; height: 10px; }
QListWidget { background: #ffffff; border: 1px solid #dfe6f1; border-radius: 8px; }
QListWidget::item { padding: 12px; border-bottom: 1px solid #edf1f8; }
QListWidget::item:selected { background: #eaf0ff; color: #244ede; }
QPlainTextEdit { background: #f4f7fc; color: #52617c; font-family: 'Cascadia Mono';
    font-size: 12px; border: 1px solid #dfe6f1; border-radius: 7px; padding: 8px; }
QScrollArea { background: transparent; border: 0; }
QScrollArea > QWidget > QWidget { background: transparent; }
QCheckBox { spacing: 9px; min-height: 25px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QProgressBar { height: 8px; border: 0; background: #dfe6f1; border-radius: 4px; text-align: center; }
QProgressBar::chunk { background: #244ede; border-radius: 4px; }
QTabWidget::pane { border: 0; }
QTabBar::tab { padding: 12px 22px; margin-right: 8px; background: transparent; color: #70809b; }
QTabBar::tab:selected { color: #244ede; border-bottom: 3px solid #244ede; }
QTabBar::tab:hover { color: #244ede; background: #edf2ff; }
QFrame#streamToolbar { background: #edf2fa; border: 0; border-bottom: 1px solid #dfe6f1; }
QWidget#windowResizeHandle { background: transparent; }
QFrame#streamToolbar QPushButton { min-height: 0; max-height: 40px; border: 0; border-radius: 0; padding: 0; background: transparent; }
QFrame#streamToolbar QPushButton#titleControl { padding: 0 18px; min-height: 40px; }
QFrame#streamToolbar QPushButton:hover { background: #dce6f6; }
QFrame#streamToolbar QPushButton:focus { background: #dce6f6; }
QFrame#streamToolbar QPushButton#titleClose:hover { background: #edb1b5; }
QLabel#streamTitle { font-family: 'Segoe UI Variable Display'; font-weight: 600; }
QMenu { background: #ffffff; border: 1px solid #dfe6f1; border-radius: 9px; padding: 7px; }
QMenu::item { padding: 10px 32px 10px 24px; border-radius: 5px; }
QMenu::item:selected { background: #eaf0ff; color: #244ede; }
QMenu::item:disabled { color: #9ca9bd; }
QMenu::separator { height: 1px; background: #e7edf6; margin: 5px 8px; }
QToolTip { background: #182743; color: white; border: 0; padding: 6px; }
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE.replace('@RES@', resource_dir().as_posix()))
