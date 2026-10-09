"""Small, interruptible Qt animations; input and data updates stay immediate."""

from PyQt6.QtCore import QObject, QEvent, QPropertyAnimation, QEasingCurve, pyqtProperty, Qt
from PyQt6.QtGui import QPainter, QColor
from PyQt6.QtWidgets import QPushButton, QGraphicsOpacityEffect, QAbstractItemView


class AnimatedButton(QPushButton):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._hover = 0.0
        self._hover_animation = QPropertyAnimation(self, b"hoverAmount", self)
        self._hover_animation.setDuration(180)
        self._hover_animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    @pyqtProperty(float)
    def hoverAmount(self):
        return self._hover

    @hoverAmount.setter
    def hoverAmount(self, value):
        self._hover = value
        self.update()

    def _animate_hover(self, value):
        self._hover_animation.stop()
        self._hover_animation.setStartValue(self._hover)
        self._hover_animation.setEndValue(value)
        self._hover_animation.start()

    def enterEvent(self, event):
        self._animate_hover(1.0 if self.isEnabled() else 0.0)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._animate_hover(0.0)
        super().leaveEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._hover > 0 and self.isEnabled():
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(238, 231, 230, round(12 * self._hover)))
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 13, 13)


class ContentFade(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.effect = QGraphicsOpacityEffect(widget)
        self.effect.setOpacity(1.0)
        widget.setGraphicsEffect(self.effect)
        self.animation = QPropertyAnimation(self.effect, b"opacity", self)
        self.animation.setDuration(220)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def start(self):
        if not self.parent().isVisible():
            return
        self.animation.stop()
        self.animation.setStartValue(0.84)
        self.animation.setEndValue(1.0)
        self.animation.start()


class SmoothScroll(QObject):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.bar = view.verticalScrollBar()
        self.animation = QPropertyAnimation(self.bar, b"value", self)
        self.animation.setDuration(170)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.bar.sliderPressed.connect(self.animation.stop)
        view.viewport().installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() != QEvent.Type.Wheel or not self.bar.maximum():
            return False
        # Trackpads already deliver small, smooth pixel deltas.
        if event.pixelDelta().y() or event.modifiers() or not event.angleDelta().y():
            self.animation.stop()
            return False
        target = (int(self.animation.endValue()) if self.animation.state() == QPropertyAnimation.State.Running
                  else self.bar.value())
        target -= round(event.angleDelta().y() / 120 * 110)
        target = max(self.bar.minimum(), min(self.bar.maximum(), target))
        self.animation.stop()
        self.animation.setStartValue(self.bar.value())
        self.animation.setEndValue(target)
        self.animation.start()
        event.accept()
        return True
