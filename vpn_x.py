"""VPN-X Windows desktop application."""
from __future__ import annotations

import ctypes
from collections import deque
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock, Thread
import time
import traceback

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QLockFile, QRectF, QSize, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFont, QIcon, QPainterPath, QRegion
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QTableWidget, QTableWidgetItem,
    QSystemTrayIcon, QTableView, QVBoxLayout, QWidget,
)

from app_info import APP_NAME, APP_VERSION
from ai_dialog import AIDialog
from ai_support import GUIDE, load_ai_config, save_ai_config
from ui_icons import application_icon, stroke_icon
from ui_theme import light_palette
from ui_titlebar import TitleBar, WindowDecoration, window_frame_result
from ui_transitions import PageTurnStack
from automation import BACKUP_INTERVAL, DEFAULTS, HEALTH_INTERVAL, Engine
from catalog import (
    CLOUDFLARE_CHECK, DEFAULT_AI_MODEL, DEFAULT_CHECK, PUBLIC_SETTINGS, SOURCE_FILENAME,
    CatalogDownloadError, CatalogSource, VPNError, atomic, catalog_settings, check_page_password,
    direct_hosts, encrypt, load_catalog_source, page_password_hash,
)
from verge import Bench, DATA, LEGACY_DATA, find_verge
import updates

STYLE = """
QMainWindow, QWidget#mainSurface, QDialog { background:#ffffff; }
QWidget { color:#0d0d0d; }
QLabel[role="headline"] { font-size:23px; font-weight:700; }
QLabel[role="section"] { font-size:16px; font-weight:650; }
QLabel[role="muted"] { color:#5d5d5d; }
QLabel[role="warning"] { color:#262626; font-weight:600; }
QFrame#connectionCard, QFrame#settingsCard, QFrame#passwordCard {
    background:#f7f7f7; border:1px solid #e5e5e5; border-radius:18px;
}
QPushButton {
    background:#fff; border:1px solid #dedede; border-radius:16px;
    padding:5px 16px; min-height:20px; font-weight:600;
}
QPushButton:hover { background:#f4f4f4; border-color:#c8c8c8; }
QPushButton:pressed { background:#e8e8e8; }
QPushButton:focus { border-color:#767676; }
QPushButton#proxyConfigLock:checked { background:#e5e5e5; }
QPushButton[primary="true"] { background:#0d0d0d; color:#fff; border-color:#0d0d0d; min-width:74px; }
QPushButton[primary="true"]:hover { background:#2f2f2f; }
QPushButton[primary="true"]:pressed { background:#000000; }
QPushButton[primary="true"]:focus { border-color:#767676; }
QPushButton:disabled, QPushButton[primary="true"]:disabled {
    background:#eeeeee; color:#767676; border-color:#e5e5e5;
}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {
    background:#fff; border:1px solid #dedede; border-radius:10px;
    padding:5px 9px; min-height:21px; selection-background-color:#e5e5e5; selection-color:#0d0d0d;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color:#767676; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background:#f2f2f2; color:#696969; }
QComboBox { padding-right:28px; }
QComboBox::drop-down {
    subcontrol-origin:padding; subcontrol-position:top right; width:25px; border:0;
}
QComboBox::down-arrow { image:url(:/qt-project.org/styles/commonstyle/images/arrow-down-16.png);
    width:11px; height:11px; }
QComboBox QAbstractItemView { background:#fff; border:1px solid #dedede; outline:0;
    selection-background-color:#e9e9e9; selection-color:#0d0d0d; }
QSpinBox { padding-right:27px; }
QSpinBox::up-button, QSpinBox::down-button { subcontrol-origin:padding; width:23px; border:0; }
QSpinBox::up-button { subcontrol-position:top right; }
QSpinBox::down-button { subcontrol-position:bottom right; }
QSpinBox::up-arrow { image:url(:/qt-project.org/styles/commonstyle/images/arrow-up-16.png);
    width:9px; height:9px; }
QSpinBox::down-arrow { image:url(:/qt-project.org/styles/commonstyle/images/arrow-down-16.png);
    width:9px; height:9px; }
QWidget#sidebar, QWidget#titleBar { background:#f7f7f7; }
QWidget#sidebar { border-right:1px solid #e5e5e5; }
QWidget#titleBar { border-bottom:1px solid #e5e5e5; }
QLabel#titleBarName { font-size:15px; font-weight:700; }
QToolButton#minimizeWindow, QToolButton#fullScreenWindow, QToolButton#closeWindow {
    background:transparent; border:1px solid transparent; border-radius:8px; padding:0;
}
QToolButton#minimizeWindow:hover, QToolButton#fullScreenWindow:hover, QToolButton#closeWindow:hover {
    background:#efefef;
}
QToolButton#minimizeWindow:pressed, QToolButton#fullScreenWindow:pressed, QToolButton#closeWindow:pressed {
    background:#e3e3e3;
}
QToolButton#minimizeWindow:focus, QToolButton#fullScreenWindow:focus, QToolButton#closeWindow:focus {
    border-color:#767676;
}
QPushButton#askAI {
    text-align:left; background:transparent; border:1px solid #dedede; border-radius:11px;
    padding:7px 10px; color:#0d0d0d; font-weight:550;
}
QPushButton#askAI:hover { background:#ededed; border-color:#d6d6d6; }
QPushButton#askAI:pressed { background:#e6e6e6; }
QPushButton#askAI:focus { border-color:#767676; }
QListWidget#sidebarNav {
    background:transparent; border:0; outline:0; font-weight:550;
}
QListWidget#sidebarNav::item {
    background:transparent; border:1px solid transparent; border-radius:10px;
    color:#5d5d5d; padding:8px 11px; min-height:18px;
}
QListWidget#sidebarNav::item:hover { background:#ededed; }
QListWidget#sidebarNav::item:selected { background:#e6e6e6; color:#0d0d0d; }
QListWidget#sidebarNav::item:selected:focus { border-color:#8b8b8b; }
QTableWidget, QTableView {
    background:#fff; alternate-background-color:#fafafa; border:1px solid #e5e5e5;
    border-radius:14px; outline:0; selection-background-color:#e9e9e9; selection-color:#0d0d0d;
}
QHeaderView { background:#fff; }
QHeaderView::section {
    background:#f5f5f5; border:0; border-bottom:1px solid #e5e5e5;
    color:#4d4d4d; padding:9px 10px; font-weight:600;
}
QHeaderView::section:first { border-top-left-radius:12px; }
QHeaderView::section:last { border-top-right-radius:12px; }
QFrame#nodeListFrame {
    background:qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #f5f5f5, stop:0.5 #f5f5f5, stop:0.501 #fff, stop:1 #fff);
    border:1px solid #e5e5e5; border-radius:14px;
}
QTableView#nodeList { border:0; border-radius:0; }
QTableView#nodeList QHeaderView { background:#f5f5f5; }
QTableView#nodeList QHeaderView::section { border-radius:0; }
QTableWidget::item, QTableView::item { border-bottom:1px solid #ededed; padding-left:9px; }
QStatusBar { background:#fff; color:#5d5d5d; border-top:1px solid #ededed; }
QStatusBar::item { border:0; }
QProgressBar { background:#e8e8e8; border:0; border-radius:4px; }
QProgressBar::chunk { background:#262626; border-radius:4px; }
"""


def banner(text, role):
    item = QLabel(text)
    item.setProperty("role", role)
    item.setTextFormat(Qt.TextFormat.PlainText)
    return item


def button(text, slot, primary=False):
    item = QPushButton(text)
    item.setProperty("primary", primary)
    item.clicked.connect(slot)
    return item


class Job(QThread):
    result = Signal(object)

    def __init__(self, fn, parent):
        super().__init__(parent)
        self.fn = fn
        self.download_failed = False

    def run(self):
        try:
            self.fn()
        except VPNError as e:
            self.download_failed = isinstance(e, CatalogDownloadError)
            self.result.emit(str(e))
        except Exception as e:
            # Keep URLs, stack locals and credentials out of the visible log.
            end = traceback.extract_tb(e.__traceback__)[-1]
            self.result.emit(f"{type(e).__name__}（{Path(end.filename).name}:{end.lineno}）")
        else:
            self.result.emit(None)


