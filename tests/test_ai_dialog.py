from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from PySide6.QtGui import QFont
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from ui_theme import light_palette
from vpn_x import STYLE, Window


class AIDialogTests(unittest.TestCase):
    def test_first_open_keeps_welcome_bubble_at_content_height(self):
        app = QApplication.instance() or QApplication(["vpn-x-tests", "-platform", "offscreen"])
        app.setStyle("Fusion")
        app.setPalette(light_palette())
        app.setFont(QFont("Microsoft YaHei UI", 10))
        app.setStyleSheet(STYLE)

        with TemporaryDirectory() as folder:
            window = Window(Path(folder), start_background=False)
            try:
                window.show()
                window.open_ai()
                dialog = window.ai_dialog
                QTest.qWait(dialog.turn.motion.duration() + 70)

                bubbles = dialog.chat.findChildren(QWidget, "aiBubble")
                self.assertEqual(len(bubbles), 1)
                bubble = bubbles[0]
                sender = bubble.findChild(QLabel, "aiSender")
                self.assertTrue(bubble.isVisible())
                self.assertLessEqual(bubble.height(), bubble.sizeHint().height() + 2)
                self.assertLessEqual(sender.height(), sender.sizeHint().height() + 2)
            finally:
                window.hide()
