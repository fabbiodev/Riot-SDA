"""Render the editable SVG to PNG and a Windows icon with multiple sizes.

Run with the project's Python environment from any working directory.
ICO frames contain PNG data, supported by Windows Vista and later.
"""

import os
from pathlib import Path
import struct

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QBuffer, QIODevice, Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render(renderer, size):
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise RuntimeError("Could not encode app icon")
    return bytes(buffer.data())


def main():
    app = QApplication([])
    images = Path(__file__).resolve().parents[1] / "images"
    renderer = QSvgRenderer(str(images / "icon.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid icon.svg")
    (images / "icon.png").write_bytes(render(renderer, 512))
    frames = [render(renderer, size) for size in SIZES]
    offset = 6 + 16 * len(frames)
    entries = []
    for size, frame in zip(SIZES, frames):
        entries.append(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                                   1, 32, len(frame), offset))
        offset += len(frame)
    (images / "icon.ico").write_bytes(struct.pack("<HHH", 0, 1, len(frames))
                                       + b"".join(entries) + b"".join(frames))
    print("Rendered icon.png (512px) and icon.ico:", ", ".join(map(str, SIZES)))


if __name__ == "__main__":
    main()