class CatalogEditor(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.window = parent
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 14, 0, 4)
        box.setSpacing(8)
        source_panel = QFrame()
        source_panel.setObjectName("settingsCard")
        source_layout = QVBoxLayout(source_panel)
        source_layout.setContentsMargins(16, 12, 16, 12)
        source_layout.addWidget(banner("目录来源", "section"))
        form = QFormLayout()
        self.source_url = QLineEdit()
        self.source_url.setMaxLength(8192)
        self.source_key = QLineEdit()
        self.source_key.setMaxLength(1024)
        self.source_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.update_url = QLineEdit()
        self.update_url.setMaxLength(8192)
        self.update_url.setPlaceholderText("HTTPS 更新清单地址")
        self.key_visibility = button("显示", self.toggle_key)
        key_row = QHBoxLayout()
        key_row.addWidget(self.source_key, 1)
        key_row.addWidget(self.key_visibility)
        form.addRow("VXP 链接", self.source_url)
        form.addRow("VXP 密钥", key_row)
        form.addRow("软件更新链接", self.update_url)
        source_layout.addLayout(form)
        actions = QHBoxLayout()
        actions.addWidget(button("保存来源", self.save_source))
        actions.addWidget(button("导入来源", self.import_source))
        actions.addWidget(button("导出来源", self.export_source))
        actions.addStretch()
        source_layout.addLayout(actions)
        source_layout.addWidget(banner(f"安装版从用户数据目录读取 {SOURCE_FILENAME}。", "muted"))
        box.addWidget(source_panel)
        box.addWidget(banner("制作加密目录", "section"))
        self.ai_key = QLineEdit()
        self.ai_key.setMaxLength(512)
        self.ai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_key.setPlaceholderText("可选，仅随加密 VXP 导出")
        ai_form = QFormLayout()
        ai_form.addRow("DeepSeek API Key", self.ai_key)
        self.ai_model = EndpointChoice([("DeepSeek Flash（默认）", DEFAULT_AI_MODEL),
                                        ("DeepSeek V4 Pro", "deepseek-v4-pro")],
                                       "输入 DeepSeek 模型 ID")
        self.ai_model.custom.setMaxLength(128)
        ai_form.addRow("AI 模型", self.ai_model)
        box.addLayout(ai_form)
        self.rows = QTableWidget(1, 3)
        self.rows.setShowGrid(False)
        self.rows.verticalHeader().setDefaultSectionSize(38)
        self.rows.setHorizontalHeaderLabels(["VPN 厂商", "HTTPS 订阅链接", "厂商优先级"])
        self.rows.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.rows.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.rows.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.rows.setColumnWidth(0, 150)
        box.addWidget(self.rows, 1)
        bar = QHBoxLayout()
        bar.addWidget(button("添加厂商", lambda: self.rows.insertRow(self.rows.rowCount())))
        bar.addWidget(button("删除当前行", self.remove))
        bar.addStretch()
        bar.addWidget(button("修改进入密码", parent.change_page_password))
        bar.addWidget(button("导出 .vxp", self.save, True))
        box.addLayout(bar)
        box.addWidget(banner("厂商优先级填 1–999，数字越小越优先；留空排在已设置的厂商之后。", "muted"))
        box.addWidget(banner("导出文件会包含订阅、厂商优先级和可同步的软件配置。", "muted"))

    def show_source(self, source):
        self.source_url.setText(source.url)
        self.source_key.setText(source.key)
        self.update_url.setText(source.update_url)
        self.hide_key()

    def current_source(self):
        return CatalogSource(self.source_url.text().strip(), self.source_key.text(), self.update_url.text().strip())

    def hide_key(self):
        self.source_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_visibility.setText("显示")

    def toggle_key(self):
        if self.source_key.echoMode() == QLineEdit.EchoMode.Password:
            self.source_key.setEchoMode(QLineEdit.EchoMode.Normal)
            self.key_visibility.setText("隐藏")
        else:
            self.hide_key()

    def save_source(self):
        try:
            self.window.apply_catalog_source(self.current_source())
        except (VPNError, OSError) as error:
            QMessageBox.warning(self, "无法保存来源", str(error))

    def import_source(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入目录来源", "", "JSON (*.json)")
        if not path:
            return
        try:
            self.window.apply_catalog_source(CatalogSource.load(path))
        except (VPNError, OSError) as error:
            QMessageBox.warning(self, "无法导入来源", str(error))

    def export_source(self):
        try:
            source = self.current_source()
            path, _ = QFileDialog.getSaveFileName(self, "导出目录来源", SOURCE_FILENAME, "JSON (*.json)")
            if path:
                target = Path(path if path.lower().endswith(".json") else path + ".json")
                if target.resolve() == self.window.source_file.resolve():
                    self.window.apply_catalog_source(source)
                else:
                    source.save(target)
                self.window.statusBar().showMessage("已导出目录来源", 5000)
        except (VPNError, OSError) as error:
            QMessageBox.warning(self, "无法导出来源", str(error))

    def remove(self):
        row = self.rows.currentRow()
        if row != -1:
            self.rows.removeRow(row)

    def save(self):
        try:
            rows = []
            for row in range(self.rows.rowCount()):
                name, address, priority = [self.rows.item(row, col).text().strip()
                                           if self.rows.item(row, col) else "" for col in range(3)]
                if name or address or priority:
                    if priority and not re.fullmatch(r"[0-9]{1,3}", priority):
                        raise VPNError(f"第 {row + 1} 行的厂商优先级须为 1–999 的整数，或留空")
                    rows.append({"name": name, "url": address, "priority": int(priority) if priority else None})
            source = self.current_source()
            if not source.configured:
                raise VPNError("请先填写 VXP 链接和密钥")
            config = self.window.settings()
            model = self.ai_model.address()
            data = encrypt(rows, source.key, {key: config[key] for key in PUBLIC_SETTINGS},
                           ai_key=self.ai_key.text().strip() or None,
                           ai_model=model if model != DEFAULT_AI_MODEL else None)
            path, _ = QFileDialog.getSaveFileName(self, "保存加密目录", "catalog.vxp", "VPN-X 目录 (*.vxp)")
            if not path:
                return
            atomic(path if path.lower().endswith(".vxp") else path+".vxp", data)
            self.window.events.line(f"已制作并保存加密目录（{len(rows)} 家厂商）")
            self.window.statusBar().showMessage("已导出加密目录", 5000)
        except (VPNError, OSError) as e:
            QMessageBox.warning(self, "无法导出", str(e) if isinstance(e, VPNError) else "无法写入文件")
            return


LOG_LIMIT = 3000


class Events:
    """Workers produce data; the GUI takes one batch on each timer tick."""
    def __init__(self):
        self.lock = Lock()
        self.lines = deque(maxlen=LOG_LIMIT)
        self.line_count = 0
        self.shown_count = 0
        self.latest_progress = None

    def line(self, line):
        clean = re.sub(r"(?i)https?://[^\s，；]+", "[链接]", str(line))
        clean = " ".join(clean.splitlines())[:1024]
        with self.lock:
            self.lines.append(f"{datetime.now():%H:%M:%S}  {clean}")
            self.line_count += 1

    def progress(self, *values):
        with self.lock:
            self.latest_progress = values

    def take(self, *, logs):
        with self.lock:
            progress, self.latest_progress = self.latest_progress, None
            lines = None
            if logs and self.shown_count != self.line_count:
                count = min(self.line_count - self.shown_count, len(self.lines))
                lines = tuple(self.lines)[-count:]
                self.shown_count = self.line_count
        return progress, lines


class NodeListView(QTableView):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Clip the header, viewport, and scrollbars to the same inner outline.
        outline = QPainterPath()
        outline.addRoundedRect(QRectF(self.rect()), 13, 13)
        self.setMask(QRegion(outline.toFillPolygon().toPolygon()))


class NodeTable(QAbstractTableModel):
    headers = ("节点", "厂商 / 代理组", "剩余流量", "连接延迟", "连接")

    def __init__(self, parent):
        super().__init__(parent)
        self.candidates = []
        self.current = None
        self.backups = set()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.candidates)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.headers[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        c = self.candidates[index.row()]
        column = index.column()
        if column == 0:
            return c.node.name
        if column == 1:
            return f"{c.vendor.name} / {c.group}"
        if column == 2:
            return f"{c.quota/2**30:.2f} GiB" if c.quota is not None else "未知"
        if column == 3:
            return f"{c.delay} ms" if c.delay is not None else "—"
        if self.current and c.identity == self.current.identity:
            return "使用中" if self.current.verified else "使用中 · 未测试"
        return "备用" if c.identity in self.backups else "其他"

    def replace(self, candidates, current, backups):
        self.beginResetModel()
        self.candidates, self.current, self.backups = candidates, current, backups
        self.endResetModel()

    def candidate(self, row):
        return self.candidates[row] if row is not None and 0 <= row < len(self.candidates) else None


class EndpointChoice(QWidget):
    def __init__(self, options, hint):
        super().__init__()
        self.presets = QComboBox()
        for name, address in options:
            self.presets.addItem(name, address)
        self.presets.addItem("自定义")
        self.custom = QLineEdit()
        self.custom.setPlaceholderText(hint)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.presets, 0, Qt.AlignmentFlag.AlignLeft)
        row.addWidget(self.custom, 1)
        self.presets.currentIndexChanged.connect(self.update_custom_visibility)
        self.update_custom_visibility()

    def update_custom_visibility(self):
        self.custom.setVisible(self.presets.currentData() is None)

    def set_address(self, address):
        index = self.presets.findData(address)
        if index < 0:
            self.custom.setText(address)
            index = self.presets.count() - 1
        self.presets.setCurrentIndex(index)

    def address(self):
        return self.presets.currentData() or self.custom.text().strip()


