from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication


STYLE = """
QWidget { color: #223031; font-family: 'Microsoft YaHei UI'; font-size: 13px; }
QMainWindow, QDialog { background: #f5f7f9; }
QFrame#sidebar { background: #223333; }
QFrame#sidebar QLabel { color: #dce8e6; background: transparent; }
QFrame#sidebar QPushButton { text-align: left; background: transparent; color: #dce8e6;
    border: 0; border-radius: 5px; padding: 13px 12px; }
QFrame#sidebar QPushButton:checked { background: #36524f; color: #ffffff; }
QFrame#sidebar QPushButton:hover { background: #2c4342; }
QLabel#brand { font-family: 'Segoe UI Variable Display'; font-size: 28px; font-weight: 700; color: white; }
QLabel#pageTitle { font-size: 23px; font-weight: 700; }
QLabel#sectionTitle { font-size: 15px; font-weight: 600; }
QLabel#hostTitle { font-size: 21px; font-weight: 700; }
QLabel#muted { color: #667778; }
QLabel#metric { font-family: 'Cascadia Mono'; font-size: 14px; color: #315f9d; }
QLabel#notice { padding: 10px; background: #e8eff5; color: #375369; border-radius: 4px; }
QPushButton, QToolButton { min-height: 28px; border: 1px solid #cbd6d9; background: #ffffff;
    border-radius: 4px; padding: 5px 12px; }
QPushButton:hover, QToolButton:hover { background: #eaf1f2; border-color: #99b3b0; }
QPushButton:pressed { background: #dbe9e6; }
QPushButton:disabled, QToolButton:disabled { color: #99a5aa; border-color: #dce3e6; background: #f1f4f6; }
QPushButton#primary { background: #176d61; border-color: #176d61; color: white; font-weight: 600; }
QPushButton#primary:hover { background: #105e52; }
QPushButton#primary:disabled { background: #a7c2bd; border-color: #a7c2bd; color: #f4f8f7; }
QPushButton#danger { color: #ae424c; }
QLineEdit, QSpinBox, QComboBox { background: #ffffff; border: 1px solid #cbd6d9;
    border-radius: 4px; min-height: 28px; padding: 4px 8px; selection-background-color: #176d61; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #176d61; }
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled { background: #edf1f3; color: #8b999e; }
QComboBox::drop-down { width: 22px; border: 0; }
QListWidget, QTreeWidget, QTableWidget { background: #ffffff; border: 1px solid #d7e0e3;
    border-radius: 4px; outline: none; selection-background-color: #e3efeb; selection-color: #174c43; }
QListWidget::item { padding: 12px 10px; border-bottom: 1px solid #eef2f4; }
QListWidget::item:selected, QTreeWidget::item:selected, QTableWidget::item:selected { background: #e3efeb; color: #174c43; }
QHeaderView::section { background: #edf2f5; color: #627477; border: 0; padding: 8px; text-align: left; }
QTreeWidget::item { height: 34px; }
QPlainTextEdit { background: #202d31; color: #d5e3e7; font-family: 'Cascadia Mono';
    font-size: 12px; border: 0; border-radius: 4px; padding: 8px; }
QScrollArea { background: transparent; border: 0; }
QScrollArea > QWidget > QWidget { background: transparent; }
QCheckBox { spacing: 9px; min-height: 25px; }
QCheckBox::indicator { width: 16px; height: 16px; }
QProgressBar { height: 8px; border: 0; background: #dce6e7; border-radius: 3px; text-align: center; }
QProgressBar::chunk { background: #176d61; border-radius: 3px; }
QStatusBar { background: #eaf0f3; color: #586d73; border-top: 1px solid #d7e0e3; }
QTabWidget::pane { border: 0; }
QTabBar::tab { padding: 10px 18px; background: transparent; color: #5c7176; }
QTabBar::tab:selected { color: #176d61; border-bottom: 2px solid #176d61; }
QFrame[divider='true'] { background: #dce4e7; max-height: 1px; min-height: 1px; }
QToolTip { background: #223333; color: white; border: 0; padding: 5px; }
"""


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE)
