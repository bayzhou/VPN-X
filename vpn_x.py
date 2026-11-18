"""VPN-X Windows desktop application."""
from __future__ import annotations

import ctypes
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import traceback

from PySide6.QtCore import QObject, QLockFile, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QTabWidget, QTableWidget, QTableWidgetItem,
    QStackedWidget, QVBoxLayout, QWidget,
)

from app_info import APP_NAME, APP_VERSION, RELEASE_NAME
from automation import DEFAULTS, Engine
from catalog import (
    CLOUDFLARE_CHECK, DEFAULT_CHECK, PUBLIC_SETTINGS, SOURCE_FILENAME,
    CatalogDownloadError, CatalogSource, VPNError, atomic, catalog_settings, check_page_password, encrypt,
    page_password_hash,
)
from verge import Bench, DATA, LEGACY_DATA, ensure_verge, find_verge

STYLE = """
QMainWindow, QWidget#mainSurface, QDialog { background:#ffffff; }
QWidget { color:#202321; }
QLabel[role="headline"] { font-size:23px; font-weight:700; }
QLabel[role="section"] { font-size:16px; font-weight:650; }
QLabel[role="muted"] { color:#575f59; }
QLabel[role="warning"] { color:#a02d25; }
QFrame#connectionCard, QFrame#settingsCard, QFrame#passwordCard {
    background:#f7f8f7; border:1px solid #e4e8e5; border-radius:18px;
}
QPushButton {
    background:#fff; border:1px solid #dce1dd; border-radius:16px;
    padding:5px 16px; min-height:20px; font-weight:600;
}
QPushButton:hover { background:#f3f5f3; border-color:#bfc9c1; }
QPushButton:pressed { background:#e8eee9; }
QPushButton:focus { border-color:#438064; }
QPushButton[primary="true"] { background:#232925; color:#fff; border-color:#232925; min-width:74px; }
QPushButton[primary="true"]:hover { background:#36453b; }
QPushButton[primary="true"]:pressed { background:#17251c; }
QPushButton[primary="true"]:focus { border-color:#6f9b80; }
QPushButton:disabled, QPushButton[primary="true"]:disabled {
    background:#eef1ee; color:#727971; border-color:#e2e6e2;
}
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit {
    background:#fff; border:1px solid #dce1dd; border-radius:10px;
    padding:5px 9px; min-height:21px; selection-background-color:#dcebe0; selection-color:#202321;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border-color:#438064; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { background:#f1f3f1; color:#676e68; }
QComboBox { padding-right:28px; }
QComboBox::drop-down {
    subcontrol-origin:padding; subcontrol-position:top right; width:25px; border:0;
}
QComboBox::down-arrow { image:url(:/qt-project.org/styles/commonstyle/images/arrow-down-16.png);
    width:11px; height:11px; }
QComboBox QAbstractItemView { background:#fff; border:1px solid #dce1dd; outline:0;
    selection-background-color:#e7efe8; selection-color:#202321; }
QSpinBox { padding-right:27px; }
QSpinBox::up-button, QSpinBox::down-button { subcontrol-origin:padding; width:23px; border:0; }
QSpinBox::up-button { subcontrol-position:top right; }
QSpinBox::down-button { subcontrol-position:bottom right; }
QSpinBox::up-arrow { image:url(:/qt-project.org/styles/commonstyle/images/arrow-up-16.png);
    width:9px; height:9px; }
QSpinBox::down-arrow { image:url(:/qt-project.org/styles/commonstyle/images/arrow-down-16.png);
    width:9px; height:9px; }
QTabWidget::pane { border:0; background:#fff; }
QTabBar::tab {
    background:transparent; border:1px solid transparent; border-radius:16px;
    color:#575f59; margin-right:5px; padding:6px 14px; min-height:19px;
}
QTabBar::tab:hover { background:#f4f6f4; }
QTabBar::tab:selected { background:#eaf0eb; color:#202321; font-weight:650; }
QTabBar::tab:focus { border-color:#94aa99; }
QTableWidget {
    background:#fff; alternate-background-color:#fbfcfb; border:1px solid #e4e8e5;
    border-radius:14px; outline:0; selection-background-color:#e7efe8; selection-color:#202321;
}
QHeaderView { background:#fff; }
QHeaderView::section {
    background:#f6f8f6; border:0; border-bottom:1px solid #e4e8e5;
    color:#535b55; padding:9px 10px; font-weight:600;
}
QHeaderView::section:first { border-top-left-radius:12px; }
QHeaderView::section:last { border-top-right-radius:12px; }
QTableWidget::item { border-bottom:1px solid #edf0ed; padding-left:9px; }
QStatusBar { background:#fff; color:#575f59; border-top:1px solid #edf0ed; }
QStatusBar::item { border:0; }
QProgressBar { background:#e6ece8; border:0; border-radius:4px; }
QProgressBar::chunk { background:#438064; border-radius:4px; }
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
        self.key_visibility = button("显示", self.toggle_key)
        key_row = QHBoxLayout()
        key_row.addWidget(self.source_key, 1)
        key_row.addWidget(self.key_visibility)
        form.addRow("VXP 链接", self.source_url)
        form.addRow("VXP 密钥", key_row)
        source_layout.addLayout(form)
        actions = QHBoxLayout()
        actions.addWidget(button("保存来源", self.save_source))
        actions.addWidget(button("导入来源", self.import_source))
        actions.addWidget(button("导出来源", self.export_source))
        actions.addStretch()
        source_layout.addLayout(actions)
        source_layout.addWidget(banner(f"将 {SOURCE_FILENAME} 与 {RELEASE_NAME}.exe 放在同一目录即可自动读取。", "muted"))
        box.addWidget(source_panel)
        box.addWidget(banner("制作加密目录", "section"))
        self.rows = QTableWidget(1, 2)
        self.rows.setShowGrid(False)
        self.rows.verticalHeader().setDefaultSectionSize(38)
        self.rows.setHorizontalHeaderLabels(["VPN 厂商", "HTTPS 订阅链接"])
        self.rows.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.rows.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.rows.setColumnWidth(0, 150)
        box.addWidget(self.rows, 1)
        bar = QHBoxLayout()
        bar.addWidget(button("添加厂商", lambda: self.rows.insertRow(self.rows.rowCount())))
        bar.addWidget(button("删除当前行", self.remove))
        bar.addStretch()
        bar.addWidget(button("修改进入密码", parent.change_page_password))
        bar.addWidget(button("导出 .vxp", self.save, True))
        box.addLayout(bar)
        box.addWidget(banner("导出文件会包含订阅和当前软件配置。", "muted"))

    def show_source(self, source):
        self.source_url.setText(source.url)
        self.source_key.setText(source.key)
        self.hide_key()

    def current_source(self):
        return CatalogSource(self.source_url.text().strip(), self.source_key.text())

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
        rows = []
        for row in range(self.rows.rowCount()):
            parts = [self.rows.item(row, col).text().strip() if self.rows.item(row, col) else ""
                     for col in range(2)]
            if any(parts):
                rows.append(dict(zip(("name", "url"), parts)))
        try:
            source = self.current_source()
            if not source.configured:
                raise VPNError("请先填写 VXP 链接和密钥")
            config = self.window.settings()
            data = encrypt(rows, source.key, {key: config[key] for key in PUBLIC_SETTINGS})
            path, _ = QFileDialog.getSaveFileName(self, "保存加密目录", "catalog.vxp", "VPN-X 目录 (*.vxp)")
            if not path:
                return
            atomic(path if path.lower().endswith(".vxp") else path+".vxp", data)
            self.window.events.line.emit(f"已制作并保存加密目录（{len(rows)} 家厂商）")
            self.window.statusBar().showMessage("已导出加密目录", 5000)
        except (VPNError, OSError) as e:
            QMessageBox.warning(self, "无法导出", str(e) if isinstance(e, VPNError) else "无法写入文件")
            return


class Events(QObject):
    line = Signal(str)
    progress = Signal(str, int, int)


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
    def __init__(self, data_dir=DATA, *, start_background=True):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} V{APP_VERSION} · 自动代理")
        self.resize(1040, 725)
        self.setMinimumSize(760, 590)
        self.config_file = data_dir / "settings.json"
        self.source_file = data_dir / SOURCE_FILENAME
        self.catalog_source = CatalogSource()
        self.source_error = ""
        self.events = Events()
        self.events.line.connect(self.write_log)
        self.events.progress.connect(self.show_progress)
        self.engine = Engine(self.events.line.emit, self.events.progress.emit)
        self.job = None
        self.next_sync = 0
        self.should_close = False
        self.fields = {}
        self.table_results = {}
        self.page_hash = ""
        self.settings_loading = False
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(450)
        self.save_timer.timeout.connect(self.persist_edits)
        self.build()
        self.load_catalog_source()
        self.load_settings()
        self.timer = QTimer(self)
        self.timer.setInterval(30_000)
        self.timer.timeout.connect(self.tick)
        if start_background:
            self.timer.start()
            QTimer.singleShot(0, self.startup)

    def build(self):
        panel = QWidget()
        panel.setObjectName("mainSurface")
        self.setCentralWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(22, 16, 22, 12)
        layout.setSpacing(12)
        self.log_button = button("显示日志", self.toggle_log)
        self.statusBar().addPermanentWidget(self.log_button)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self.dashboard(), "代理控制")
        self.tabs.addTab(self.settings_page(), "设置")
        self.tabs.addTab(self.catalog_page(), "高级模式")
        self.tabs.currentChanged.connect(self.lock_catalog_page)
        layout.addWidget(self.tabs, 1)
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
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("执行步骤将在这里显示。")
        self.log_view.setMaximumBlockCount(70000)
        self.log_view.setFixedHeight(158)
        self.log_view.hide()
        layout.addWidget(self.log_view)

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
        self.detail = banner("初次使用请先一键配置，再一键启动。", "muted")
        self.detail.setWordWrap(True)
        self.warning = banner("", "warning")
        self.warning.setWordWrap(True)
        self.warning.hide()
        title_row = QHBoxLayout()
        title_row.setSpacing(9)
        icon = QApplication.windowIcon()
        if not icon.isNull():
            mark = QLabel()
            mark.setPixmap(icon.pixmap(33, 33))
            mark.setFixedSize(33, 33)
            title_row.addWidget(mark)
        title_row.addWidget(self.status, 1)
        content.addLayout(title_row)
        content.addWidget(self.detail)
        content.addWidget(self.warning)
        row = QHBoxLayout()
        row.setContentsMargins(0, 5, 0, 0)
        self.configure_button = button("一键配置", self.one_click_configure)
        self.auto_button = button("一键启动", lambda: self.with_settings("一键启动", self.engine.auto), True)
        self.stop_button = button("关闭", lambda: self.run_job("关闭代理", self.engine.close))
        self.bad_button = button("这个节点不可用", lambda: self.run_job("切换下一节点", self.engine.next_node))
        self.restart_button = button("重启", lambda: self.with_settings("重启代理", self.engine.restart), True)
        self.cancel_button = button("取消操作", self.cancel_job)
        for b in (self.configure_button, self.auto_button, self.stop_button, self.bad_button, self.restart_button):
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
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["节点", "厂商 / 代理组", "剩余流量", "连接延迟", "连接"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.verticalHeader().hide()
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.node_menu)
        self.table.cellDoubleClicked.connect(lambda *_: self.manual_use())
        self.table.setToolTip("双击节点可启用，右键可测试连接或启用所选代理组")
        self.empty_nodes = banner("暂无节点记录", "muted")
        self.empty_nodes.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        empty_layout = QVBoxLayout(self.table.viewport())
        empty_layout.addWidget(self.empty_nodes, alignment=Qt.AlignmentFlag.AlignCenter)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in range(2, self.table.columnCount()):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
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
        card_layout.addWidget(banner("代理配置", "section"))
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
        box.addWidget(self.settings_form)
        regions.textEdited.connect(self.schedule_save)
        hours.valueChanged.connect(self.schedule_save)
        net.currentIndexChanged.connect(self.schedule_save)
        mode.currentIndexChanged.connect(self.schedule_save)
        self.fields["check"].presets.currentIndexChanged.connect(self.schedule_save)
        self.fields["check"].custom.textEdited.connect(self.schedule_save)
        box.addWidget(banner("更改后自动保存到程序目录。", "muted"))
        box.addStretch()
        return page

    def catalog_page(self):
        self.catalog_stack = QStackedWidget()
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

    def lock_catalog_page(self, index):
        if index != 2:
            self.catalog_stack.setCurrentIndex(0)
            self.page_password.clear()
            self.page_error.clear()
            self.catalog_editor.hide_key()

    def unlock_catalog_page(self):
        password = self.page_password.text()
        self.page_password.clear()
        if not check_page_password(password, self.page_hash):
            self.page_error.setText("密码不正确")
            return
        self.page_error.clear()
        table = self.catalog_editor.rows
        if table.rowCount() == 1 and all(table.item(0, col) is None for col in range(2)):
            for index, vendor in enumerate(self.engine.catalog or []):
                if index:
                    table.insertRow(index)
                table.setItem(index, 0, QTableWidgetItem(vendor.name))
                table.setItem(index, 1, QTableWidgetItem(vendor.url))
        self.catalog_stack.setCurrentIndex(1)

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

    def write_log(self, line):
        # Defensively redact links even if a node or vendor chose a URL as its name.
        clean = re.sub(r"(?i)https?://[^\s，；]+", "[链接]", str(line))
        self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {clean}")

    def toggle_log(self):
        show = self.log_view.isHidden()
        self.log_view.setVisible(show)
        self.log_button.setText("隐藏日志" if show else "显示日志")

    def load_catalog_source(self):
        try:
            if self.source_file.exists():
                self.catalog_source = CatalogSource.load(self.source_file)
            else:
                self.catalog_source.save(self.source_file)
        except (VPNError, OSError) as error:
            reason = str(error) if isinstance(error, VPNError) else "无法读取或写入文件"
            self.source_error = f"{SOURCE_FILENAME} 无法使用：{reason}。请在「高级模式」检查目录来源。"
            self.warning.setText(self.source_error)
            self.warning.show()
            self.events.line.emit(self.source_error)
        self.catalog_editor.show_source(self.catalog_source)
        if not self.source_error and not self.catalog_source.configured:
            self.detail.setText("请先在「高级模式」填写或导入目录来源。")

    def apply_catalog_source(self, source):
        source.save(self.source_file)
        if source != self.catalog_source or self.source_error:
            self.next_sync = 0
        if self.source_error:
            self.warning.hide()
        self.source_error = ""
        self.catalog_source = source
        self.saved.update(source.settings())
        self.catalog_editor.show_source(source)
        if not self.engine.current:
            self.detail.setText("目录来源已保存，可一键配置或启动。" if source.configured else
                                "请先在「高级模式」填写或导入目录来源。")
        self.statusBar().showMessage("目录来源已保存", 5000)
        self.events.line.emit("已保存目录来源")

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
                if saved.get("catalog_source_id") == self.catalog_source.identity():
                    self.next_sync = float(saved.get("next_sync", 0))
                self.engine.last_sync = saved.get("last_sync")
            except Exception:
                self.events.line.emit("部分设置无法读取，已使用默认值")
        self.apply_fields(data)
        self.saved = data

    def apply_fields(self, data):
        self.settings_loading = True
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
        return dict(catalog_settings(result), **self.catalog_source.settings())

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
        atomic(self.config_file, json.dumps(stored, ensure_ascii=False, indent=2).encode())
        self.saved = data

    def schedule_save(self, *_):
        if not self.settings_loading:
            self.save_timer.start()

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
        if (self.job or self.source_error
                or time.time() < self.next_sync):
            return
        self.run_job("定时更新订阅", lambda: self.engine.refresh(self.saved))

    def set_controls(self, busy):
        active = bool(self.engine.current)
        recovery = self.engine.own.journal.exists()
        self.configure_button.setVisible(not active)
        self.auto_button.setVisible(not active)
        self.stop_button.setVisible(active or recovery)
        self.bad_button.setVisible(active and self.engine.current.verified)
        self.restart_button.setVisible(active)
        self.bad_button.setEnabled(not busy)
        cancellable = busy and self.job_title in (
            "一键启动", "定时更新订阅", "连接测试", "切换下一节点", "重启代理",
            "导入目录并更新订阅", "应用所选代理")
        self.cancel_button.setVisible(cancellable)
        self.cancel_button.setEnabled(cancellable)
        for item in (self.configure_button, self.auto_button, self.stop_button, self.restart_button):
            item.setEnabled(not busy)
        for item in (self.vendor, self.group):
            item.setEnabled(not busy and bool(self.engine.profiles))
        for name in ("settings_form", "catalog_editor"):
            if hasattr(self, name):
                getattr(self, name).setEnabled(not busy)

    def run_job(self, title, fn):
        if self.job:
            return
        self.job_title = title
        self.engine.cancel.clear()
        self.old_sync = self.engine.last_sync
        self.job_error = None
        self.events.line.emit(f"{title} · 开始")
        self.detail.setText(f"{title}进行中… 可展开日志查看进度")
        self.job = Job(fn, self)
        self.job.result.connect(self.set_error)
        self.job.finished.connect(self.finished)
        self.progress_timer.stop()
        self.show_progress(f"{title}进行中", 0, 0)
        self.set_controls(True)
        self.job.start()

    @Slot(str, int, int)
    def show_progress(self, stage, completed, total):
        if not self.job:
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
        self.events.line.emit("已请求取消当前操作")

    @Slot(object)
    def set_error(self, error):
        self.job_error = error

    @Slot()
    def finished(self):
        fallback = self.job.download_failed and self.job_title == "一键配置"
        self.job.deleteLater()
        self.job = None
        error = self.job_error
        if error:
            self.progress_panel.hide()
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(1)
            self.progress_label.setText(f"{self.job_title}完成")
            self.progress_timer.start(1600)
        imported = self.engine.imported_settings
        self.engine.imported_settings = {}
        if imported:
            self.saved = dict(self.saved, **imported)
            self.apply_fields(self.saved)
            self.events.line.emit("已应用加密目录中的软件配置")
        if self.engine.last_sync != self.old_sync:
            self.next_sync = time.time() + 3600*self.saved["hours"]
        elif self.job_title == "定时更新订阅":
            self.next_sync = time.time() + min(3600, 3600*self.saved["hours"])
        try:
            self.save_settings(self.saved)
        except OSError:
            self.events.line.emit("无法保存任务时间")
        self.fill_profiles()
        self.set_controls(False)
        self.events.line.emit(f"{self.job_title} · " + (f"失败：{error}" if error else "完成"))
        current = self.engine.current
        if current:
            self.status.setText(f"代理运行中 · {current.node.name}")
            summary = f"剩余候选 {len(self.engine.queue)} 个" if current.verified else "手动启用 · 未测试"
            self.detail.setText(f"{current.vendor.name}  /  {current.group}　•　{summary}")
        elif self.engine.own.journal.exists():
            self.status.setText("需要恢复代理设置")
            self.detail.setText("点击关闭以重试；详细信息见日志。")
        else:
            self.status.setText("代理尚未启动")
            self.detail.setText(f"{self.job_title}已完成" if not error else "可展开日志查看执行过程")
        warnings = [self.source_error] if self.source_error else []
        if error:
            warnings.append(error)
        if self.engine.failures:
            names = "、".join(self.engine.failures[:5])
            more = f" 等 {len(self.engine.failures)} 项" if len(self.engine.failures) > 5 else ""
            warnings.append(f"更新或选组失败：{names}{more}")
        self.warning.setText("　".join(warnings))
        self.warning.setVisible(bool(warnings))
        self.statusBar().showMessage(f"{self.job_title}失败：{error}" if error else f"{self.job_title}完成", 10000)
        if fallback:
            QTimer.singleShot(0, self.import_catalog)
        if self.should_close:
            self.should_close = False
            if error:
                QMessageBox.warning(self, "未能恢复代理", error)
            else:
                self.close()

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
        chosen = self.choice()
        from automation import Candidate
        if not chosen:
            candidates = self.engine.results
        else:
            p = next((p for p in self.engine.profiles if p.vendor.id == chosen[0]), None)
            best = [c for c in self.engine.results if c.vendor.id == chosen[0] and c.group == chosen[1]]
            used = {c.node.key for c in best}
            candidates = best + [Candidate(p.vendor, chosen[1], n, p.quota)
                                 for n in p.groups[chosen[1]] if n.key not in used] if p else []
        self.table.setUpdatesEnabled(False)
        self.table.clearContents()
        self.table.setRowCount(len(candidates))
        self.empty_nodes.setVisible(not candidates)
        self.table_results = dict(enumerate(candidates))
        current = self.engine.current
        for row, c in enumerate(candidates):
            selected = current and current.node.key == c.node.key and current.group == c.group
            status = ("使用中" if current.verified else "使用中 · 未测试") if selected else ""
            vals = (c.node.name, f"{c.vendor.name} / {c.group}",
                    f"{c.quota/2**30:.2f} GiB" if c.quota is not None else "未知",
                    f"{c.delay} ms" if c.delay is not None else "—",
                    status)
            for col, value in enumerate(vals):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        self.table.clearSelection()
        self.table.setUpdatesEnabled(True)

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

    def manual_use(self):
        indexes = self.table.selectionModel().selectedRows()
        candidate = self.table_results.get(indexes[0].row()) if indexes else None
        choice = (candidate.vendor.id, candidate.group) if candidate else self.choice()
        node = candidate.node.key if candidate else None
        self.with_settings("应用所选代理", lambda s: self.engine.activate(s, choice, node), require_source=False)

    def import_catalog(self):
        path, _ = QFileDialog.getOpenFileName(self, "下载失败，请导入本地加密目录", "",
                                               "VPN-X 目录 (*.vxp)")
        if path:
            self.with_settings("导入目录并更新订阅", lambda s: self.engine.refresh(s, path))

    def one_click_configure(self):
        def run(settings):
            if self.engine.own.journal.exists():
                self.events.progress.emit("正在关闭代理", 0, 0)
                self.engine.close()
            self.engine.own.stop_headless()
            self.events.progress.emit("启动本地 Clash 内核", 0, 0)
            exe = ensure_verge(self.events.line.emit)
            self.engine.own.prepare(exe)
            self.engine.refresh(settings)
        self.with_settings("一键配置", run)

    def closeEvent(self, event):
        if self.job:
            self.detail.setText("当前操作正在执行，请等任务完成后关闭窗口。")
            event.ignore()
            return
        if self.save_timer.isActive():
            self.save_timer.stop()
            self.persist_edits()
        if self.engine.own.journal.exists():
            event.ignore()
            self.should_close = True
            self.run_job("关闭代理并退出", self.engine.close)
            return
        self.engine.own.stop_headless()
        event.accept()


def migrate_data(data_dir):
    for old_name, new_name in (("settings.json", "settings.json"), ("restore.json.dpapi", "restore.json.dpapi")):
        old = LEGACY_DATA / old_name
        new = data_dir / new_name
        if old.is_file() and not new.exists():
            atomic(new, old.read_bytes())
            if old_name == "restore.json.dpapi":
                old.unlink()


def run_window(app, data_dir, *, check_startup=False):
    lock_dir = data_dir if check_startup else LEGACY_DATA
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_dir / "vpn-x.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "VPN-X", "VPN-X 已在运行。")
        return 1
    try:
        if not check_startup:
            try:
                migrate_data(data_dir)
            except OSError as err:
                QMessageBox.warning(None, "VPN-X", f"无法写入程序目录：{err}")
                return 1
        window = Window(data_dir, start_background=not check_startup)
        if check_startup:
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
            QTimer.singleShot(0, lambda: app.exit(0))
        window.show()
        return app.exec()
    finally:
        lock.unlock()


def main():
    if sys.argv[1:] == ["--check-clash"]:
        with Bench(find_verge(), lambda *_: None) as runtime:
            return 0 if runtime.core.call("GET", "/version").get("meta") else 1
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    assets = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    icon = QIcon(str(assets / "logo.ico"))
    app.setWindowIcon(icon)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(STYLE)
    if sys.argv[1:] == ["--check-startup"]:
        if icon.pixmap(32, 32).isNull():
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
    return run_window(app, DATA)


if __name__ == "__main__":
    sys.exit(main())