class Window(QMainWindow):
    def __init__(self, data_dir=DATA, *, start_background=True, install_root=None):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} V{APP_VERSION} · 自动代理")
        self.resize(1040, 725)
        self.setMinimumSize(760, 590)
        self.restore_maximized = False
        self.config_file = data_dir / "settings.json"
        self.source_file = data_dir / SOURCE_FILENAME
        self.catalog_source = CatalogSource()
        self.ai_key = ""
        self.ai_model = DEFAULT_AI_MODEL
        self.ai_dialog = None
        self.waiting_for_ai_exit = False
        self.last_job_ok = None
        self.source_error = ""
        self.install_root = install_root
        self.next_update = 0
        self.pending_update = None
        self.update_prompt = False
        self.subscription_prompt = None
        self.prompted_subscription_revision = 0
        self.exit_action = None
        self.tray = None
        self.events = Events()
        self.engine = Engine(self.events.line, self.events.progress, data_dir=data_dir)
        self.job = None
        self.next_sync = 0
        self.next_health = None
        self.next_backups = None
        self.monitored = None
        self.fields = {}
        self.page_hash = ""
        self.settings_loading = False
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(450)
        self.save_timer.timeout.connect(self.persist_edits)
        self.build()
        self.decoration = WindowDecoration(self)
        self.events_timer = QTimer(self)
        self.events_timer.setInterval(200)
        self.events_timer.timeout.connect(self.flush_events)
        self.events_timer.start()
        self.load_catalog_source()
        self.load_settings()
        self.timer = QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self.tick)
        if start_background:
            self.setup_tray()
            self.timer.start()
            QTimer.singleShot(0, self.startup)

    def build(self):
        panel = QWidget()
        panel.setObjectName("mainSurface")
        self.setCentralWidget(panel)
        frame = QVBoxLayout(panel)
        frame.setContentsMargins(0, 0, 0, 0)
        frame.setSpacing(0)
        self.title_bar = TitleBar(self)
        frame.addWidget(self.title_bar)

        body = QWidget()
        frame.addWidget(body, 1)
        split = QHBoxLayout(body)
        split.setContentsMargins(0, 0, 0, 0)
        split.setSpacing(0)
        split.addWidget(self.sidebar())

        content = QWidget()
        split.addWidget(content, 1)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(22, 9, 22, 12)
        layout.setSpacing(12)
        self.pages = PageTurnStack()
        for title, icon, page in (("代理控制", "proxy", self.dashboard()),
                                  ("代理设置", "settings", self.settings_page()),
                                  ("高级模式", "advanced", self.catalog_page()),
                                  ("运行日志", "logs", self.log_page())):
            self.pages.addWidget(page)
            self.navigation.addItem(QListWidgetItem(stroke_icon(icon), title))
        self.navigation.currentRowChanged.connect(self.turn_page)
        self.navigation.setCurrentRow(0)
        self.navigation.setFocus()
        layout.addWidget(self.pages, 1)
        self.progress_panel = QWidget()
        progress_layout = QVBoxLayout(self.progress_panel)
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(6)
        self.progress_label = banner("", "muted")
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        progress_layout.addWidget(self.progress_label)
        progress_layout.addWidget(self.progress_bar)
        self.progress_panel.hide()
        layout.addWidget(self.progress_panel)
        self.progress_timer = QTimer(self)
        self.progress_timer.setSingleShot(True)
        self.progress_timer.timeout.connect(self.progress_panel.hide)

    def nativeEvent(self, event_type, message):
        result = window_frame_result(self, getattr(self, "title_bar", None), event_type, message)
        return super().nativeEvent(event_type, message) if result is None else (True, result)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "title_bar"):
            self.title_bar.show_full_screen_state(self.isFullScreen())

    def toggle_full_screen(self):
        if self.isFullScreen():
            if self.restore_maximized:
                self.showMaximized()
            else:
                self.showNormal()
        else:
            self.restore_maximized = self.isMaximized()
            self.showFullScreen()

    def log_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.addWidget(banner("运行日志", "headline"))
        layout.addWidget(banner(f"保留最近 {LOG_LIMIT:,} 条运行记录。", "muted"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("执行步骤将在这里显示。")
        self.log_view.setMaximumBlockCount(LOG_LIMIT)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.log_view, 1)
        return page

    def sidebar(self):
        panel = QWidget()
        panel.setObjectName("sidebar")
        panel.setFixedWidth(196)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 13, 12, 15)
        layout.setSpacing(12)
        ask = button("Ask AI", self.open_ai)
        ask.setObjectName("askAI")
        ask.setIcon(stroke_icon("search"))
        ask.setIconSize(QSize(21, 21))
        ask.setAccessibleName("Ask AI，打开 VPN-X AI")
        layout.addWidget(ask)

        self.navigation = QListWidget()
        self.navigation.setObjectName("sidebarNav")
        self.navigation.setAccessibleName("页面导航")
        self.navigation.setSpacing(2)
        self.navigation.setIconSize(QSize(22, 22))
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.navigation, 1)

        version = banner(f"V{APP_VERSION}", "muted")
        version.setContentsMargins(10, 0, 0, 0)
        layout.addWidget(version)
        return panel

    def open_ai(self):
        if self.ai_dialog is None:
            try:
                self.ai_dialog = AIDialog(self, self.config_file.parent)
            except OSError:
                QMessageBox.warning(self, "VPN-X AI", "找不到本地使用指南，请完整安装 VPN-X。")
                return
        self.ai_dialog.present()

    def ai_state(self):
        engine = self.engine
        current = engine.current
        state = "需要恢复代理" if not current and engine.own.journal.exists() else "代理未启动"
        if current:
            state = "节点检测失败" if engine.health_failures >= 2 else "代理运行中"
        settings = engine.settings if current and engine.settings else getattr(self, "saved", DEFAULTS)
        connection = "虚拟网卡" if settings["network"] == "tun" else "系统代理"
        mode = "规则" if settings["mode"] == "rule" else "全局"
        task = self.job_title if self.job else "空闲"
        checked = engine.last_sync
        checked = datetime.fromtimestamp(checked).strftime("%Y-%m-%d %H:%M") if isinstance(checked, (int, float)) and 0 < checked < 4e9 else "未同步"
        last = "尚无记录" if self.last_job_ok is None else ("成功" if self.last_job_ok else "失败")
        return (f"版本 V{APP_VERSION}；{state}；{connection}/{mode}；备用 {len(engine.available_backups())} 个；"
                f"当前任务：{task}；上次任务：{last}；上次目录同步：{checked}；"
                f"订阅问题 {len(engine.failures)} 项；"
                f"待应用订阅：{'有' if engine.pending_profiles is not None else '无'}。")

    def ai_secrets(self):
        return (self.ai_key, self.catalog_source.key, self.catalog_source.url, self.catalog_source.update_url)

    def dashboard(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 13, 0, 0)
        layout.setSpacing(14)
        card = QFrame()
        card.setObjectName("connectionCard")
        content = QVBoxLayout(card)
        content.setContentsMargins(20, 16, 20, 16)
        content.setSpacing(6)
        self.status = banner("代理尚未启动", "headline")
        self.status.setWordWrap(True)
        self.detail = banner("点击一键启动，自动检查内核、更新订阅并连接。", "muted")
        self.detail.setWordWrap(True)
        self.warning = banner("", "warning")
        self.warning.setWordWrap(True)
        self.warning.hide()
        content.addWidget(self.status)
        content.addWidget(self.detail)
        content.addWidget(self.warning)
        row = QHBoxLayout()
        row.setContentsMargins(0, 5, 0, 0)
        self.auto_button = button("一键启动", lambda: self.with_settings("一键启动", self.engine.auto), True)
        self.stop_button = button("关闭", self.request_stop)
        self.bad_button = button("切换为备用节点", lambda: self.run_job("切换为备用节点", self.engine.switch_backup))
        self.restart_button = button("重启", lambda: self.with_settings("重启代理", self.engine.restart), True)
        self.cancel_button = button("取消操作", self.cancel_job)
        for b in (self.auto_button, self.stop_button, self.bad_button, self.restart_button):
            row.addWidget(b)
        row.addWidget(self.cancel_button)
        row.addStretch()
        content.addLayout(row)
        layout.addWidget(card)

        switch = QHBoxLayout()
        self.vendor = QComboBox()
        self.vendor.setPlaceholderText("所有厂商")
        self.vendor.setMinimumWidth(140)
        self.group = QComboBox()
        self.group.setPlaceholderText("各厂商首组")
        self.group.setMinimumWidth(170)
        self.vendor.currentIndexChanged.connect(self.fill_groups)
        self.group.currentIndexChanged.connect(self.fill_table)
        switch.addWidget(banner("厂商", "muted"))
        switch.addWidget(self.vendor, 1)
        switch.addWidget(banner("代理组", "muted"))
        switch.addWidget(self.group, 1)
        layout.addLayout(switch)
        node_list_frame = QFrame()
        node_list_frame.setObjectName("nodeListFrame")
        node_list_layout = QVBoxLayout(node_list_frame)
        node_list_layout.setContentsMargins(0, 0, 0, 0)
        self.table = NodeListView()
        self.table.setObjectName("nodeList")
        node_list_layout.addWidget(self.table)
        self.node_table = NodeTable(self.table)
        self.table.setModel(self.node_table)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.verticalHeader().hide()
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.node_menu)
        self.table.doubleClicked.connect(lambda index: self.manual_use(index.row()))
        self.table.setToolTip("双击节点，连接测试通过后切换")
        self.empty_nodes = banner("暂无节点记录", "muted")
        self.empty_nodes.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        empty_layout = QVBoxLayout(self.table.viewport())
        empty_layout.addWidget(self.empty_nodes, alignment=Qt.AlignmentFlag.AlignCenter)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setResizeContentsPrecision(0)
        for i in range(2, self.node_table.columnCount()):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(node_list_frame, 1)
        self.set_controls(False)
        return page

    def settings_page(self):
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(0, 13, 0, 4)
        box.setSpacing(10)
        self.settings_form = QFrame()
        self.settings_form.setObjectName("settingsCard")
        card_layout = QVBoxLayout(self.settings_form)
        card_layout.setContentsMargins(20, 16, 20, 18)
        card_layout.setSpacing(12)
        config_heading = QHBoxLayout()
        config_heading.addWidget(banner("代理配置", "section"), 1)
        self.proxy_lock_button = QPushButton("锁定")
        self.proxy_lock_button.setObjectName("proxyConfigLock")
        self.proxy_lock_button.setCheckable(True)
        self.proxy_lock_button.setToolTip("锁定后使用本机代理配置，订阅仍会更新")
        self.proxy_lock_button.toggled.connect(self.change_proxy_lock)
        config_heading.addWidget(self.proxy_lock_button)
        card_layout.addLayout(config_heading)
        form = QFormLayout()
        card_layout.addLayout(form)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        regions = self.fields["regions"] = QLineEdit()
        regions.setPlaceholderText("例如：香港，日本，新加坡（从高到低）")
        form.addRow("地区优先级", regions)
        self.fields["check"] = EndpointChoice(
            [("ChatGPT（默认）", DEFAULT_CHECK), ("Cloudflare（通用）", CLOUDFLARE_CHECK)],
            "填写返回 HTTP 2xx 的测试地址",
        )
        form.addRow("连接测试", self.fields["check"])
        hours = self.fields["hours"] = QSpinBox()
        hours.setRange(1, 720)
        hours.setSuffix(" 小时")
        form.addRow("自动更新间隔", hours)
        net = self.fields["network"] = QComboBox()
        net.addItem("虚拟网卡 (TUN)", "tun")
        net.addItem("Windows 系统代理", "system")
        form.addRow("网络接入方式", net)
        mode = self.fields["mode"] = QComboBox()
        mode.addItem("规则", "rule")
        mode.addItem("全局", "global")
        form.addRow("代理模式", mode)
        exceptions = self.fields["direct_hosts"] = QLineEdit()
        exceptions.setMaxLength(2000)
        exceptions.setPlaceholderText("例如：*.office.example.com, 10.1.2.3")
        exceptions.setToolTip("规则模式下直连；使用 Windows 系统代理时，也加入系统代理例外。仅保存到本机。")
        form.addRow("自定义直连地址", exceptions)
        box.addWidget(self.settings_form)
        regions.textEdited.connect(self.schedule_save)
        exceptions.textEdited.connect(self.schedule_save)
        hours.valueChanged.connect(self.schedule_save)
        net.currentIndexChanged.connect(self.schedule_save)
        mode.currentIndexChanged.connect(self.schedule_save)
        self.fields["check"].presets.currentIndexChanged.connect(self.schedule_save)
        self.fields["check"].custom.textEdited.connect(self.schedule_save)
        self.update_button = button("检查更新", lambda: self.check_updates(manual=True))
        box.addWidget(self.update_button, alignment=Qt.AlignmentFlag.AlignLeft)
        box.addWidget(banner("更改后自动保存。软件运行时每天检查一次更新。", "muted"))
        box.addStretch()
        return page

    def catalog_page(self):
        self.catalog_stack = PageTurnStack()
        login = QWidget()
        layout = QVBoxLayout(login)
        layout.addStretch()
        box = QFrame()
        box.setObjectName("passwordCard")
        box.setFixedWidth(340)
        form = QVBoxLayout(box)
        form.setContentsMargins(20, 17, 20, 17)
        form.setSpacing(9)
        form.addWidget(banner("输入管理密码", "section"))
        self.page_password = QLineEdit()
        self.page_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.page_password.setMaxLength(256)
        self.page_password.returnPressed.connect(self.unlock_catalog_page)
        self.page_password.setPlaceholderText("密码")
        form.addWidget(self.page_password)
        form.addWidget(button("进入", self.unlock_catalog_page, True))
        self.page_error = banner("", "warning")
        form.addWidget(self.page_error)
        layout.addWidget(box, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch()
        self.catalog_stack.addWidget(login)
        self.catalog_editor = CatalogEditor(self)
        self.catalog_stack.addWidget(self.catalog_editor)
        return self.catalog_stack

    def turn_page(self, index):
        if index < 0 or index == self.pages.currentIndex():
            return
        if index != 2:
            self.lock_catalog_page()
        self.pages.turn_to(index)

    def lock_catalog_page(self):
        # Relock before the outgoing page is captured for animation.
        self.catalog_stack.turn.clear()
        self.catalog_stack.setCurrentIndex(0)
        self.page_password.clear()
        self.page_error.clear()
        self.catalog_editor.hide_key()
        self.catalog_editor.ai_key.clear()
        self.catalog_editor.ai_model.set_address(DEFAULT_AI_MODEL)
        self.catalog_editor.ai_model.custom.clear()

    def unlock_catalog_page(self):
        password = self.page_password.text()
        self.page_password.clear()
        if not check_page_password(password, self.page_hash):
            self.page_error.setText("密码不正确")
            return
        self.page_error.clear()
        self.catalog_editor.ai_key.setText(self.ai_key)
        self.catalog_editor.ai_model.set_address(self.ai_model)
        table = self.catalog_editor.rows
        if table.rowCount() == 1 and all(table.item(0, col) is None for col in range(table.columnCount())):
            for index, vendor in enumerate(self.engine.catalog or []):
                if index:
                    table.insertRow(index)
                table.setItem(index, 0, QTableWidgetItem(vendor.name))
                table.setItem(index, 1, QTableWidgetItem(vendor.url))
                if vendor.priority is not None:
                    table.setItem(index, 2, QTableWidgetItem(str(vendor.priority)))
        self.catalog_stack.turn_to(1)

    def change_page_password(self):
        if self.job:
            return
        password, ok = QInputDialog.getText(self, "修改进入密码", "输入新密码",
                                            QLineEdit.EchoMode.Password)
        if not ok:
            return
        if not 1 <= len(password) <= 256:
            QMessageBox.warning(self, "修改进入密码", "请输入 1 至 256 个字符")
            return
        confirm, ok = QInputDialog.getText(self, "修改进入密码", "再次输入新密码",
                                           QLineEdit.EchoMode.Password)
        if not ok:
            return
        if password != confirm:
            QMessageBox.warning(self, "修改进入密码", "两次输入的密码不一致")
            return
        old_hash = self.page_hash
        self.page_hash = page_password_hash(password)
        try:
            self.save_settings(self.settings())
        except (VPNError, OSError) as err:
            self.page_hash = old_hash
            QMessageBox.warning(self, "无法保存密码", str(err))
            return
        self.statusBar().showMessage("进入密码已修改", 5000)

    @Slot()
    def flush_events(self):
        progress, lines = self.events.take(logs=self.log_view.isVisible())
        if progress:
            self.show_progress(*progress)
        if lines is not None:
            scrollbar = self.log_view.verticalScrollBar()
            previous = scrollbar.value()
            at_bottom = previous == scrollbar.maximum()
            self.log_view.appendPlainText("\n".join(lines))
            if not at_bottom:
                scrollbar.setValue(previous)

    def load_catalog_source(self):
        try:
            defaults = self.install_root / SOURCE_FILENAME if self.install_root else None
            self.catalog_source = load_catalog_source(self.source_file, defaults)
            self.reload_ai_config()
        except (VPNError, OSError) as error:
            reason = str(error) if isinstance(error, VPNError) else "无法读取或写入文件"
            self.source_error = f"{SOURCE_FILENAME} 无法使用：{reason}。请在「高级模式」检查目录来源。"
            self.warning.setText(self.source_error)
            self.warning.show()
            self.events.line(self.source_error)
        self.catalog_editor.show_source(self.catalog_source)
        if not self.source_error and not self.catalog_source.configured:
            self.detail.setText("请先在「高级模式」填写或导入目录来源。")

    def apply_catalog_source(self, source):
        source.save(self.source_file)
        if source != self.catalog_source or self.source_error:
            self.next_sync = 0
        if source.update_url != self.catalog_source.update_url:
            self.next_update = 0
        if self.source_error:
            self.warning.hide()
        self.source_error = ""
        self.catalog_source = source
        self.reload_ai_config()
        self.saved.update(source.settings())
        self.catalog_editor.show_source(source)
        if not self.engine.current:
            self.detail.setText("目录来源已保存，可一键启动。" if source.configured else
                                "请先在「高级模式」填写或导入目录来源。")
        self.statusBar().showMessage("目录来源已保存", 5000)
        self.events.line("已保存目录来源")

    def reload_ai_config(self):
        try:
            self.ai_key, self.ai_model = load_ai_config(self.config_file.parent, self.catalog_source.identity())
        except VPNError as err:
            self.ai_key, self.ai_model = "", DEFAULT_AI_MODEL
            self.events.line(str(err))

    def load_settings(self):
        data = dict(DEFAULTS, **self.catalog_source.settings())
        if self.config_file.exists():
            try:
                saved = json.loads(self.config_file.read_text(encoding="utf-8"))
                page_hash = saved.get("page_password_hash", "")
                if isinstance(page_hash, str):
                    self.page_hash = page_hash
                if not saved.get("endpoint_presets") and saved.get("check") == CLOUDFLARE_CHECK:
                    saved["check"] = DEFAULT_CHECK
                data.update(catalog_settings({k: saved[k] for k in PUBLIC_SETTINGS if k in saved}))
                data["proxy_settings_locked"] = saved.get("proxy_settings_locked") is True
                exceptions = saved.get("direct_hosts", "")
                direct_hosts(exceptions)
                data["direct_hosts"] = exceptions
                if saved.get("catalog_source_id") == self.catalog_source.identity():
                    self.next_sync = float(saved.get("next_sync", 0))
                self.engine.last_sync = saved.get("last_sync")
                if saved.get("update_source_id") == self.update_source_id():
                    self.next_update = float(saved.get("next_update", 0))
            except Exception:
                self.events.line("部分设置无法读取，已使用默认值")
        self.apply_fields(data)
        self.saved = data

    def apply_fields(self, data):
        self.settings_loading = True
        self.proxy_lock_button.setChecked(data.get("proxy_settings_locked", False))
        for key, field in self.fields.items():
            value = data[key]
            if isinstance(field, EndpointChoice):
                field.set_address(value)
            elif isinstance(field, QComboBox):
                field.setCurrentIndex(max(0, field.findData(value)))
            elif isinstance(field, QSpinBox):
                field.setValue(value)
            else:
                field.setText(value)
        self.settings_loading = False

    def settings(self):
        result = {}
        for key, field in self.fields.items():
            if isinstance(field, EndpointChoice):
                result[key] = field.address()
            elif isinstance(field, QComboBox):
                result[key] = field.currentData()
            elif isinstance(field, QSpinBox):
                result[key] = field.value()
            else:
                result[key] = field.text().strip()
        exceptions = result.pop("direct_hosts")
        direct_hosts(exceptions)
        return dict(catalog_settings(result), direct_hosts=exceptions,
                    proxy_settings_locked=self.proxy_lock_button.isChecked(), **self.catalog_source.settings())

    def save_settings(self, data):
        if hasattr(self, "saved") and any(data[k] != self.saved[k] for k in ("catalog", "password", "hours")):
            self.next_sync = 0
        stored = data.copy()
        stored.pop("catalog")
        stored["endpoint_presets"] = True
        stored["page_password_hash"] = self.page_hash
        stored.pop("password")
        stored["catalog_source_id"] = self.catalog_source.identity()
        stored["next_sync"] = self.next_sync
        stored["last_sync"] = self.engine.last_sync
        stored["next_update"] = self.next_update
        stored["update_source_id"] = self.update_source_id()
        atomic(self.config_file, json.dumps(stored, ensure_ascii=False, indent=2).encode())
        self.saved = data

    def schedule_save(self, *_):
        if not self.settings_loading:
            self.save_timer.start()

    def change_proxy_lock(self, locked):
        self.proxy_lock_button.setText("解除锁定" if locked else "锁定")
        if self.settings_loading:
            return
        self.save_timer.stop()
        try:
            self.save_settings(self.settings())
        except (VPNError, OSError) as error:
            self.settings_loading = True
            self.proxy_lock_button.setChecked(self.saved.get("proxy_settings_locked", False))
            self.settings_loading = False
            self.statusBar().showMessage(f"未能保存锁定设置：{error}", 5000)
        else:
            self.statusBar().showMessage("代理配置已锁定" if locked else "代理配置已解除锁定", 3500)

    def persist_edits(self):
        try:
            self.save_settings(self.settings())
            self.statusBar().showMessage("设置已自动保存", 3500)
        except (VPNError, OSError) as e:
            self.statusBar().showMessage(f"未保存设置：{e}", 5000)

    def with_settings(self, title, fn, *, require_source=True):
        if self.job:
            return
        if self.source_error:
            QMessageBox.warning(self, "检查目录来源", self.source_error)
            return
        if require_source and not self.catalog_source.configured:
            QMessageBox.information(self, "配置目录来源", "请先在「高级模式」填写或导入 VXP 链接和密钥。")
            return
        self.save_timer.stop()
        try:
            data = self.settings()
            self.save_settings(data)
        except (VPNError, OSError) as e:
            QMessageBox.warning(self, "检查设置", str(e))
            return
        self.run_job(title, lambda: fn(data))

    def startup(self):
        if self.engine.own.journal.exists():
            self.run_job("恢复上次会话", self.engine.close)
        else:
            self.tick()

    def tick(self):
        if self.job or self.update_prompt or self.exit_action or self.subscription_prompt is not None:
            return
        now = time.monotonic()
        if self.engine.current and self.next_health is not None and now >= self.next_health:
            self.next_health = now + HEALTH_INTERVAL
            self.run_job("后台检测当前节点", self.engine.check_health, silent=True)
        elif self.engine.current and self.next_backups is not None and now >= self.next_backups:
            self.next_backups = now + BACKUP_INTERVAL
            self.run_job("后台检查备用节点", self.engine.refresh_backups, silent=True)
        elif self.source_error:
            return
        elif self.install_root and self.catalog_source.update_url and time.time() >= self.next_update:
            self.check_updates()
        elif self.catalog_source.configured and time.time() >= self.next_sync:
            data = self.saved.copy()
            self.run_job("定时更新订阅", lambda: self.engine.refresh(data, background=True), silent=True)

    def schedule_health_checks(self):
        current = self.engine.current
        if not current:
            self.monitored = self.next_health = self.next_backups = None
        elif current is not self.monitored:
            old = self.monitored
            self.monitored = current
            now = time.monotonic()
            self.next_health = now + HEALTH_INTERVAL
            if old is None:
                self.next_backups = now + BACKUP_INTERVAL

    def set_controls(self, busy):
        active = bool(self.engine.current)
        recovery = self.engine.own.journal.exists()
        self.auto_button.setVisible(not active)
        self.stop_button.setVisible(active or recovery)
        self.bad_button.setVisible(active)
        self.restart_button.setVisible(active)
        self.bad_button.setEnabled(not busy and bool(self.engine.available_backups()))
        cancellable = busy and self.job_title in (
            "一键启动", "连接测试", "切换为备用节点", "重启代理", "测试并切换节点",
            "从本地目录启动", "应用所选代理")
        self.cancel_button.setVisible(cancellable)
        self.cancel_button.setEnabled(cancellable)
        for item in (self.auto_button, self.stop_button, self.restart_button):
            item.setEnabled(not busy)
        if busy and self.silent_job:
            self.stop_button.setEnabled(self.exit_action is None)
        if hasattr(self, "update_button"):
            self.update_button.setEnabled(not busy)
        for item in (self.vendor, self.group):
            item.setEnabled(not busy and bool(self.engine.profiles))
        for name in ("settings_form", "catalog_editor"):
            if hasattr(self, name):
                getattr(self, name).setEnabled(not busy)

    def run_job(self, title, fn, *, silent=False):
        if self.job:
            return
        self.job_title = title
        self.silent_job = silent
        self.events.take(logs=False)
        self.engine.cancel.clear()
        self.old_sync = self.engine.last_sync
        self.job_error = None
        self.events.line(f"{title} · 开始")
        if not silent:
            self.detail.setText(f"{title}进行中… 可展开日志查看进度")
        self.job = Job(fn, self)
        self.job.result.connect(self.set_error)
        self.job.finished.connect(self.finished)
        self.progress_timer.stop()
        if not silent:
            self.show_progress(f"{title}进行中", 0, 0)
        self.set_controls(True)
        self.job.start()

    @Slot(str, int, int)
    def show_progress(self, stage, completed, total):
        if not self.job or self.silent_job:
            return
        self.progress_bar.setRange(0, total)
        if total:
            self.progress_bar.setValue(completed)
        self.progress_label.setText(f"{stage} · {completed}/{total}" if total else stage)
        self.progress_panel.show()

    def cancel_job(self):
        self.engine.cancel.set()
        self.cancel_button.setEnabled(False)
        self.detail.setText("正在取消，等待已发出的网络请求结束…")
        self.show_progress("正在取消，等待网络请求结束", 0, 0)
        self.events.line("已请求取消当前操作")

    @Slot(object)
    def set_error(self, error):
        self.job_error = error

    @Slot()
    def finished(self):
        fallback = self.job.download_failed and self.job_title in ("一键启动", "重启代理")
        self.job.deleteLater()
        self.job = None
        error = self.job_error
        self.last_job_ok = not bool(error)
        if error or self.silent_job:
            self.progress_panel.hide()
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.progress_label.setText(f"{self.job_title}完成")
            self.progress_timer.start(1600)
        imported = self.engine.imported_settings
        self.engine.imported_settings = {}
        ai_config = self.engine.imported_ai
        self.engine.imported_ai = None
        if ai_config is not None and ai_config != (self.ai_key, self.ai_model):
            try:
                save_ai_config(self.config_file.parent, self.catalog_source.identity(), *ai_config)
                self.ai_key, self.ai_model = ai_config
                if self.catalog_stack.currentIndex() == 1:
                    self.catalog_editor.ai_key.setText(self.ai_key)
                    self.catalog_editor.ai_model.set_address(self.ai_model)
            except Exception:
                self.events.line("无法安全保存 VXP 中的 AI 凭据")
        if imported and not self.saved.get("proxy_settings_locked"):
            self.saved = dict(self.saved, **imported)
            self.apply_fields(self.saved)
            self.events.line("已应用加密目录中的软件配置")
        if self.engine.last_sync != self.old_sync:
            self.next_sync = time.time() + 3600*self.saved["hours"]
        elif self.job_title == "定时更新订阅":
            self.next_sync = time.time() + min(3600, 3600*self.saved["hours"])
        try:
            self.save_settings(self.saved)
        except OSError:
            self.events.line("无法保存任务时间")
        self.schedule_health_checks()
        self.fill_profiles()
        self.set_controls(False)
        self.events.line(f"{self.job_title} · " + (f"失败：{error}" if error else "完成"))
        current = self.engine.current
        if current:
            status = "当前节点不可用，等待备用" if self.engine.health_failures >= 2 else "代理运行中"
            self.status.setText(f"{status} · {current.node.name}")
            summary = f"可用备用节点 {len(self.engine.available_backups())} 个"
            if self.engine.pending_profiles is not None:
                summary += " · 已缓存新订阅，重启代理后使用"
            self.detail.setText(f"{current.vendor.name}  /  {current.group}　•　{summary}")
        elif self.engine.own.journal.exists():
            self.status.setText("需要恢复代理设置")
            self.detail.setText("点击关闭以重试；详细信息见日志。")
        elif not self.silent_job:
            self.status.setText("代理尚未启动")
            self.detail.setText(f"{self.job_title}已完成" if not error else "可展开日志查看执行过程")
        warnings = [self.source_error] if self.source_error else []
        if error and not self.silent_job:
            warnings.append(error)
        if self.engine.failures and not self.silent_job:
            names = "、".join(self.engine.failures[:5])
            more = f" 等 {len(self.engine.failures)} 项" if len(self.engine.failures) > 5 else ""
            warnings.append(f"更新或选组失败：{names}{more}")
        self.warning.setText("　".join(warnings))
        self.warning.setVisible(bool(warnings))
        if not self.silent_job:
            self.statusBar().showMessage(f"{self.job_title}失败：{error}" if error else f"{self.job_title}完成", 10000)
        if self.exit_action:
            action, self.exit_action = self.exit_action, None
            if self.job_title == "关闭代理并退出" and error:
                self.show_window()
                QMessageBox.warning(self, "未能恢复代理", error)
            else:
                QTimer.singleShot(0, lambda: self.finish_exit(action))
        elif self.job_title == "检查软件更新":
            QTimer.singleShot(0, lambda: self.update_finished(error))
        elif self.job_title == "定时更新订阅" and not error:
            QTimer.singleShot(0, self.offer_subscription_restart)
        elif fallback:
            QTimer.singleShot(0, self.import_catalog)

    def offer_subscription_restart(self):
        revision = self.engine.pending_revision
        if (not self.engine.current or self.engine.pending_profiles is None or self.job or self.exit_action
                or self.subscription_prompt is not None or revision == self.prompted_subscription_revision):
            return
        self.prompted_subscription_revision = revision
        prompt = QMessageBox(self)
        self.subscription_prompt = prompt
        prompt.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        prompt.setWindowTitle("订阅更新已下载")
        prompt.setText("订阅、节点或配置有变化。是否现在重启代理并应用已下载的内容？")
        prompt.setInformativeText("重启会短暂中断连接。选择稍后会继续使用当前代理，可在主页点击「重启」应用。")
        apply_now = prompt.addButton("现在重启", QMessageBox.ButtonRole.AcceptRole)
        later = prompt.addButton("稍后", QMessageBox.ButtonRole.RejectRole)
        prompt.setDefaultButton(later)
        prompt.setEscapeButton(later)

        def decided(_):
            self.subscription_prompt = None
            if (prompt.clickedButton() is apply_now and self.engine.pending_profiles is not None
                    and self.engine.current and revision == self.engine.pending_revision):
                self.with_settings("应用已下载的订阅", self.engine.apply_pending, require_source=False)

        prompt.finished.connect(decided)
        self.show_window()
        prompt.open()

    def fill_profiles(self):
        selected = self.vendor.currentData()
        group = self.group.currentData()
        self.vendor.blockSignals(True)
        self.vendor.clear()
        self.vendor.addItem("所有厂商 · 排名", None)
        for p in self.engine.profiles:
            self.vendor.addItem(p.vendor.name, p.vendor.id)
        self.vendor.setCurrentIndex(max(0, self.vendor.findData(selected)))
        self.vendor.blockSignals(False)
        self.fill_groups(preferred=group)

    def fill_groups(self, *_args, preferred=None):
        self.group.blockSignals(True)
        self.group.clear()
        for p in self.engine.profiles:
            if p.vendor.id == self.vendor.currentData():
                for name, ns in p.groups.items():
                    self.group.addItem(f"{name} ({len(ns)})", name)
        if not self.group.count():
            self.group.addItem("各厂商首组选优", None)
        ix = self.group.findData(preferred)
        self.group.setCurrentIndex(max(0, ix))
        self.group.blockSignals(False)
        self.fill_table()

    def fill_table(self, *_):
        if self.job:
            return
        selected_rows = self.table.selectionModel().selectedRows()
        selected = self.node_table.candidate(selected_rows[0].row()) if selected_rows else None
        chosen = self.choice()
        current = self.engine.current
        backups = {c.identity: c for c in self.engine.available_backups()}
        all_nodes = {c.identity: c for c in self.engine.list_candidates(self.engine.profiles)}
        entries = {c.identity: c for c in self.engine.results if c.identity in all_nodes}
        for identity, c in all_nodes.items():
            entries.setdefault(identity, c)
        entries.update(backups)
        if current:
            entries[current.identity] = current
        candidates = [c for c in entries.values() if not chosen or c.identity[:2] == chosen]
        candidates.sort(key=lambda c: 0 if current and c.identity == current.identity else
                        1 if c.identity in backups else 2)
        self.empty_nodes.setVisible(not candidates)
        scroll = self.table.verticalScrollBar()
        offset = scroll.value()
        self.node_table.replace(candidates, current, set(backups))
        if selected:
            for row, c in enumerate(candidates):
                if c.identity == selected.identity:
                    self.table.selectRow(row)
                    break
        scroll.setValue(offset)

    def choice(self):
        vendor, group = self.vendor.currentData(), self.group.currentData()
        return (vendor, group) if vendor and group else None

    def manual_measure(self):
        choice = self.choice()
        self.with_settings("连接测试", lambda data: self.engine.measure(data, choice), require_source=False)

    def node_menu(self, position):
        if self.job or not self.engine.profiles:
            return
        row = self.table.rowAt(position.y())
        if row >= 0:
            self.table.selectRow(row)
        else:
            self.table.clearSelection()
        menu = QMenu(self)
        menu.addAction("测试连接", self.manual_measure)
        menu.addAction("使用所选组 / 节点", self.manual_use)
        menu.exec(self.table.viewport().mapToGlobal(position))

    def manual_use(self, row=None):
        indexes = self.table.selectionModel().selectedRows()
        if row is None or isinstance(row, bool):
            row = indexes[0].row() if indexes else None
        candidate = self.node_table.candidate(row)
        if candidate:
            self.with_settings("测试并切换节点", lambda s: self.engine.test_and_use(s, candidate), require_source=False)
        else:
            choice = self.choice()
            self.with_settings("应用所选代理", lambda s: self.engine.activate(s, choice), require_source=False)

    def import_catalog(self):
        path, _ = QFileDialog.getOpenFileName(self, "下载失败，请导入本地加密目录", "",
                                               "VPN-X 目录 (*.vxp)")
        if path:
            self.with_settings("从本地目录启动", lambda s: self.engine.auto(s, path))

    def setup_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("VPN-X 自动代理")
        menu = QMenu(self)
        menu.addAction("打开 VPN-X", self.show_window)
        menu.addAction("关闭代理并退出", self.request_exit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_window()
                                    if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                                                  QSystemTrayIcon.ActivationReason.DoubleClick) else None)
        self.tray.show()

    def show_window(self):
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def request_exit(self):
        self.finish_exit("quit")

    def request_stop(self):
        self.finish_exit("stop")

    def finish_exit(self, action):
        if self.job:
            self.exit_action = action
            self.stop_button.setEnabled(False)
            self.cancel_job()
            return
        if action == "stop":
            self.run_job("关闭代理", self.engine.close)
            return
        if self.save_timer.isActive():
            self.save_timer.stop()
            self.persist_edits()
        if self.engine.own.journal.exists() or self.engine.own.headless:
            self.exit_action = action
            self.run_job("关闭代理并退出", self.engine.shutdown)
            return
        if self.ai_dialog is not None and self.ai_dialog.worker is not None:
            if not self.waiting_for_ai_exit:
                self.waiting_for_ai_exit = True
                self.exit_action = action
                self.statusBar().showMessage("代理已关闭，正在等待 AI 请求结束…")
                self.ai_dialog.worker.finished.connect(self.resume_exit_after_ai)
            return
        if action == "update":
            try:
                subprocess.Popen([sys.executable, "--apply-update", str(os.getpid())],
                                 cwd=Path(sys.executable).parent)
            except OSError as err:
                self.show_window()
                detail = f"启动更新程序失败：{updates.failure_detail(err)}"
                self.events.line(detail)
                QMessageBox.warning(self, "无法更新", f"{detail}\n\n可在代理设置中重新检查更新。")
                return
        if self.tray:
            self.tray.hide()
        if self.ai_dialog is not None:
            self.ai_dialog.hide()
        QApplication.instance().quit()

    @Slot()
    def resume_exit_after_ai(self):
        self.waiting_for_ai_exit = False
        action, self.exit_action = self.exit_action, None
        self.finish_exit(action)

    def update_source_id(self):
        return hashlib.sha256(self.catalog_source.update_url.encode()).hexdigest()

    def check_updates(self, *, manual=False):
        if self.job or self.update_prompt:
            return
        address = self.catalog_source.update_url
        if not self.install_root or not address:
            if manual:
                QMessageBox.information(self, "检查软件更新", "请使用安装版，并在高级模式设置 HTTPS 更新清单链接。")
            return
        try:
            if updates.current_version(self.install_root) != APP_VERSION:
                return  # The updater has not yet committed this newly started version.
        except (OSError, ValueError, VPNError):
            self.next_update = time.time() + 86400
            if manual:
                QMessageBox.warning(self, "检查软件更新", "无法读取当前安装版本，请检查安装目录。")
            return
        self.next_update = time.time() + 86400
        self.pending_update = None
        self.update_manual = manual
        self.update_prompt = True
        def run():
            self.pending_update = updates.check(self.install_root, address, self.events.progress,
                                                self.engine.cancelled)
        self.run_job("检查软件更新", run)

    def update_finished(self, error):
        retry = install = False
        if error:
            self.show_window()
            retry = QMessageBox.question(self, "更新下载失败", f"{error}\n\n是否重试？",
                                         defaultButton=QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        elif self.pending_update:
            self.show_window()
            prompt = QMessageBox(self)
            prompt.setWindowTitle("更新已就绪")
            prompt.setText(f"V{self.pending_update.version} 已下载并通过校验。\n\n现在关闭代理、安装更新并重启 VPN-X？")
            now = prompt.addButton("现在更新", QMessageBox.ButtonRole.AcceptRole)
            prompt.setDefaultButton(prompt.addButton("稍后", QMessageBox.ButtonRole.RejectRole))
            prompt.exec()
            install = prompt.clickedButton() is now
        elif self.update_manual:
            QMessageBox.information(self, "检查软件更新", "当前已经是最新版本。")
        self.update_prompt = False
        if install:
            self.finish_exit("update")
        elif retry:
            self.check_updates(manual=True)

    def closeEvent(self, event):
        event.ignore()
        if self.tray:
            self.hide()
        else:
            self.showMinimized()


def migrate_data(data_dir):
    for old_name, new_name in (("settings.json", "settings.json"), ("restore.json.dpapi", "restore.json.dpapi")):
        old = LEGACY_DATA / old_name
        new = data_dir / new_name
        if old != new and old.is_file() and not new.exists():
            atomic(new, old.read_bytes())
            if old_name == "restore.json.dpapi":
                old.unlink()


def run_window(app, data_dir, *, check_startup=False, install_root=None, health=None, update_failure=None):
    lock_dir = data_dir if check_startup else LEGACY_DATA
    lock_dir.mkdir(parents=True, exist_ok=True)
    channel = "VPN-X-" + hashlib.sha256(str(lock_dir).lower().encode()).hexdigest()[:24]
    lock = QLockFile(str(lock_dir / "vpn-x.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        socket = QLocalSocket()
        socket.connectToServer(channel)
        return 0 if socket.waitForConnected(1000) and not health else 1
    try:
        if not check_startup:
            try:
                data_dir.mkdir(exist_ok=True, parents=True)
                migrate_data(data_dir)
            except (OSError, VPNError) as err:
                if not health:
                    QMessageBox.warning(None, "VPN-X", f"无法写入数据目录：{err}")
                return 1
        app.setQuitOnLastWindowClosed(False)
        window = Window(data_dir, start_background=not check_startup, install_root=install_root)
        server = QLocalServer(window)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not check_startup:
            QLocalServer.removeServer(channel)
            server.listen(channel)
            def reopen():
                connection = server.nextPendingConnection()
                if connection:
                    connection.deleteLater()
                window.show_window()
            server.newConnection.connect(reopen)
            def session_end(manager):
                # Do not block Windows waiting on a network task. The DPAPI journal remains
                # available for crash recovery on the next launch if cleanup cannot finish.
                if not window.job:
                    try:
                        window.engine.close()
                    except Exception:
                        pass
            app.commitDataRequest.connect(session_end)
        if check_startup:
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            QTimer.singleShot(0, lambda: app.exit(0))
        elif health:
            QTimer.singleShot(0, lambda: atomic(health, b"ready"))
        elif update_failure:
            window.events.line(f"软件更新失败 · {update_failure}")

            def show_update_failure():
                prompt = QMessageBox(QMessageBox.Icon.Warning, "软件更新失败", "", parent=window)
                prompt.setTextFormat(Qt.TextFormat.PlainText)
                prompt.setText(f"{update_failure}\n\n已保留原版本。可在代理设置中重新检查更新。")
                prompt.exec()

            QTimer.singleShot(0, show_update_failure)
        window.show()
        if install_root and not check_startup:
            Thread(target=updates.maintain_installation, args=(install_root, APP_VERSION, window.events.line),
                   daemon=True).start()
        return app.exec()
    finally:
        lock.unlock()


def main():
    root = updates.installed_root()
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "--apply-update" and root and args[1].isdigit():
        def notify_failure(detail):
            ctypes.windll.user32.MessageBoxW(None, f"{detail}\n\n请关闭 VPN-X 后从开始菜单重新打开。",
                                              "软件更新失败", 0x10030)

        return updates.apply_pending(root, int(args[1]), notify_failure)
    if sys.argv[1:] == ["--check-clash"]:
        with Bench(find_verge(), lambda *_: None) as runtime:
            return 0 if runtime.core.call("GET", "/version").get("meta") else 1
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    assets = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    logo = assets / "logo.ico"
    icon = QIcon(str(logo)) if getattr(sys, "frozen", False) else application_icon(logo)
    app.setWindowIcon(icon)
    app.setStyle("Fusion")
    app.setPalette(light_palette())
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE)
    if sys.argv[1:] == ["--check-startup"]:
        if icon.pixmap(32, 32).isNull() or not GUIDE.is_file():
            return 1
        # Exercise the real lock and window startup with isolated settings and no background jobs.
        with TemporaryDirectory(prefix="vpnx-startup-") as folder:
            return run_window(app, Path(folder), check_startup=True)
    if not ctypes.windll.shell32.IsUserAnAdmin():
        target = sys.executable
        args = sys.argv[1:] if getattr(sys, "frozen", False) else [str(Path(__file__).resolve()), *sys.argv[1:]]
        launch = ctypes.windll.shell32.ShellExecuteW
        launch.restype = ctypes.c_ssize_t
        if launch(None, "runas", target, subprocess.list2cmdline(args), str(Path(__file__).parent), 1) <= 32:
            QMessageBox.warning(None, "需要管理员权限", "请允许 Windows 提权，以便控制 Clash Verge 和虚拟网卡。")
            return 1
        return 0
    health = None
    update_failure = None
    if root and len(args) == 2 and args[0] == "--update-health" and re.fullmatch(r"[a-f0-9]{24}", args[1]):
        health = root / "updates" / ("health-" + args[1])
    if root and args[:1] == ["--update-failed"]:
        update_failure = args[1] if len(args) == 2 and args[1] else "旧版更新程序未记录失败原因。"
    return run_window(app, LEGACY_DATA if root else DATA, install_root=root, health=health,
                      update_failure=update_failure)


if __name__ == "__main__":
    sys.exit(main())
