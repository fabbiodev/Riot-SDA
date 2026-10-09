"""Bundled Riot emblems and a keyboard-friendly account list delegate."""

from functools import lru_cache
import os

from PyQt6.QtCore import Qt, QSize, QRect
from PyQt6.QtGui import QPixmap, QPainter, QColor, QPainterPath, QFont
from PyQt6.QtWidgets import QApplication, QStyledItemDelegate, QStyleOptionViewItem, QStyle

from app.api.rankings import TIERS
from app.core.paths import resource_path


@lru_cache(maxsize=128)
def rank_pixmap(queue, tier, status, size):
    image = QPixmap()
    if status == "ranked" and tier in TIERS:
        product = "tft" if queue == "tft" else "lol"
        image.load(resource_path(os.path.join("app", "assets", "ranks", f"{product}-{tier}.png")))
    output = QPixmap(size * 2, size * 2)
    output.fill(Qt.GlobalColor.transparent)
    painter = QPainter(output)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    if not image.isNull():
        scaled = image.scaled(size * 2, size * 2, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap((output.width() - scaled.width()) // 2,
                           (output.height() - scaled.height()) // 2, scaled)
    else:
        # A neutral shield must never look like an earned rank.
        painter.setPen(QColor("#69717c"))
        painter.setBrush(QColor("#24272b"))
        shield = QPainterPath()
        shield.moveTo(size * .5, size * .3)
        shield.lineTo(size * 1.5, size * .3)
        shield.lineTo(size * 1.45, size * 1.15)
        shield.quadTo(size * 1.4, size * 1.5, size, size * 1.75)
        shield.quadTo(size * .6, size * 1.5, size * .55, size * 1.15)
        shield.closeSubpath()
        painter.drawPath(shield)
        font = QFont(QApplication.font())
        font.setPixelSize(size // 2)
        painter.setFont(font)
        painter.drawText(output.rect(), Qt.AlignmentFlag.AlignCenter,
                         "—" if status == "unranked" else "?")
    painter.end()
    output.setDevicePixelRatio(2)
    return output


class AccountDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return QSize(200, 76)

    def paint(self, painter, option, index):
        card = index.data(Qt.ItemDataRole.UserRole + 1) or {}
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        styled.text = ""
        style = option.widget.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, styled, painter, option.widget)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        painter.setClipRect(option.rect)
        left, width = option.rect.x() + 11, option.rect.width() - 22
        top = option.rect.y() + 16
        ranks = card.get("ranks")
        name_width = width - 78 if ranks else width
        font = QFont(option.font)
        font.setPixelSize(13)
        painter.setFont(font)
        painter.setPen(QColor("#e0e3e6"))
        painter.drawText(QRect(left, top, name_width, 19), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(card.get("name", "Аккаунт"), Qt.TextElideMode.ElideRight, name_width))
        font.setPixelSize(11)
        painter.setFont(font)
        painter.setPen(QColor("#99a0a8"))
        painter.drawText(QRect(left, top + 21, name_width, 17), Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(card.get("subtitle", ""), Qt.TextElideMode.ElideRight, name_width))
        if ranks:
            rank_left = left + width - 69
            font.setPixelSize(10)
            painter.setFont(font)
            for i, (queue, caption) in enumerate((("solo", "Solo"), ("flex", "Flex"), ("tft", "TFT"))):
                rank = ranks.get(queue, {})
                y = option.rect.y() + 9 + i * 19
                painter.setPen(QColor("#99a0a8"))
                painter.drawText(QRect(rank_left, y, 25, 18), Qt.AlignmentFlag.AlignVCenter, caption)
                painter.drawPixmap(rank_left + 28, y, rank_pixmap(queue, rank.get("tier"), rank.get("status"), 18))
                painter.setPen(QColor("#e0e3e6") if rank.get("status") == "ranked" else QColor("#99a0a8"))
                text = (rank.get("division", "") if rank.get("status") == "ranked"
                        else "—" if rank.get("status") == "unranked" else "?")
                if rank.get("refresh_error"):
                    text += " *"
                painter.drawText(QRect(rank_left + 48, y, 21, 18),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)
        painter.restore()
