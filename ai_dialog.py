"""VPN-X AI dialog. Network calls stay off the main UI thread."""
from collections import deque
from math import ceil

from PySide6.QtCore import QEvent, QPoint, QSize, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QSizePolicy, QTextBrowser, QTextEdit, QVBoxLayout, QWidget)

from ai_support import (DAILY_LIMIT, QUESTION_LIMIT, DailyQuota, cloud_answer, guide_text,
                        local_answer, read_topics, redact)
from catalog import VPNError
from ui_icons import stroke_icon
from ui_transitions import PageTurn, PageTurnStack


POPUP_STYLE = """
QDialog#aiDialog { background:transparent; }
QWidget#aiShell { background:#fff; border:1px solid #e5e5e5; border-radius:18px; }
QLabel#aiTitle { color:#0d0d0d; font-size:17px; font-weight:700; }
QLabel#aiMeta { color:#5d5d5d; font-size:11px; }
QPushButton#aiGuide, QPushButton#aiClose {
    background:#f4f4f4; border:1px solid transparent; border-radius:9px;
    color:#262626; padding:4px 8px; font-weight:550;
}
QPushButton#aiClose { padding:0; }
QPushButton#aiGuide:hover, QPushButton#aiClose:hover { background:#e9e9e9; }
QPushButton#aiGuide:focus, QPushButton#aiClose:focus { border-color:#767676; }
QScrollArea#aiTranscript, QWidget#aiChatViewport, QWidget#aiMessages {
    background:transparent; border:0;
}
QWidget#aiBubble { background:#f2f2f2; border-radius:10px; }
QWidget#aiBubble[mine="true"] { background:#e9e9e9; }
QLabel#aiSender { color:#5d5d5d; font-size:11px; font-weight:600; }
QTextEdit#aiMessage {
    background:transparent; border:0;
    selection-background-color:#d5d5d5; selection-color:#0d0d0d;
}
QPushButton#aiExpand {
    background:transparent; color:#5d5d5d; border:1px solid transparent;
    border-radius:5px; padding:1px 3px; min-height:0; font-size:11px; font-weight:600;
}
QPushButton#aiExpand:hover { background:#e2e2e2; }
QPushButton#aiExpand:focus { border-color:#767676; }
QTextBrowser#aiGuideText {
    background:#f7f7f7; border:0; border-radius:12px; padding:10px;
    selection-background-color:#e5e5e5; selection-color:#0d0d0d;
}
QLineEdit#aiInput { background:#fff; border:1px solid #dedede; border-radius:10px; padding:8px 10px; }
QLineEdit#aiInput:focus { border-color:#767676; }
QPushButton#aiSend { background:#0d0d0d; color:#fff; border:0; border-radius:10px; padding:7px 14px; }
QPushButton#aiSend:hover { background:#2f2f2f; }
QPushButton#aiSend:pressed { background:#000; }
QPushButton#aiSend:disabled { background:#eeeeee; color:#767676; }
"""


class AnswerThread(QThread):
    answer = Signal(str, bool)

    def __init__(self, parent, key, question, history, state, topics, model, secrets):
        super().__init__(parent)
        self.args = key, question, history, state, topics, model, secrets

    def run(self):
        key, question, history, state, topics, model, secrets = self.args
        self.args = None
        try:
            answer = cloud_answer(key, question, history, state, topics, model=model, secrets=secrets)
        except VPNError as error:
            self.answer.emit(str(error), False)
        except Exception:
            self.answer.emit("暂时无法获取 AI 回复，请查看本地指南。", False)
        else:
            self.answer.emit(answer, True)


class MessageText(QTextEdit):
    overflow_changed = Signal(bool)

    def __init__(self, text):
        super().__init__()
        self.expanded = False
        self.has_overflow = False
        self.setObjectName("aiMessage")
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        document = self.document()
        document.setDocumentMargin(0)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setPlainText(text)
        self.preferred_width = max(1, min(286, ceil(document.idealWidth())))
        self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        document.documentLayout().documentSizeChanged.connect(self.fit_text)
        self.fit_text(document.size())

    def sizeHint(self):
        return QSize(self.preferred_width, self.minimumHeight())

    def minimumSizeHint(self):
        return QSize(1, self.minimumHeight())

    def fit_text(self, size):
        # Match the rendered document after the viewport wraps it, including font fallback.
        full_height = ceil(size.height())
        preview_height = 7 * self.fontMetrics().lineSpacing()
        height = full_height if self.expanded else min(full_height, preview_height)
        if height != self.minimumHeight():
            self.setFixedHeight(height)
        has_overflow = full_height > preview_height
        if has_overflow != self.has_overflow:
            self.has_overflow = has_overflow
            self.overflow_changed.emit(has_overflow)

    def set_expanded(self, expanded):
        self.expanded = expanded
        self.fit_text(self.document().size())
        if not expanded:
            self.verticalScrollBar().setValue(0)

    def wheelEvent(self, event):
        event.ignore()


