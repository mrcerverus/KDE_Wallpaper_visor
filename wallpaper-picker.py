#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Selector visual de fondos de pantalla para KDE Plasma 6."""

from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from urllib.parse import unquote

from PySide6.QtCore import (
    QEvent,
    QElapsedTimer,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QIcon,
    QImage,
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

APP_NAME = "wallpaper-picker"
SERVER_NAME = f"{APP_NAME}-{os.getuid()}"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / APP_NAME
CONFIG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / f"{APP_NAME}.conf"

THUMB_W, THUMB_H = 480, 270
CARD_W = 290
CARD_PAD = 8
THUMB_DISPLAY_W = 268
THUMB_DISPLAY_H = 151
GRID_SPACING = 14

CAROUSEL_GAP = 16
CAROUSEL_ROW_GAP = 18
CAROUSEL_SPEED = 14.0

HEX_HEIGHT = 180.0
HEX_WIDTH = HEX_HEIGHT * math.sqrt(3.0) / 2.0
HEX_GAP = 6.0
HEX_MARGIN = 12.0

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".avif", ".bmp",
    ".gif", ".jxl", ".tif", ".tiff", ".heic",
}

PLASMA_APPLY = shutil.which("plasma-apply-wallpaperimage") or "/usr/bin/plasma-apply-wallpaperimage"

MODES = [
    ("grid", "▦  Cuadrícula"),
    ("carousel", "⇄  Carrusel"),
    ("hex", "⬡  Hexágono"),
]


@dataclass
class WallpaperItem:
    path: Path
    name: str
    is_current: bool


