"""Responsive, virtualized collection tiles using the existing public artwork cache."""

from PyQt6.QtCore import QSize, QRectF, QPointF, Qt
from PyQt6.QtGui import QColor, QFont, QPen, QTextLayout, QTextOption
from PyQt6.QtWidgets import QListWidget, QStyledItemDelegate, QStyle, QAbstractItemView


class InventoryGrid(QListWidget):
    def __init__(self, store, parent=None):
        super().__init__(parent)
        self.setObjectName("inventoryGrid")
        self.setAccessibleName("Сетка персонажей и скинов")
        self.setViewMode(QListWidget.ViewMode.IconMode)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.setMovement(QListWidget.Movement.Static)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Reserve the scrollbar's width so the grid never jumps between column counts.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setMouseTracking(True)
        self.setSpacing(0)
        self.game, self.collection, self.compact = "lol", "characters", False
        self.setItemDelegate(InventoryDelegate(store, self))
        store.changed.connect(self.viewport().update)
        self.reflow()

    def set_context(self, game, collection, compact):
        self.game, self.collection, self.compact = game, collection, compact
        self.reflow()

    def reflow(self):
        weapon = self.game == "valorant" and self.collection == "skins"
        target = ((174 if weapon else 112 if self.collection == "skins" else 128) if self.compact
                  else (190 if weapon else 128 if self.collection == "skins" else 148))
        available = max(1, self.viewport().width())
        columns = max(1, available // target)
        # QListView wraps at the right edge even when cells exactly fill it.
        width = max(1, (available - 1) // columns)
        # Cap artwork height for wide windows while keeping a regular grid.
        art_height = (min(90, round((width - 26) / 2.1)) if weapon else
                      (124 if self.compact else 144) if self.collection == "skins" else
                      min(112 if self.compact else 132, max(32, width - 26)))
        size = QSize(width, art_height + (78 if self.compact else 86) + (20 if self.collection == "skins" else 0))
        if size != self.gridSize():
            self.setGridSize(size)
            self.doItemsLayout()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()


class InventoryDelegate(QStyledItemDelegate):
    def __init__(self, store, parent):
        super().__init__(parent)
        self.store = store

    def sizeHint(self, option, index):
        return self.parent().gridSize()

    @staticmethod
    def artwork_rect(rect, compact, skin=False):
        return rect.adjusted(9, 9, -9, -(67 if compact else 75) - (20 if skin else 0))

    def paint(self, painter, option, index):
        row = index.data(Qt.ItemDataRole.UserRole) or {}
        meta = index.data(Qt.ItemDataRole.UserRole + 1) or {}
        compact = self.parent().compact
        rect = QRectF(option.rect).adjusted(0, 0, -10, -10)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        painter.setRenderHint(painter.RenderHint.TextAntialiasing)
        painter.setBrush(QColor("#292327" if selected else "#24272c" if hovered else "#1c1f22"))
        painter.setPen(QPen(QColor("#b8767e" if selected else "#515861" if hovered else "#30353a"), 1))
        painter.drawRoundedRect(rect.adjusted(.5, .5, -.5, -.5), 12, 12)
        is_skin = meta.get("collection") == "skins"
        image_rect = self.artwork_rect(rect, compact, is_skin)
        pixmap = self.store.thumbnail(meta.get("game", "lol"), meta.get("collection", "characters"),
                                      row, QSize(max(1, int(image_rect.width())), max(1, int(image_rect.height()))))
        painter.drawPixmap(image_rect.topLeft(), pixmap)

        font = QFont(option.font)
        font.setPixelSize(12 if compact else 13)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor("#e4e7ea"))
        name_rect = QRectF(rect.left() + 10, image_rect.bottom() + 10, rect.width() - 20, 34)
        self._title(painter, str(row.get("name", "")), font, name_rect)
        font.setPixelSize(11)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor("#a0a7af"))
        subtitle_rect = QRectF(name_rect.left(), rect.bottom() - 24, name_rect.width(), 16)
        subtitle = painter.fontMetrics().elidedText(str(meta.get("subtitle", "")),
                                                    Qt.TextElideMode.ElideRight, int(subtitle_rect.width()))
        painter.drawText(subtitle_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, subtitle)
        if is_skin:
            label = str(row.get("skin_type", "Тип не указан"))
            colors = {"Обычный": "#a0a7af", "Редкий": "#74b9ce", "Эпический": "#b79ce8",
                      "Легендарный": "#e7ad76", "Мифический": "#d69ad3", "Абсолютный": "#e4ca7a",
                      "Возвышенный": "#e998a9", "Трансцендентный": "#ed8b7d",
                      "Select": "#85b6e8", "Deluxe": "#78c8b6", "Premium": "#b89ce6",
                      "Exclusive": "#e8af7e", "Ultra": "#e4cc7d"}
            font.setPixelSize(10)
            painter.setFont(font)
            type_rect = QRectF(name_rect.left(), rect.bottom() - 44, name_rect.width(), 16)
            label = painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, int(type_rect.width()) - 10)
            pill = QRectF(type_rect.left(), type_rect.top(), min(type_rect.width(), painter.fontMetrics().horizontalAdvance(label) + 10), 16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#292c31"))
            painter.drawRoundedRect(pill, 4, 4)
            painter.setPen(QColor(colors.get(str(row.get("skin_type")), "#a0a7af")))
            painter.drawText(pill.adjusted(5, 0, -5, 0), Qt.AlignmentFlag.AlignVCenter, label)
        if option.state & QStyle.StateFlag.State_HasFocus:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#d4a0a6"), 1))
            painter.drawRoundedRect(rect.adjusted(2.5, 2.5, -2.5, -2.5), 10, 10)
        painter.restore()

    @staticmethod
    def _title(painter, text, font, rect):
        layout = QTextLayout(text, font)
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        layout.beginLayout()
        for number in range(2):
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(rect.width())
            if number == 1 and line.textStart() + line.textLength() < len(text):
                remainder = painter.fontMetrics().elidedText(text[line.textStart():], Qt.TextElideMode.ElideRight,
                                                             int(rect.width()))
                painter.drawText(QPointF(rect.left(), rect.top() + 17 + line.ascent()), remainder)
                break
            line.setPosition(QPointF(0, number * 17))
            line.draw(painter, rect.topLeft())
        layout.endLayout()