class AIDialog(QDialog):
    def __init__(self, window, data_dir):
        super().__init__(window, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.window = window
        self.setWindowTitle("VPN-X AI")
        self.setObjectName("aiDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet(POPUP_STYLE)
        self.resize(440, 510)
        self.setMinimumSize(320, 320)
        self.restore_with_window = False
        window.installEventFilter(self)
        self.quota = DailyQuota(data_dir)
        self.history = deque(maxlen=2)
        self.worker = None
        self.last_question = ""
        self.guide = guide_text()
        self.topics = read_topics()
        self.guide_snapshot = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.shell = QWidget()
        self.shell.setObjectName("aiShell")
        self.shell.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        policy = self.shell.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.shell.setSizePolicy(policy)
        outer.addWidget(self.shell)
        box = QVBoxLayout(self.shell)
        box.setContentsMargins(15, 13, 15, 15)
        box.setSpacing(11)
        head = QHBoxLayout()
        title_group = QVBoxLayout()
        title_group.setSpacing(2)
        title = QLabel("VPN-X AI")
        title.setObjectName("aiTitle")
        title_group.addWidget(title)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setObjectName("aiMeta")
        title_group.addWidget(self.status)
        head.addLayout(title_group, 1)
        self.guide_button = QPushButton("使用指南")
        self.guide_button.setObjectName("aiGuide")
        self.guide_button.clicked.connect(self.toggle_guide)
        head.addWidget(self.guide_button)
        close = QPushButton(stroke_icon("close"), "")
        close.setObjectName("aiClose")
        close.setAccessibleName("关闭 AI 对话")
        close.setToolTip("关闭")
        close.setFixedSize(30, 30)
        close.clicked.connect(self.close)
        head.addWidget(close)
        box.addLayout(head)

        self.pages = PageTurnStack()
        self.chat_page = QWidget()
        conversation = QVBoxLayout(self.chat_page)
        conversation.setContentsMargins(0, 0, 0, 0)
        conversation.setSpacing(11)
        self.chat = QScrollArea()
        self.pending_latest_message = False
        self.reveal_timer = QTimer(self)
        self.reveal_timer.setSingleShot(True)
        self.reveal_timer.setInterval(16)
        self.reveal_timer.timeout.connect(self.reveal_latest_message)
        self.chat.setObjectName("aiTranscript")
        self.chat.setWidgetResizable(True)
        self.chat.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.chat.viewport().setObjectName("aiChatViewport")
        messages = QWidget()
        messages.setObjectName("aiMessages")
        self.messages = QVBoxLayout(messages)
        self.messages.setContentsMargins(0, 2, 4, 2)
        self.messages.setSpacing(6)
        self.messages.addStretch(1)
        self.chat.setWidget(messages)
        messages.setAutoFillBackground(False)
        conversation.addWidget(self.chat, 1)
        self.pages.addWidget(self.chat_page)

        self.guide_view = QTextBrowser()
        self.guide_view.setObjectName("aiGuideText")
        self.guide_view.setOpenLinks(False)
        self.guide_view.setOpenExternalLinks(False)
        self.guide_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.guide_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pages.addWidget(self.guide_view)
        box.addWidget(self.pages, 1)
        self.append("VPN-X AI", "你好，可以问我「没有网怎么办」「怎么用」或「软件架构」。")

        compose = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setObjectName("aiInput")
        self.input.setMaxLength(QUESTION_LIMIT)
        self.input.setPlaceholderText("问一个关于 VPN-X 的问题…")
        self.input.returnPressed.connect(self.send)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("aiSend")
        self.send_button.clicked.connect(self.send)
        compose.addWidget(self.input, 1)
        compose.addWidget(self.send_button)
        conversation.addLayout(compose)

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh_state)

        self.turn = PageTurn(self, corner_radius=18)
        self.turn.completed.connect(self.finish_turn)

    def append(self, who, text):
        mine = who == "你"
        bubble = QWidget()
        bubble.setObjectName("aiBubble")
        bubble.setProperty("mine", mine)
        bubble.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        bubble.setMaximumWidth(310)
        bubble.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        content = QVBoxLayout(bubble)
        content.setContentsMargins(10, 5, 10, 6)
        content.setSpacing(2)

        sender = QLabel(who)
        sender.setObjectName("aiSender")
        sender.setTextFormat(Qt.TextFormat.PlainText)
        content.addWidget(sender)

        message = MessageText(text)
        content.addWidget(message)
        expand = QPushButton("展开全文")
        expand.setObjectName("aiExpand")
        expand.setCheckable(True)
        content.addWidget(expand, alignment=Qt.AlignmentFlag.AlignLeft)
        expand.setVisible(message.has_overflow)
        message.overflow_changed.connect(expand.setVisible)

        def toggle_expanded(expanded):
            message.set_expanded(expanded)
            expand.setText("收起全文" if expanded else "展开全文")

        expand.toggled.connect(toggle_expanded)

        alignment = Qt.AlignmentFlag.AlignRight if mine else Qt.AlignmentFlag.AlignLeft
        if self.messages.count() > 200:  # The final item is the stretch beneath the messages.
            oldest = self.messages.takeAt(0).widget()
            oldest.hide()
            oldest.deleteLater()
        self.messages.insertWidget(self.messages.count() - 1, bubble, alignment=alignment)
        self.pending_latest_message = True
        self.reveal_timer.start()

    def reveal_latest_message(self):
        if self.pending_latest_message and self.chat.isVisible():
            self.pending_latest_message = False
            bubble = self.messages.itemAt(self.messages.count() - 2).widget()
            self.chat.verticalScrollBar().setValue(bubble.y())

    def present(self):
        self.anchor_to_window()
        if self.turn.isVisible():
            self.turn.set_opening(True)
        self.show()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_state()
        self.timer.start()
        self.turn.play(self.shell.grab(), from_right=False, opening=True)
        self.shell.hide()

    def dismiss(self):
        if self.turn.isVisible():
            self.turn.set_opening(False)
        elif self.isVisible():
            self.turn.play(self.shell.grab(), from_right=False)
            self.shell.hide()

    def finish_turn(self, opening):
        if opening:
            self.shell.show()
            (self.input if self.pages.currentIndex() == 0 else self.guide_view).setFocus()
            self.reveal_timer.start()
        else:
            self.hide()

    def reject(self):
        self.dismiss()

    def closeEvent(self, event):
        if self.isVisible():
            event.ignore()
            self.dismiss()
        else:
            event.accept()

    def anchor_to_window(self):
        bounds = self.window.centralWidget().rect()
        origin = self.window.centralWidget().mapToGlobal(QPoint(0, 0))
        margin = 18
        width = min(440, bounds.width() - 2 * margin)
        height = min(510, bounds.height() - 2 * margin)
        left = origin.x() + bounds.width() - width - margin
        top = origin.y() + bounds.height() - height - margin
        self.setGeometry(left, top, width, height)

    def eventFilter(self, obj, event):
        if obj is self.window:
            event_type = event.type()
            if event_type == QEvent.Type.Hide or (event_type == QEvent.Type.WindowStateChange and self.window.isMinimized()):
                if self.isVisible():
                    self.restore_with_window = self.shell.isVisible() or self.turn.opening
                    self.hide()
            elif event_type == QEvent.Type.Show or (event_type == QEvent.Type.WindowStateChange and self.window.isVisible()):
                if self.restore_with_window:
                    self.restore_with_window = False
                    self.anchor_to_window()
                    self.show()
            elif event_type in (QEvent.Type.Move, QEvent.Type.Resize) and self.isVisible():
                self.anchor_to_window()
        return super().eventFilter(obj, event)

    def hideEvent(self, event):
        self.turn.clear()
        self.shell.show()
        self.timer.stop()
        super().hideEvent(event)

    def refresh_state(self):
        snapshot = self.window.ai_state()
        try:
            remaining = f"{self.quota.remaining()}/{DAILY_LIMIT}"
        except VPNError:
            remaining = "不可读取"
        mode = f"模型：{self.window.ai_model}" if self.window.ai_key else "本地指南模式"
        activity = " · 正在回复…" if self.worker is not None else ""
        self.status.setText(f"今日剩余：{remaining} · {mode}{activity}")
        if self.pages.currentIndex() == 1 and snapshot != self.guide_snapshot:
            offset = self.guide_view.verticalScrollBar().value()
            header, _, guide = self.guide.partition("\n")
            self.guide_view.setMarkdown(f"{header}\n\n## 实时状态\n\n{snapshot}\n\n{guide}")
            self.guide_view.verticalScrollBar().setValue(offset)
            self.guide_snapshot = snapshot

    def toggle_guide(self):
        show = self.pages.currentIndex() == 0
        self.pages.turn_to(1 if show else 0)
        self.guide_button.setText("返回对话" if show else "使用指南")
        self.refresh_state()
        (self.guide_view if show else self.input).setFocus()
        if not show:
            self.reveal_timer.start()

    def send(self):
        if self.window.exit_action:
            return
        question = self.input.text().strip()
        if not question or self.worker is not None:
            return
        secrets = self.window.ai_secrets()
        question = redact(question, secrets)
        local = local_answer(question, self.topics)
        key = self.window.ai_key
        if not local and not key:
            self.append("VPN-X AI", "联网问答暂不可用，你可以先查看本地使用指南。")
            return
        try:
            self.quota.reserve()
        except VPNError as err:
            self.append("VPN-X AI", str(err))
            return
        self.refresh_state()
        self.append("你", question)
        self.input.clear()
        if local:
            self.reply(local, True, question)
            return
        self.send_button.setEnabled(False)
        self.last_question = question
        self.worker = AnswerThread(self, key, question, tuple(self.history), self.window.ai_state(), self.topics,
                                   self.window.ai_model, secrets)
        self.worker.answer.connect(self.remote_reply)
        self.worker.finished.connect(self.request_finished)
        self.worker.start()
        self.refresh_state()

    def remote_reply(self, answer, ok):
        self.reply(answer, ok, self.last_question)

    def reply(self, answer, ok, question):
        self.append("VPN-X AI", answer)
        if ok:
            self.history.extend((("user", question), ("assistant", answer)))

    def request_finished(self):
        self.worker.deleteLater()
        self.worker = None
        self.send_button.setEnabled(True)
        self.refresh_state()