def wallpaper_dir() -> Path:
    env_dir = os.environ.get("WALLPAPER_PICKER_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    candidates = [
        Path.home() / "Imágenes" / "Wallpapers",
        Path.home() / "Pictures" / "Wallpapers",
        Path.home() / "Imágenes",
        Path.home() / "Pictures",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def natural_key(path: Path) -> list[tuple[int, object]]:
    parts = re.split(r"(\d+)", path.name.casefold())
    return [(0, int(part)) if part.isdigit() else (1, part) for part in parts if part]


def scan_wallpapers(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    try:
        items = [
            path
            for path in folder.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in IMAGE_EXTS
        ]
    except OSError:
        return []
    return sorted(items, key=natural_key)


def cache_path(path: Path) -> Path:
    stat = path.stat()
    key = f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}"
    digest = hashlib.sha1(key.encode("utf-8", "surrogatepass")).hexdigest()
    return CACHE_DIR / f"{digest}.jpg"


def make_thumbnail(path: Path) -> QImage:
    try:
        cached = cache_path(path)
    except OSError:
        cached = None

    if cached is not None and cached.is_file():
        image = QImage(str(cached))
        if not image.isNull():
            return image

    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid() and source_size.width() > 0 and source_size.height() > 0:
        target = source_size.scaled(
            QSize(THUMB_W * 2, THUMB_H * 2),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        )
        if target.width() > THUMB_W * 2 or target.height() > THUMB_H * 2:
            target = source_size.scaled(
                QSize(THUMB_W, THUMB_H),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            )
        reader.setScaledSize(target)

    image = reader.read()
    if image.isNull():
        return image

    image = image.convertToFormat(QImage.Format.Format_RGB32)
    if image.width() < THUMB_W or image.height() < THUMB_H:
        image = image.scaled(
            QSize(THUMB_W, THUMB_H),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )

    x = max(0, (image.width() - THUMB_W) // 2)
    y = max(0, (image.height() - THUMB_H) // 2)
    image = image.copy(x, y, THUMB_W, THUMB_H)

    if cached is not None:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            image.save(str(cached), "JPEG", 84)
        except OSError:
            pass

    return image


def prune_cache(paths: list[Path]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        keep = set()
        for path in paths:
            try:
                keep.add(cache_path(path).name)
            except OSError:
                continue
        for cached in CACHE_DIR.glob("*.jpg"):
            if cached.name not in keep:
                try:
                    cached.unlink()
                except OSError:
                    pass
    except OSError:
        pass


def current_wallpaper_paths() -> set[str]:
    config = Path.home() / ".config" / "plasma-org.kde.plasma.desktop-appletsrc"
    found: set[str] = set()
    try:
        text = config.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return found

    for match in re.finditer(r"^\s*Image=(.+?)\s*$", text, re.MULTILINE):
        raw = match.group(1).strip()
        if raw.startswith("file://"):
            raw = unquote(raw[7:])
        if not raw.startswith("/"):
            continue
        try:
            found.add(str(Path(raw).resolve()))
        except OSError:
            continue
    return found


def send_notification(summary: str, body: str, icon: str | None = None, urgent: bool = False) -> None:
    notify = shutil.which("notify-send")
    if not notify:
        return
    command = [notify, "-a", "Selector de fondos", "-t", "3000"]
    if urgent:
        command += ["-u", "critical"]
    if icon:
        command += ["-i", icon]
    command += [summary, body]
    try:
        subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


class ThumbnailLoader(QThread):
    ready = Signal(str, QImage)
    finished_all = Signal()

    def __init__(self, paths: list[Path], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._paths = paths
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def run(self) -> None:
        for path in self._paths:
            if self._stopping:
                break
            image = make_thumbnail(path)
            if not image.isNull():
                self.ready.emit(str(path), image)
        self.finished_all.emit()


class WallpaperCard(QFrame):
    clicked = Signal()

    def __init__(self, path: Path, is_current: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self._source_pixmap = QPixmap()
        self.has_image = False

        self.setObjectName("card")
        self.setProperty("selected", "false")
        self.setFixedWidth(CARD_W)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(CARD_PAD, CARD_PAD, CARD_PAD, CARD_PAD)
        layout.setSpacing(7)

        self.thumb = QLabel("Cargando…")
        self.thumb.setObjectName("thumb")
        self.thumb.setFixedSize(THUMB_DISPLAY_W, THUMB_DISPLAY_H)
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.badge = QLabel("Actual", self.thumb)
        self.badge.setObjectName("badge")
        self.badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.badge.setFont(self._badge_font())
        self.badge.adjustSize()
        self.badge.move(8, THUMB_DISPLAY_H - self.badge.height() - 8)
        self.badge.setVisible(is_current)

        layout.addWidget(self.thumb)

    @staticmethod
    def _badge_font() -> QFont:
        font = QFont()
        font.setPointSizeF(7.5)
        font.setWeight(QFont.Weight.DemiBold)
        return font

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_image(self, image: QImage) -> None:
        if image.isNull():
            return
        self._source_pixmap = QPixmap.fromImage(image)
        self.has_image = True
        self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        if self._source_pixmap.isNull():
            return
        scaled = self._source_pixmap.scaled(
            self.thumb.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - self.thumb.width()) // 2)
        y = max(0, (scaled.height() - self.thumb.height()) // 2)
        self.thumb.setPixmap(scaled.copy(x, y, self.thumb.width(), self.thumb.height()))
        self.thumb.setText("")

    def mousePressEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class WallpaperView(QWidget):
    selection_changed = Signal(int)
    apply_requested = Signal(int)

    def set_items(self, items: list[WallpaperItem], thumbnails: dict[str, QImage],
                  current_paths: set[str]) -> None:
        raise NotImplementedError

    def set_thumbnail(self, index: int, image: QImage) -> None:
        raise NotImplementedError

    def set_selected(self, index: int, ensure: bool = True) -> None:
        raise NotImplementedError

    def select_index(self, index: int) -> None:
        self.set_selected(index)

    def move(self, dx: int, dy: int) -> None:
        raise NotImplementedError

    def current_index(self) -> int:
        return 0

    def hint(self) -> str:
        return ""

    def activate(self) -> None:
        pass

    def deactivate(self) -> None:
        pass


class GridView(WallpaperView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.items: list[WallpaperItem] = []
        self.cards: list[WallpaperCard] = []
        self.selected = 0
        self.columns = 4

        self._relayout_timer = QTimer(self)
        self._relayout_timer.setSingleShot(True)
        self._relayout_timer.setInterval(60)
        self._relayout_timer.timeout.connect(self._relayout_if_needed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setAutoFillBackground(False)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.verticalScrollBar().setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.canvas = QWidget()
        self.canvas.setObjectName("canvas")
        self.grid = QGridLayout(self.canvas)
        self.grid.setContentsMargins(4, 4, 4, 4)
        self.grid.setSpacing(GRID_SPACING)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        self.empty_label = QLabel("")
        self.empty_label.setObjectName("empty")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setMinimumHeight(260)
        self.empty_label.hide()

        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll)

    def set_items(self, items: list[WallpaperItem], thumbnails: dict[str, QImage],
                  current_paths: set[str]) -> None:
        self._clear_cards()
        self.items = list(items)
        self.cards = []
        for index, item in enumerate(self.items):
            card = WallpaperCard(item.path, item.is_current, self.canvas)
            card.clicked.connect(partial(self._on_card_clicked, index))
            image = thumbnails.get(str(item.path))
            if image is not None:
                card.set_image(image)
            self.cards.append(card)
        self.selected = 0
        self._relayout()
        if self.cards:
            self.set_selected(0, ensure=False)

    def set_thumbnail(self, index: int, image: QImage) -> None:
        if 0 <= index < len(self.cards):
            self.cards[index].set_image(image)

    def _clear_cards(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.empty_label:
                widget.hide()
                widget.deleteLater()
        self.cards = []
        self.empty_label.hide()

    def _columns(self) -> int:
        available = self.scroll.viewport().width()
        if available < 320:
            available = self.width() - 40
        return max(1, (available - 8 + GRID_SPACING) // (CARD_W + GRID_SPACING))

    def _relayout(self) -> None:
        self.columns = self._columns()
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()

        if not self.cards:
            if not self.folder_message():
                pass
            self.grid.addWidget(self.empty_label, 0, 0)
            self.empty_label.show()
            return

        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // self.columns, index % self.columns)
            card.show()

    def folder_message(self) -> str:
        return self.empty_label.text()

    def _relayout_if_needed(self) -> None:
        if self._columns() != self.columns:
            selected = self.selected
            self._relayout()
            if self.cards:
                self.set_selected(min(max(selected, 0), len(self.cards) - 1))

    def _on_card_clicked(self, index: int) -> None:
        self.set_selected(index)
        self.selection_changed.emit(index)
        self.apply_requested.emit(index)

    def set_selected(self, index: int, ensure: bool = True) -> None:
        if not self.cards:
            self.selected = 0
            return
        index = min(max(index, 0), len(self.cards) - 1)
        if 0 <= self.selected < len(self.cards):
            self.cards[self.selected].set_selected(False)
        self.selected = index
        card = self.cards[index]
        card.set_selected(True)
        if ensure:
            self.scroll.ensureWidgetVisible(card, 24, 24)

    def select_index(self, index: int) -> None:
        self.set_selected(index)
        self.selection_changed.emit(self.selected)

    def move(self, dx: int, dy: int) -> None:
        if not self.cards:
            return
        index = self.selected + dx + dy * self.columns
        index = min(max(index, 0), len(self.cards) - 1)
        if index != self.selected:
            self.set_selected(index)
            self.selection_changed.emit(index)

    def current_index(self) -> int:
        return self.selected

    def hint(self) -> str:
        return "←→ mover · ↑↓ filas · Enter aplicar · Esc cerrar · Shift/1/2/3 vista · Ctrl+R recargar"

    def resizeEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        super().resizeEvent(event)
        self._relayout_timer.start()


class CarouselView(WallpaperView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.items: list[WallpaperItem] = []
        self.thumbnails: dict[str, QImage] = {}
        self.pixmaps: dict[int, QPixmap] = {}
        self.offsets = [0.0, 0.0]
        self.targets: list[float | None] = [None, None]
        self.active_belt = 0
        self.hover_index = -1
        self.selected = 0
        self.cell_w = 356
        self.cell_h = 230
        self._hit_rects: list[tuple[int, QRectF]] = []
        self._pause_until = 0
        self.clock = QElapsedTimer()

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self._tick)

    def _belt_indices(self, belt: int) -> list[int]:
        return [index for index in range(len(self.items)) if index % 2 == belt]

    def _stride(self) -> float:
        return self.cell_w + CAROUSEL_GAP

    def _update_cell_size(self) -> bool:
        row_height = (max(self.height(), 300) - CAROUSEL_ROW_GAP) / 2.0
        cell_h = int(min(290, max(140, row_height - 8)))
        cell_w = int(cell_h * 16.0 / 9.0)
        cell_w = min(cell_w, max(240, int(self.width() * 0.38)))
        cell_h = int(cell_w * 9.0 / 16.0)
        if cell_w == self.cell_w and cell_h == self.cell_h:
            return False
        self.cell_w = cell_w
        self.cell_h = cell_h
        return True

    @staticmethod
    def _cover_pixmap(image: QImage, width: int, height: int) -> QPixmap:
        pixmap = QPixmap.fromImage(image)
        scaled = pixmap.scaled(
            QSize(width, height),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - width) // 2)
        y = max(0, (scaled.height() - height) // 2)
        return scaled.copy(x, y, width, height)

    def _rebuild_pixmaps(self) -> None:
        self.pixmaps = {}
        for index, item in enumerate(self.items):
            image = self.thumbnails.get(str(item.path))
            if image is not None:
                self.pixmaps[index] = self._cover_pixmap(image, self.cell_w, self.cell_h)

    def set_items(self, items: list[WallpaperItem], thumbnails: dict[str, QImage],
                  current_paths: set[str]) -> None:
        self.items = list(items)
        self.thumbnails = dict(thumbnails)
        self.hover_index = -1
        self.selected = 0
        self.targets = [None, None]
        self._update_cell_size()
        self._rebuild_pixmaps()
        if self.items:
            for belt in (0, 1):
                index = belt if belt < len(self.items) else 0
                self.offsets[belt] = self._center_offset(index)
            self.set_selected(0)
        else:
            self.offsets = [0.0, 0.0]
        self.update()

    def set_thumbnail(self, index: int, image: QImage) -> None:
        if not (0 <= index < len(self.items)):
            return
        item = self.items[index]
        self.thumbnails[str(item.path)] = image
        self.pixmaps[index] = self._cover_pixmap(image, self.cell_w, self.cell_h)
        self.update()

    def _center_offset(self, index: int) -> float:
        indices = self._belt_indices(index % 2)
        if not indices or index not in indices:
            return 0.0
        position = indices.index(index)
        return position * self._stride() + self.cell_w / 2 - self.width() / 2

    def _center_index(self, belt: int) -> int:
        indices = self._belt_indices(belt)
        if not indices:
            return 0
        length = len(indices) * self._stride()
        offset = self.offsets[belt] % length
        position = round((offset + self.width() / 2 - self.cell_w / 2) / self._stride())
        return indices[position % len(indices)]

    def set_selected(self, index: int, ensure: bool = True) -> None:
        if not self.items:
            self.selected = 0
            return
        index = min(max(index, 0), len(self.items) - 1)
        self.selected = index
        self.active_belt = index % 2
        self.offsets[self.active_belt] = self._center_offset(index)
        self.targets = [None, None]
        self.hover_index = -1
        self.update()

    def select_index(self, index: int) -> None:
        self.set_selected(index)
        self.selection_changed.emit(self.selected)

    def move(self, dx: int, dy: int) -> None:
        if not self.items:
            return
        self.hover_index = -1
        self._pause_until = 1.5

        if dy:
            self.active_belt = 1 - self.active_belt
            self.selected = self._center_index(self.active_belt)
            self.selection_changed.emit(self.selected)

        if dx:
            belt = self.active_belt
            if self.targets[belt] is None:
                self.targets[belt] = self.offsets[belt]
            self.targets[belt] += dx * self._stride()
            indices = self._belt_indices(belt)
            if indices:
                length = len(indices) * self._stride()
                offset = self.targets[belt] % length
                position = round((offset + self.width() / 2 - self.cell_w / 2) / self._stride())
                self.selected = indices[position % len(indices)]
                self.selection_changed.emit(self.selected)

        self.update()

    def current_index(self) -> int:
        if not self.items:
            return 0
        if self.hover_index >= 0:
            return self.hover_index
        if self.targets[self.active_belt] is not None:
            return self.selected
        self.selected = self._center_index(self.active_belt)
        return self.selected

    def hint(self) -> str:
        return "←→ mover · ↑↓ cinta · Enter aplicar · Esc cerrar · Shift/1/2/3 vista · Ctrl+R recargar"

    def activate(self) -> None:
        if not self.clock.isValid():
            self.clock.start()
        self.clock.restart()
        self._pause_until = 0
        if self.items:
            self.timer.start()

    def deactivate(self) -> None:
        self.timer.stop()

    def resizeEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        super().resizeEvent(event)
        changed = self._update_cell_size()
        if changed and self.items:
            self._rebuild_pixmaps()
            self.set_selected(self.selected)
        self.update()


    def _tick(self) -> None:
        if not self.isVisible() or not self.items:
            return
        if not self.clock.isValid():
            self.clock.start()
        dt = min(0.05, max(0.001, self.clock.restart() / 1000.0))
        self._pause_until = max(0.0, self._pause_until - dt)
        moved = False

        for belt in (0, 1):
            indices = self._belt_indices(belt)
            if not indices:
                continue
            target = self.targets[belt]
            if target is not None:
                difference = target - self.offsets[belt]
                if abs(difference) < 0.5:
                    self.offsets[belt] = target
                    self.targets[belt] = None
                else:
                    self.offsets[belt] += difference * min(1.0, dt * 12.0)
                    moved = True
            elif self.hover_index < 0 and self._pause_until <= 0.0:
                direction = -1.0 if belt == 0 else 1.0
                self.offsets[belt] += direction * CAROUSEL_SPEED * dt
                moved = True

        if (self.hover_index < 0 and self.targets[self.active_belt] is None):
            center = self._center_index(self.active_belt)
            if center != self.selected:
                self.selected = center
                self.selection_changed.emit(center)

        if moved:
            self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self._hit_rects = []

        if not self.items:
            painter.setPen(QColor("#6f6f7b"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No hay fondos")
            return

        for belt in (0, 1):
            self._paint_belt(painter, belt)

    def _paint_belt(self, painter: QPainter, belt: int) -> None:
        indices = self._belt_indices(belt)
        if not indices:
            return
        stride = self._stride()
        length = len(indices) * stride
        offset = self.offsets[belt] % length
        group_height = 2.0 * self.cell_h + CAROUSEL_ROW_GAP
        group_top = max(0.0, (self.height() - group_height) / 2.0)
        row_top = group_top + belt * (self.cell_h + CAROUSEL_ROW_GAP)
        y = row_top

        painter.save()
        painter.setClipRect(
            QRectF(0, row_top - CAROUSEL_ROW_GAP / 2.0,
                   self.width(), self.cell_h + CAROUSEL_ROW_GAP)
        )
        start = int(offset / stride) - 1
        count = int(self.width() / stride) + 4
        for slot in range(count):
            position = start + slot
            index = indices[position % len(indices)]
            x = position * stride - offset
            rect = QRectF(x, y, self.cell_w, self.cell_h)
            if rect.right() < -4 or rect.left() > self.width() + 4:
                continue
            highlighted = (
                index == self.hover_index
                if self.hover_index >= 0
                else (index == self.selected and belt == self.active_belt)
            )
            self._draw_card(painter, index, rect, highlighted)
            self._hit_rects.append((index, rect))
        painter.restore()

        if belt == self.active_belt and self.hover_index < 0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(61, 174, 233, 150))
            painter.drawRoundedRect(
                QRectF(self.width() / 2 - 18, y + self.cell_h + 6, 36, 2), 1, 1
            )

    def _draw_card(self, painter: QPainter, index: int, rect: QRectF, highlighted: bool) -> None:
        item = self.items[index]
        path = QPainterPath()
        path.addRoundedRect(rect, 11, 11)

        painter.save()
        painter.setClipPath(path)
        painter.fillPath(path, QColor("#0d0d11"))
        pixmap = self.pixmaps.get(index)
        if pixmap is not None and not pixmap.isNull():
            painter.drawPixmap(rect.toRect(), pixmap)
        painter.restore()

        if highlighted:
            pen = QPen(QColor("#3daee9"), 2.4)
        else:
            pen = QPen(QColor("#2b2b33"), 1.2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0.6, 0.6, -0.6, -0.6), 11, 11)

        if item.is_current:
            badge = QRectF(rect.left() + 10, rect.bottom() - 30, 56, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#2e7d32"))
            painter.drawRoundedRect(badge, 6, 6)
            font = QFont()
            font.setPointSizeF(7.5)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor("#ffffff"))
            painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "Actual")

    def _hit_index(self, position: QPointF) -> int:
        for index, rect in reversed(self._hit_rects):
            if rect.contains(position):
                return index
        return -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        index = self._hit_index(event.position())
        if index != self.hover_index:
            self.hover_index = index
            if index >= 0:
                self.selection_changed.emit(index)
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        if self.hover_index >= 0:
            self.hover_index = -1
            self._pause_until = 0.6
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        if event.button() == Qt.MouseButton.LeftButton:
            index = self._hit_index(event.position())
            if index >= 0:
                self.hover_index = index
                self.selected = index
                self.selection_changed.emit(index)
                self.apply_requested.emit(index)
                return
        super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        steps = event.angleDelta().y() / 120.0
        if steps:
            self.offsets[0] -= steps * self._stride() * 0.6
            self.offsets[1] += steps * self._stride() * 0.6
            self.targets = [None, None]
            self.hover_index = -1
            self._pause_until = 1.2
            if self._belt_indices(self.active_belt):
                self.selected = self._center_index(self.active_belt)
                self.selection_changed.emit(self.selected)
            self.update()
            event.accept()
            return
        super().wheelEvent(event)


class HexCanvas(QWidget):
    def __init__(self, view: "HexView") -> None:
        super().__init__()
        self.view = view
        self.layout_items: list[tuple[int, QPointF, QPainterPath, QRectF]] = []
        self.columns = 4
        self.setObjectName("canvas")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    @staticmethod
    def _hex_path(cx: float, cy: float, rx: float, ry: float) -> QPainterPath:
        path = QPainterPath()
        points = [
            (0.0, -ry),
            (rx, -ry / 2.0),
            (rx, ry / 2.0),
            (0.0, ry),
            (-rx, ry / 2.0),
            (-rx, -ry / 2.0),
        ]
        path.moveTo(cx + points[0][0], cy + points[0][1])
        for x, y in points[1:]:
            path.lineTo(cx + x, cy + y)
        path.closeSubpath()
        return path

    def update_layout(self) -> None:
        count = len(self.view.items)
        width = max(self.width(), 700)
        if count == 0:
            self.layout_items = []
            self.setMinimumHeight(420)
            self.update()
            return

        margin = HEX_MARGIN
        gap = HEX_GAP
        hex_w = HEX_WIDTH
        hex_h = HEX_HEIGHT
        columns = max(1, int((width - 2 * margin + gap) // (hex_w + gap)))
        while columns > 1 and (
            margin + (hex_w + gap) / 2 + (columns - 1) * (hex_w + gap) + hex_w
            > width - margin
        ):
            columns -= 1

        rows = (count + columns - 1) // columns
        total_height = 2 * margin + hex_h + (rows - 1) * (0.75 * hex_h + gap)
        minimum = int(math.ceil(total_height))
        if self.minimumHeight() != minimum:
            self.setMinimumHeight(minimum)

        self.columns = columns
        self.view.columns = columns
        step = hex_w + gap
        span = columns * step - gap + step / 2.0
        x0 = max(margin, (width - span) / 2.0)
        self.layout_items = []
        for index in range(count):
            row = index // columns
            column = index % columns
            cx = x0 + hex_w / 2 + column * step + (row % 2) * step / 2.0
            cy = margin + hex_h / 2 + row * (0.75 * hex_h + gap)
            path = self._hex_path(cx, cy, hex_w / 2, hex_h / 2)
            rect = QRectF(cx - hex_w / 2, cy - hex_h / 2, hex_w, hex_h)
            self.layout_items.append((index, QPointF(cx, cy), path, rect))
        self.update()

    def rect_for(self, index: int) -> QRectF | None:
        for item_index, _center, _path, rect in self.layout_items:
            if item_index == index:
                return rect
        return None

    def hit_test(self, position: QPointF) -> int:
        for index, _center, path, _rect in reversed(self.layout_items):
            if path.contains(position):
                return index
        return -1

    def paintEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if not self.view.items:
            painter.setPen(QColor("#6f6f7b"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No hay fondos")
            return

        visible = QRectF(event.rect()).adjusted(-4, -4, 4, 4)
        for index, center, path, rect in self.layout_items:
            if not visible.intersects(rect):
                continue
            item = self.view.items[index]
            painter.save()
            painter.setClipPath(path)
            painter.fillPath(path, QColor("#0d0d11"))
            pixmap = self.view.pixmaps.get(index)
            if pixmap is not None and not pixmap.isNull():
                painter.drawPixmap(rect.toRect(), pixmap)
            painter.restore()

            if index == self.view.selected:
                pen = QPen(QColor("#3daee9"), 3.0)
            elif index == self.view.hover_index:
                pen = QPen(QColor("#5a5a68"), 2.0)
            elif item.is_current:
                pen = QPen(QColor("#2e7d32"), 2.0)
            else:
                pen = QPen(QColor("#2b2b33"), 1.2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

            if item.is_current:
                badge = QRectF(center.x() - 28, center.y() + HEX_HEIGHT * 0.18, 56, 20)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor("#2e7d32"))
                painter.drawRoundedRect(badge, 6, 6)
                font = QFont()
                font.setPointSizeF(7.5)
                font.setWeight(QFont.Weight.DemiBold)
                painter.setFont(font)
                painter.setPen(QColor("#ffffff"))
                painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, "Actual")

    def resizeEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        super().resizeEvent(event)
        self.update_layout()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        index = self.hit_test(event.position())
        if index != self.view.hover_index:
            self.view.hover_index = index
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        if self.view.hover_index >= 0:
            self.view.hover_index = -1
            self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        if event.button() == Qt.MouseButton.LeftButton:
            index = self.hit_test(event.position())
            if index >= 0:
                self.view.choose(index)
                return
        super().mousePressEvent(event)


class HexView(WallpaperView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.items: list[WallpaperItem] = []
        self.pixmaps: dict[int, QPixmap] = {}
        self.selected = 0
        self.hover_index = -1
        self.columns = 4

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.scroll.setAutoFillBackground(False)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.verticalScrollBar().setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.canvas = HexCanvas(self)
        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll)

    @staticmethod
    def _cover_pixmap(image: QImage, width: int, height: int) -> QPixmap:
        pixmap = QPixmap.fromImage(image)
        scaled = pixmap.scaled(
            QSize(width, height),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        x = max(0, (scaled.width() - width) // 2)
        y = max(0, (scaled.height() - height) // 2)
        return scaled.copy(x, y, width, height)

    def set_items(self, items: list[WallpaperItem], thumbnails: dict[str, QImage],
                  current_paths: set[str]) -> None:
        self.items = list(items)
        self.pixmaps = {}
        self.selected = 0
        self.hover_index = -1
        for index, item in enumerate(self.items):
            image = thumbnails.get(str(item.path))
            if image is not None:
                self.pixmaps[index] = self._cover_pixmap(
                    image, int(HEX_WIDTH), int(HEX_HEIGHT)
                )
        self.canvas.update_layout()
        if self.items:
            self.set_selected(0, ensure=False)
        self.scroll.verticalScrollBar().setValue(0)

    def set_thumbnail(self, index: int, image: QImage) -> None:
        if 0 <= index < len(self.items):
            self.pixmaps[index] = self._cover_pixmap(
                image, int(HEX_WIDTH), int(HEX_HEIGHT)
            )
            self.canvas.update()

    def set_selected(self, index: int, ensure: bool = True) -> None:
        if not self.items:
            self.selected = 0
            return
        index = min(max(index, 0), len(self.items) - 1)
        self.selected = index
        self.canvas.update()
        if ensure:
            rect = self.canvas.rect_for(index)
            if rect is not None:
                center = rect.center()
                self.scroll.ensureVisible(int(center.x()), int(center.y()), 60, 60)

    def select_index(self, index: int) -> None:
        self.set_selected(index)
        self.selection_changed.emit(self.selected)

    def move(self, dx: int, dy: int) -> None:
        if not self.items:
            return
        index = self.selected + dx + dy * max(1, self.columns)
        index = min(max(index, 0), len(self.items) - 1)
        if index != self.selected:
            self.set_selected(index)
            self.selection_changed.emit(index)

    def choose(self, index: int) -> None:
        self.set_selected(index)
        self.selection_changed.emit(index)
        self.apply_requested.emit(index)

    def current_index(self) -> int:
        return self.selected

    def hint(self) -> str:
        return "←→ mover · ↑↓ filas · Enter aplicar · Esc cerrar · Shift/1/2/3 vista · Ctrl+R recargar"


class PickerWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.folder = wallpaper_dir()
        self.items: list[WallpaperItem] = []
        self.thumbnails: dict[str, QImage] = {}
        self.index_by_path: dict[str, int] = {}
        self.current_paths: set[str] = set()
        self.selected = 0
        self.mode = 0
        self.loader: ThumbnailLoader | None = None
        self._shift_armed = False
        self._shift_consumed = False

        self.settings = QSettings(str(CONFIG_FILE), QSettings.Format.IniFormat)
        self._saved_mode = self.mode_index(str(self.settings.value("view_mode", "grid")))

        self.setWindowTitle("Selector de fondos")
        self.setWindowIcon(QIcon.fromTheme("preferences-desktop-wallpaper"))
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(860, 560)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(1280, max(900, available.width() - 160))
            height = min(860, max(580, available.height() - 120))
            self.resize(width, height)
        else:
            self.resize(1280, 860)

        self._build_ui()
        self.reload()
        self.set_mode(self._saved_mode, persist=False)

    @staticmethod
    def mode_index(name: str) -> int:
        for index, (key, _label) in enumerate(MODES):
            if key == name:
                return index
        return 0

    def active_view(self) -> WallpaperView:
        return self.views[self.mode]

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        panel = QFrame()
        panel.setObjectName("panel")
        panel.setAutoFillBackground(False)
        outer.addWidget(panel)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(24, 20, 24, 14)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("Fondos de pantalla")
        title.setObjectName("title")
        title_font = QFont()
        title_font.setPointSizeF(15.0)
        title_font.setWeight(QFont.Weight.DemiBold)
        title.setFont(title_font)

        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons: list[QPushButton] = []
        for index, (_key, label) in enumerate(MODES):
            button = QPushButton(label)
            button.setObjectName("viewBtn")
            button.setCheckable(True)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(f"{label.strip()}  ·  tecla {index + 1}  ·  Shift cambia de vista")
            button.clicked.connect(partial(self.set_mode, index))
            self.mode_group.addButton(button, index)
            self.mode_buttons.append(button)
            header.addWidget(button)

        close_button = QPushButton("✕")
        close_button.setObjectName("closeBtn")
        close_button.setFixedSize(30, 30)
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_button.setToolTip("Cerrar (Esc)")
        close_button.clicked.connect(self.close)

        header.insertWidget(0, title)
        header.insertStretch(1)
        header.addWidget(close_button)
        layout.addLayout(header)

        self.stack = QStackedWidget()
        self.stack.setObjectName("stack")
        self.stack.setAutoFillBackground(False)
        self.stack.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self.grid_view = GridView()
        self.carousel_view = CarouselView()
        self.hex_view = HexView()
        self.views = [self.grid_view, self.carousel_view, self.hex_view]
        for view in self.views:
            view.selection_changed.connect(
                lambda index, current=view: self._on_selection_changed(current, index)
            )
            view.apply_requested.connect(self._apply_index)
            self.stack.addWidget(view)

        self.empty_page = QLabel("")
        self.empty_page.setObjectName("empty")
        self.empty_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_page.setWordWrap(True)
        empty_font = QFont()
        empty_font.setPointSizeF(11.0)
        self.empty_page.setFont(empty_font)
        self.stack.addWidget(self.empty_page)
        layout.addWidget(self.stack, 1)

        footer = QHBoxLayout()
        footer.setSpacing(16)
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("hint")
        self.count_label = QLabel("")
        self.count_label.setObjectName("count")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        footer_font = QFont()
        footer_font.setPointSizeF(8.5)
        for label in (self.hint_label, self.count_label):
            label.setFont(footer_font)

        footer.addWidget(self.hint_label, 0)
        footer.addStretch(1)
        footer.addWidget(self.count_label, 0)
        layout.addLayout(footer)

        self.setStyleSheet(
            """
            QFrame#panel {
                background-color: transparent;
                border: none;
            }
            QLabel#title {
                color: #f7f7fa;
                background-color: rgba(14, 14, 18, 150);
                border: 1px solid rgba(255, 255, 255, 22);
                border-radius: 9px;
                padding: 5px 12px;
            }
            QPushButton#viewBtn {
                background-color: rgba(14, 14, 18, 150);
                border: 1px solid rgba(255, 255, 255, 26);
                border-radius: 8px;
                padding: 6px 12px;
                color: #b8b8c4;
            }
            QPushButton#viewBtn:hover {
                background-color: rgba(38, 38, 48, 190);
                color: #f2f2f7;
            }
            QPushButton#viewBtn:checked {
                background-color: rgba(31, 48, 62, 210);
                border-color: #3daee9;
                color: #f7f7fa;
            }
            QPushButton#closeBtn {
                background-color: rgba(14, 14, 18, 150);
                border: 1px solid rgba(255, 255, 255, 22);
                border-radius: 8px;
                color: #b8b8c4;
                font-size: 14px;
            }
            QPushButton#closeBtn:hover {
                background-color: rgba(38, 38, 48, 190);
                color: #f7f7fa;
            }
            QStackedWidget#stack, QWidget#canvas, QScrollArea,
            QScrollArea > QWidget, QScrollArea > QWidget > QWidget {
                background: transparent;
                border: none;
            }
            QFrame#card {
                background-color: #1c1c22;
                border: 2px solid #2b2b33;
                border-radius: 11px;
            }
            QFrame#card:hover {
                background-color: #23232b;
                border-color: #454552;
            }
            QFrame#card[selected="true"] {
                border-color: #3daee9;
                background-color: #1f2730;
            }
            QLabel#thumb {
                background-color: #0d0d11;
                border-radius: 7px;
                color: #4b4b57;
            }
            QLabel#badge {
                background-color: #2e7d32;
                color: #ffffff;
                border-radius: 6px;
                padding: 2px 7px;
            }
            QLabel#hint, QLabel#count {
                color: #d6d6de;
                background-color: rgba(14, 14, 18, 150);
                border-radius: 6px;
                padding: 2px 8px;
            }
            QLabel#empty {
                color: #d6d6de;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 10px;
                margin: 4px 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(90, 90, 108, 200);
                border-radius: 5px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(120, 120, 140, 220);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            """
        )


    def _on_selection_changed(self, view: WallpaperView, index: int) -> None:
        if view is not self.active_view():
            return
        self.selected = index

    def _update_footer(self) -> None:
        if self.items:
            self.hint_label.setText(self.active_view().hint())
            self.count_label.setText(f"{len(self.items)} fondos")
        else:
            self.hint_label.setText("")
            self.count_label.setText("0 fondos")

    def _update_buttons(self) -> None:
        for index, button in enumerate(self.mode_buttons):
            button.setChecked(index == self.mode)

    def set_mode(self, index: int, persist: bool = True) -> None:
        if not 0 <= index < len(self.views):
            return
        old_view = self.active_view()
        new_view = self.views[index]
        if old_view is not new_view:
            if self.items:
                self.selected = old_view.current_index()
            old_view.deactivate()

        self.mode = index
        if self.items:
            self.stack.setCurrentWidget(new_view)
            new_view.set_selected(self.selected)
            if self.isVisible():
                new_view.activate()
        else:
            self.stack.setCurrentWidget(self.empty_page)

        self._update_buttons()
        self._update_footer()
        if persist:
            self.settings.setValue("view_mode", MODES[index][0])
            self.settings.sync()
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def reload(self) -> None:
        self._stop_loader()
        self.current_paths = current_wallpaper_paths()
        paths = scan_wallpapers(self.folder)
        prune_cache(paths)

        self.thumbnails.clear()
        self.items = []
        for path in paths:
            try:
                resolved = str(path.resolve())
            except OSError:
                resolved = str(path)
            self.items.append(WallpaperItem(path, path.name, resolved in self.current_paths))
        self.index_by_path = {str(item.path): index for index, item in enumerate(self.items)}
        self.selected = 0

        for view in self.views:
            view.set_items(self.items, {}, self.current_paths)

        if not self.items:
            if not self.folder.is_dir():
                message = f"No se encontró la carpeta:\n{self.folder}"
            else:
                message = f"No hay imágenes en:\n{self.folder}"
            self.empty_page.setText(message)
            self.grid_view.empty_label.setText(message)
            self.stack.setCurrentWidget(self.empty_page)
            self._update_footer()
            return

        self.stack.setCurrentWidget(self.views[self.mode])
        self.views[self.mode].set_selected(0)
        self._update_footer()

        self.loader = ThumbnailLoader([item.path for item in self.items], self)
        self.loader.ready.connect(self._on_thumbnail)
        self.loader.start()

        if self.isVisible():
            self.views[self.mode].activate()

    def _stop_loader(self) -> None:
        if self.loader is not None:
            self.loader.stop()
            if not self.loader.wait(8000):
                self.loader.terminate()
                self.loader.wait(1000)
            self.loader = None

    def _on_thumbnail(self, path: str, image: QImage) -> None:
        self.thumbnails[path] = image
        index = self.index_by_path.get(path)
        if index is None:
            return
        for view in self.views:
            view.set_thumbnail(index, image)

    def _apply_index(self, index: int) -> None:
        if 0 <= index < len(self.items):
            self._apply_path(str(self.items[index].path))

    def _apply_path(self, path_str: str) -> None:
        path = Path(path_str)
        if not path.is_file():
            send_notification(
                "Fondo de pantalla",
                f"No se encontró el archivo:\n{path}",
                urgent=True,
            )
            return

        self.hide()
        QApplication.processEvents()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        ok = False
        detail = ""
        try:
            result = subprocess.run(
                [PLASMA_APPLY, str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            ok = result.returncode == 0
            detail = (result.stderr or result.stdout or "").strip()
        except FileNotFoundError:
            detail = f"No se encontró {PLASMA_APPLY}"
        except subprocess.TimeoutExpired:
            detail = "El comando de Plasma tardó demasiado en responder."
        except OSError as error:
            detail = str(error)
        finally:
            QApplication.restoreOverrideCursor()

        if ok:
            send_notification(
                "Fondo de pantalla",
                "Fondo de pantalla cambiado.",
                icon=str(path),
            )
            self.close()
            return

        send_notification(
            "Error al cambiar el fondo",
            detail or "plasma-apply-wallpaperimage falló sin dar detalles.",
            urgent=True,
        )
        self.show()
        self.raise_()
        self.activateWindow()
        if self.windowHandle() is not None:
            self.windowHandle().requestActivate()
        self.setFocus()

    def show_picker(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        if self.windowHandle() is not None:
            self.windowHandle().requestActivate()
        self.setFocus(Qt.FocusReason.ShortcutFocusReason)
        if self.items:
            self.views[self.mode].activate()

    def keyPressEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        key = event.key()
        view = self.active_view()

        if key == Qt.Key.Key_Shift:
            if not event.isAutoRepeat():
                self._shift_armed = True
                self._shift_consumed = False
            event.accept()
            return
        if self._shift_armed and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self._shift_consumed = True

        if key == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._apply_index(view.current_index())
            event.accept()
            return
        if key == Qt.Key.Key_Left:
            view.move(-1, 0)
            event.accept()
            return
        if key == Qt.Key.Key_Right:
            view.move(1, 0)
            event.accept()
            return
        if key == Qt.Key.Key_Up:
            view.move(0, -1)
            event.accept()
            return
        if key == Qt.Key.Key_Down:
            view.move(0, 1)
            event.accept()
            return
        if key == Qt.Key.Key_Home and self.items:
            view.select_index(0)
            event.accept()
            return
        if key == Qt.Key.Key_End and self.items:
            view.select_index(len(self.items) - 1)
            event.accept()
            return
        if key == Qt.Key.Key_PageUp:
            view.move(0, -3)
            event.accept()
            return
        if key == Qt.Key.Key_PageDown:
            view.move(0, 3)
            event.accept()
            return
        if key == Qt.Key.Key_R and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.reload()
            event.accept()
            return
        if key in (Qt.Key.Key_1, Qt.Key.Key_2, Qt.Key.Key_3):
            self.set_mode(key - Qt.Key.Key_1)
            event.accept()
            return
        if key == Qt.Key.Key_V:
            self.set_mode((self.mode + 1) % len(self.views))
            event.accept()
            return

        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        if event.key() == Qt.Key.Key_Shift:
            if not event.isAutoRepeat():
                armed = self._shift_armed
                consumed = self._shift_consumed
                self._shift_armed = False
                self._shift_consumed = False
                if armed and not consumed:
                    self.set_mode((self.mode + 1) % len(self.views))
            event.accept()
            return
        super().keyReleaseEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        super().showEvent(event)
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        if self.items:
            self.views[self.mode].activate()

    def hideEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        self.views[self.mode].deactivate()
        super().hideEvent(event)

    def changeEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        if event.type() == QEvent.Type.ActivationChange and hasattr(self, "views"):
            if self.isActiveWindow() and self.isVisible() and self.items:
                self.views[self.mode].activate()
            else:
                self.views[self.mode].deactivate()
        super().changeEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        super().mousePressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802 (API de Qt)
        self._stop_loader()
        for view in self.views:
            view.deactivate()
        super().closeEvent(event)


class SingleInstance(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.window: PickerWindow | None = None
        self.is_primary = False
        self.server: QLocalServer | None = None
        self.socket = QLocalSocket(self)
        self.socket.connectToServer(SERVER_NAME)
        if self.socket.waitForConnected(200):
            self.socket.write(b"show\n")
            self.socket.waitForBytesWritten(200)
            self.socket.disconnectFromServer()
            return

        QLocalServer.removeServer(SERVER_NAME)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._on_new_connection)
        if self.server.listen(SERVER_NAME):
            self.is_primary = True

    def set_window(self, window: PickerWindow) -> None:
        self.window = window

    def _on_new_connection(self) -> None:
        if self.server is None:
            return
        while self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            if connection is None:
                continue
            connection.readyRead.connect(lambda conn=connection: self._read(conn))
            connection.disconnected.connect(connection.deleteLater)
            if connection.bytesAvailable() > 0:
                self._read(connection)

    def _read(self, connection: QLocalSocket) -> None:
        data = bytes(connection.readAll())
        if b"show" in data and self.window is not None:
            self.window.show_picker()


def run_selftest(window: PickerWindow, app: QApplication) -> int:
    window.show()
    for index in range(len(MODES)):
        window.set_mode(index, persist=False)
        QApplication.processEvents()
    window.set_mode(0, persist=False)

    finished = False

    def finish() -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        loaded = len(window.thumbnails)
        print(f"folder={window.folder}")
        print(f"images={len(window.items)}")
        print(f"loaded={loaded}")
        print("views=grid,carousel,hex")
        print(f"cache={CACHE_DIR}")
        app.quit()

    if window.loader is not None:
        window.loader.finished_all.connect(finish)
    QTimer.singleShot(20000, finish)
    return app.exec()


def main() -> int:
    args = sys.argv[1:]
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("Selector de fondos")
    app.setDesktopFileName(APP_NAME)
    app.setWindowIcon(QIcon.fromTheme("preferences-desktop-wallpaper"))
    app.setQuitOnLastWindowClosed(True)

    if "--selftest" in args:
        return run_selftest(PickerWindow(), app)

    instance = SingleInstance()
    if not instance.is_primary:
        return 0

    window = PickerWindow()
    instance.set_window(window)
    if "--mode" in args:
        position = args.index("--mode")
        if position + 1 < len(args):
            window.set_mode(window.mode_index(args[position + 1]), persist=False)

    window.show_picker()
    return app.exec()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)

