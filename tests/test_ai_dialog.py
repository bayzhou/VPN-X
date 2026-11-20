from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from ui_theme import light_palette
from vpn_x import STYLE, Window


class AIDialogTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication(["vpn-x-tests", "-platform", "offscreen"])
        self.app.setStyle("Fusion")
        self.app.setPalette(light_palette())
        self.app.setFont(QFont("Microsoft YaHei UI", 10))
        self.app.setStyleSheet(STYLE)

        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        window = Window(Path(folder.name), start_background=False)
        self.addCleanup(window.hide)
        window.show()
        window.open_ai()
        self.dialog = window.ai_dialog
        QTest.qWait(self.dialog.turn.motion.duration() + 70)

    def test_first_open_keeps_welcome_bubble_at_content_height(self):
        bubbles = self.dialog.chat.findChildren(QWidget, "aiBubble")
        self.assertEqual(len(bubbles), 1)
        bubble = bubbles[0]
        sender = bubble.findChild(QLabel, "aiSender")
        self.assertTrue(bubble.isVisible())
        self.assertLessEqual(bubble.height(), bubble.sizeHint().height() + 2)
        self.assertLessEqual(sender.height(), sender.sizeHint().height() + 2)

    def test_enter_sends_only_in_chat_and_leaves_the_guide_alone(self):
        dialog = self.dialog
        for key, modifier in ((Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier),
                              (Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)):
            with self.subTest(key=key):
                before = dialog.quota.remaining()
                dialog.input.setText("VPN-X 怎么用")
                QTest.keyClick(dialog.input, key, modifier)
                self.assertIs(dialog.pages.currentWidget(), dialog.chat_page)
                self.assertEqual(dialog.quota.remaining(), before - 1)
                self.assertEqual(dialog.input.text(), "")

        before = dialog.quota.remaining()
        QTest.keyClick(dialog.input, Qt.Key.Key_Return)
        self.assertEqual(dialog.quota.remaining(), before)
        self.assertIs(dialog.pages.currentWidget(), dialog.chat_page)

        dialog.input.setText("VPN-X 怎么用")
        dialog.guide_button.click()
        self.assertIs(dialog.pages.currentWidget(), dialog.guide_view)
        dialog.guide_button.setFocus()
        QTest.keyClick(dialog.guide_button, Qt.Key.Key_Return)
        self.assertIs(dialog.pages.currentWidget(), dialog.guide_view)
        self.assertEqual(dialog.quota.remaining(), before)
        self.assertEqual(dialog.input.text(), "VPN-X 怎么用")
        QTest.keyClick(dialog.guide_button, Qt.Key.Key_Space)
        self.assertIs(dialog.pages.currentWidget(), dialog.chat_page)
