"""Page-turn animation shared by the content stacks and the AI popup."""
from math import pi, sin

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPixmap, QPolygonF
from PySide6.QtWidgets import QStackedWidget, QWidget


class PageTurn(QWidget):
    completed = Signal(bool)

    def __init__(self, parent, *, corner_radius=0):
        super().__init__(parent)
        self.corner_radius = corner_radius
        self.sheet = QPixmap()
        self.from_right = True
        self.motion = QVariantAnimation(self)
        self.motion.setStartValue(0.0)
        self.motion.setEndValue(1.0)
        self.motion.setDuration(320)
        self.motion.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.motion.valueChanged.connect(lambda _: self.update())
        self.motion.finished.connect(self.finish)
        parent.installEventFilter(self)
        self.hide()

    def play(self, sheet, *, from_right=True, opening=False):
        self.clear()
        self.sheet = sheet
        self.from_right = from_right
        self.setGeometry(self.parentWidget().contentsRect())
        self.set_opening(opening)
        self.motion.setCurrentTime(self.motion.duration() if opening else 0)
        self.raise_()
        self.show()
        self.motion.start()

    def set_opening(self, opening):
        direction = QAbstractAnimation.Direction.Backward if opening else QAbstractAnimation.Direction.Forward
        self.motion.setDirection(direction)

    @property
    def opening(self):
        return self.motion.direction() == QAbstractAnimation.Direction.Backward

    def clear(self):
        self.motion.stop()
        self.hide()
        self.sheet = QPixmap()

    def finish(self):
        opening = self.opening
        self.clear()
        self.completed.emit(opening)

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self.setGeometry(obj.contentsRect())
        return super().eventFilter(obj, event)

    def paintEvent(self, event):
        if self.sheet.isNull() or self.rect().isEmpty():
            return
        progress = self.motion.currentValue()
        if progress >= 1:
            return

        page = QRectF(self.rect())
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self.corner_radius:
            rounded = QPainterPath()
            rounded.addRoundedRect(page, self.corner_radius, self.corner_radius)
            painter.setClipPath(rounded)

        width, height = page.width(), page.height()
        curl = min(width * 0.14, 92) * sin(pi * progress)
        crease = width * (1 - progress)

        def point(x, y):
            return QPointF(x if self.from_right else width - x, y)

        top = crease + curl * 0.20
        bottom = crease - curl * 0.29
        # Leave a soft shadow on the newly revealed page, beside the turning edge.
        if curl > 0:
            shadow_width = curl * 0.55
            shadow = QLinearGradient(point(top, 0), point(top + shadow_width, 0))
            shadow.setColorAt(0, QColor(0, 0, 0, 42))
            shadow.setColorAt(1, QColor(0, 0, 0, 0))
            # Shear the gradient to follow the crease by painting in the crease's coordinate system.
            painter.save()
            skew = (bottom - top) / height if self.from_right else (top - bottom) / height
            painter.shear(skew, 0)
            x = top if self.from_right else width - top - shadow_width
            painter.fillRect(QRectF(x, 0, shadow_width, height), shadow)
            painter.restore()

        flat = QPainterPath()
        flat.addPolygon(QPolygonF([point(0, 0), point(top, 0), point(bottom, height), point(0, height)]))
        painter.save()
        painter.setClipPath(flat, Qt.ClipOperation.IntersectClip)
        if not self.corner_radius:
            painter.fillRect(page, QColor("#ffffff"))
        painter.drawPixmap(page, self.sheet, QRectF(self.sheet.rect()))
        painter.restore()

        if curl > 0:
            # The reverse of the paper curves back across its front along the moving crease.
            fold = QPainterPath(point(top, 0))
            fold.lineTo(point(bottom, height))
            fold.quadTo(point(bottom - curl * 0.57, height - curl * 0.09),
                        point(bottom - curl, height - curl * 0.27))
            fold.quadTo(point(crease - curl * 0.89, height * 0.52),
                        point(top - curl * 0.84, curl * 0.30))
            fold.quadTo(point(top - curl * 0.49, curl * 0.09), point(top, 0))
            fold.closeSubpath()
            paper = QLinearGradient(point(crease - curl, 0), point(crease + curl * 0.19, 0))
            paper.setColorAt(0, QColor("#dcdcdc"))
            paper.setColorAt(0.37, QColor("#fcfcfc"))
            paper.setColorAt(0.72, QColor("#f1f1f1"))
            paper.setColorAt(1, QColor("#cbcbcb"))
            painter.fillPath(fold, paper)
        painter.end()


class PageTurnStack(QStackedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.turn = PageTurn(self)

    def turn_to(self, index):
        start = self.currentIndex()
        if not 0 <= index < self.count() or index == start:
            return
        # A quick second click starts from the visible frame of the first turn.
        visible_page = self if self.turn.isVisible() else self.currentWidget()
        sheet = visible_page.grab() if self.isVisible() else QPixmap()
        self.turn.clear()
        self.setCurrentIndex(index)
        if not sheet.isNull():
            self.turn.play(sheet, from_right=index > start)

    def hideEvent(self, event):
        self.turn.clear()
        super().hideEvent(event)
