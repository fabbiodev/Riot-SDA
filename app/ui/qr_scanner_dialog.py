"""Draggable on-screen QR code reader.

A frameless, always-on-top window with an opaque drag bar and a transparent
viewport. Position the viewport over a QR code on screen; it continuously grabs
the pixels behind it and decodes them with OpenCV. Emits `decoded(str)` on the
first successful read.
"""

import numpy as np
import cv2

from PyQt6.QtWidgets import (
    QDialog,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QSizeGrip,
)
from PyQt6.QtCore import Qt, QTimer, QPoint, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QImage

def _qimage_to_bgr(qimg):
    img = qimg.convertToFormat(QImage.Format.Format_RGB888)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    if w == 0 or h == 0:
        return None
    buf = img.constBits()
    buf.setsize(h * bpl)
    arr = np.frombuffer(buf, np.uint8).reshape((h, bpl))[:, : w * 3].reshape((h, w, 3))
    return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

class QrScannerDialog(QDialog):
    decoded = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._result = None
        self._detector = cv2.QRCodeDetector()

        self.setWindowTitle("Scan QR Code")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Window
        )
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(300, 300)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.bar = QWidget()
        self.bar.setObjectName("qrBar")
        self.bar.setFixedHeight(30)
        self.bar.setStyleSheet(
            "#qrBar{background:#252a32; border:1px solid #64788b;"
            "border-top-left-radius:12px; border-top-right-radius:12px;}"
        )
        bar_l = QHBoxLayout(self.bar)
        bar_l.setContentsMargins(10, 0, 6, 0)
        self.status = QLabel("Drag over a QR code…")
        self.status.setStyleSheet("color:#9aa; font-size:11px; background:transparent;")
        bar_l.addWidget(self.status)
        bar_l.addStretch()
        close = QPushButton("✕")
        close.setFixedSize(26, 26)
        close.setToolTip("Close (Esc)")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setStyleSheet(
            "QPushButton{background:#35303a; border:1px solid #725b6b; border-radius:9px;"
            "color:#e7bdc8; font-size:14px;}"
            "QPushButton:hover{background:#51404c; color:#f3e9ed; border-color:#947488;}"
        )
        close.clicked.connect(self.reject)
        bar_l.addWidget(close)
        root.addWidget(self.bar)

        self.viewport = QFrame()
        self.viewport.setObjectName("qrViewport")
        self.viewport.setStyleSheet(
            "#qrViewport{background:transparent; border:2px solid #8cabbc;"
            "border-top:none;}"
        )
        root.addWidget(self.viewport, stretch=1)

        grip = QSizeGrip(self.viewport)
        grip_l = QHBoxLayout(self.viewport)
        grip_l.setContentsMargins(0, 0, 0, 0)
        grip_l.addStretch()
        gb = QVBoxLayout()
        gb.addStretch()
        gb.addWidget(grip, alignment=Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        grip_l.addLayout(gb)

        self._drag_offset = None

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._scan_tick)
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._start_scanning)

    def showEvent(self, event):
        super().showEvent(event)
        if self._result is None:
            self._startup_timer.start(1000)

    def hideEvent(self, event):
        self._stop_scanning()
        super().hideEvent(event)

    def _start_scanning(self):
        if self.isVisible() and not self.isMinimized() and self._result is None:
            self._timer.start(450)

    def _stop_scanning(self):
        self._startup_timer.stop()
        self._timer.stop()

    def done(self, result):
        self._stop_scanning()
        super().done(result)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.bar.geometry().contains(
            event.position().toPoint()
        ):
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def _scan_tick(self):
        if not self.isVisible() or self.isMinimized() or self._result is not None:
            return
        inset = 4
        tl = self.viewport.mapToGlobal(QPoint(inset, inset))
        w = max(1, self.viewport.width() - 2 * inset)
        h = max(1, self.viewport.height() - 2 * inset)
        center = self.viewport.mapToGlobal(self.viewport.rect().center())
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        pixmap = screen.grabWindow(0, tl.x(), tl.y(), w, h)
        if pixmap.isNull():
            return
        frame = _qimage_to_bgr(pixmap.toImage())
        if frame is None:
            return
        try:
            data, _pts, _ = self._detector.detectAndDecode(frame)
        except cv2.error:
            data = ""
        if data:
            self._result = data
            self._timer.stop()
            self.status.setText("QR detected ✓")
            self.decoded.emit(data)
            self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
        else:
            super().keyPressEvent(event)

    @property
    def result_text(self):
        return self._result

    def reject(self):
        self._timer.stop()
        super().reject()
