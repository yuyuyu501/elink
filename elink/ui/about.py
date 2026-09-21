from __future__ import annotations

import asyncio
import shutil
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QVBoxLayout, QWidget)

from .. import __version__
from .. import updates


class AboutPage(QWidget):
    progress = Signal(int, int, int)

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.future = None
        self.cancelled = threading.Event()
        self.workspace = None
        self.target = None
        self.installing = False
        self.handoff_complete = False
        self.generation = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 20, 12, 10)
        title = QLabel(f"Elink {__version__} · Windows x64 预览版")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        description = QLabel("独立远程桌面与游戏串流。由 Elink 负责采集、编码、连接和控制。\n"
                             "当前仍是预览版，游戏性能与手柄兼容性仍在完善。")
        description.setWordWrap(True)
        layout.addWidget(description)
        link = QLabel(f'<a href="{updates.RELEASES_URL}">查看 GitHub Releases / 手动下载</a>')
        link.setOpenExternalLinks(True)
        layout.addWidget(link)
        self.previews = QCheckBox("包括预览版本（当前为预览版，建议开启）")
        self.previews.setChecked(True)
        layout.addWidget(self.previews)
        self.check_button = QPushButton("检测更新")
        self.check_button.clicked.connect(self.check)
        layout.addWidget(self.check_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.status = QLabel("仅在点击检测时访问 GitHub；安装前会询问，不会自动更新。")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.hide()
        layout.addWidget(self.bar)
        self.cancel_button = QPushButton("取消下载")
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.hide()
        layout.addWidget(self.cancel_button, alignment=Qt.AlignmentFlag.AlignLeft)
        note = QLabel("便携版更新将下载完整应用，校验后退出并替换、重启；配置与用户数据保留。\n"
                      "更新会断开当前串流；新版启动后自动恢复等待连接。\n"
                      "旧程序会保留在应用旁的 .Elink-update-* 文件夹，确认新版正常后可删除该备份。")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        self.progress.connect(self.show_progress)

    def set_busy(self, busy):
        self.check_button.setEnabled(not busy)
        self.previews.setEnabled(not busy)
        if not busy:
            self.future = None
            self.cancel_button.hide()
            self.bar.hide()

    def check(self):
        if self.future or self.installing or self.window._closing:
            return
        self.set_busy(True)
        self.cancelled = threading.Event()
        self.generation += 1
        job = self.generation
        self.status.setText("正在检测 GitHub Releases…")
        self.future = self.window.runtime.submit(
            updates.check_release(self.previews.isChecked()),
            lambda result: self.checked(result) if job == self.generation else None,
            lambda error: self.failed(error) if job == self.generation else None)

    def checked(self, release):
        self.set_busy(False)
        if self.window._closing:
            return
        if release is None:
            self.status.setText(f"当前 {__version__} 已是所选渠道的最新可用版本。")
            return
        self.status.setText(f"发现 {release.version}{' 预览版' if release.preview else ''} · {release.size / 1048576:.1f} MiB")
        try:
            self.target = updates.installation_dir(self.window.root)
        except Exception as exc:
            self.status.setText(self.status.text() + f"\n{exc}\n可使用上方链接手动下载。")
            return
        answer = QMessageBox.question(
            self, "发现新版本",
            f"将 {__version__} 更新到 {release.version}？\n下载约 {release.size / 1048576:.1f} MiB。"
            "\n下载和校验完成后将断开串流，退出并重启 Elink。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            self.status.setText("已暂缓更新，可稍后再次检测。")
            return
        self.cancelled = threading.Event()
        self.generation += 1
        job = self.generation
        self.set_busy(True)
        self.bar.setValue(0)
        self.bar.show()
        self.cancel_button.show()
        self.status.setText("正在下载更新…")
        self.future = self.window.runtime.submit(
            updates.prepare_update(release, self.target, self.cancelled,
                                   lambda count, total: self.progress.emit(job, count, total)),
            lambda workspace: self.prepared(workspace, job),
            lambda error: self.failed(error) if job == self.generation else None)

    def show_progress(self, job, received, total):
        if job != self.generation or self.window._closing or self.cancelled.is_set():
            return
        self.bar.setValue(int(received * 100 / max(1, total)))
        self.status.setText("正在校验并解压…" if received == total else
                            f"正在下载：{received / 1048576:.1f} / {total / 1048576:.1f} MiB")

    def prepared(self, workspace, job):
        if job != self.generation or self.window._closing or self.cancelled.is_set():
            shutil.rmtree(workspace, ignore_errors=True)
            return
        self.workspace = workspace
        self.installing = True
        self.cancel_button.hide()
        self.status.setText("更新包已校验，正在启动更新助手…")
        self.future = self.window.runtime.submit(
            asyncio.to_thread(updates.launch_helper, workspace, self.target, self.window.root),
            self.helper_started, self.failed)

    def helper_started(self, _):
        # Arm only after a successful hand-off, immediately before graceful shutdown.
        try:
            (self.workspace / "armed").write_text("ready", encoding="ascii")
        except OSError as exc:
            self.failed(str(exc))
            return
        self.status.setText("正在退出，随后自动更新并重启…")
        self.handoff_complete = True
        self.window.close()

    def failed(self, message):
        self.installing = False
        self.set_busy(False)
        self.status.setText("已取消更新，应用未修改。" if self.cancelled.is_set() else
                            f"更新未完成：{message}\n可重试，或通过上方链接手动下载。")

    def cancel(self):
        self.generation += 1
        self.cancelled.set()
        if self.future:
            self.future.cancel()
        self.set_busy(False)
        self.status.setText("已取消更新，应用未修改。")

    def shutdown(self):
        if not self.installing:
            self.cancel()
