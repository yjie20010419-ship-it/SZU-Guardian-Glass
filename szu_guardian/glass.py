"""Painted widgets for the v1.1 "glass" interface.

Every colour comes from one hue (the theme colour the user picks), so a theme
change only has to set ``THEME.hue``. Animations only tick while the window
is on screen, so the guardian costs nothing while it sits in the tray.
"""

from __future__ import annotations

import math
import time
import weakref

from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QConicalGradient,
    QCursor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QFrame, QPushButton, QWidget

OK_COLOR = QColor("#2BB5A0")
BAD_COLOR = QColor("#F06A6A")
WARN_COLOR = QColor("#E0A030")
DISPLAY_FONT = "Segoe UI Variable Display"
TEXT_FONT = "Microsoft YaHei UI"
DEFAULT_HUE = 207


def hsl(h: float, s: float, l: float, a: float = 1.0) -> QColor:
    return QColor.fromHslF((h % 360) / 360.0, max(0.0, min(1.0, s)), max(0.0, min(1.0, l)), a)


def with_alpha(color: QColor, alpha: float) -> QColor:
    c = QColor(color)
    c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


def mix(a: QColor, b: QColor, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
        a.alphaF() + (b.alphaF() - a.alphaF()) * t,
    )


def hue_of(color: str) -> int:
    """Hue of a saved theme colour; greys fall back to the default blue."""
    c = QColor(color)
    h = c.hslHue()
    return DEFAULT_HUE if h < 0 or c.hslSaturation() < 25 else h


def accent_for_hue(hue: int) -> str:
    return hsl(hue, 0.78, 0.57).name().upper()


class Palette:
    def __init__(self, hue: int, down: float = 0.0):
        h = hue
        self.hue = hue
        self.a = hsl(h, 0.78, 0.57)
        self.a2 = hsl(h, 0.86, 0.73)
        self.deep = hsl(h, 0.58, 0.20)
        self.mid = hsl(h, 0.70, 0.50)
        self.text = hsl(h, 0.40, 0.25)
        self.mute = hsl(h, 0.18, 0.50)
        self.faint = hsl(h, 0.20, 0.66)
        self.bg = hsl(h, 0.80, 0.97)
        self.tint = hsl(h, 0.78, 0.57, 0.14)
        self.tint2 = hsl(h, 0.78, 0.57, 0.22)
        self.island = hsl(h, 0.58, 0.15)
        self.blobs = [
            mix(hsl(h, 0.95, 0.87), QColor("#FFD6D6"), down),
            mix(hsl(h - 35, 0.60, 0.88), QColor("#FFE6D5"), down),
            hsl(h + 15, 0.90, 0.91),
        ]
        self.down = down
        self.status = mix(OK_COLOR, BAD_COLOR, down)


class Theme(QObject):
    """Shared colour state. ``down`` fades the page to warm red while offline."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self.hue = DEFAULT_HUE
        self.down = 0.0
        self.palette = Palette(self.hue)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(800)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(self._set_down)

    def set_hue(self, hue: int) -> None:
        self.hue = int(hue) % 360
        self._rebuild()

    def set_down(self, down: bool, animated: bool = True) -> None:
        target = 1.0 if down else 0.0
        if abs(target - self.down) < 1e-3:
            return
        self._anim.stop()
        if not animated:
            self._set_down(target)
            return
        self._anim.setStartValue(self.down)
        self._anim.setEndValue(target)
        self._anim.start()

    def _set_down(self, value) -> None:
        self.down = float(value)
        self._rebuild()

    def _rebuild(self) -> None:
        self.palette = Palette(self.hue, self.down)
        self.changed.emit()


THEME = Theme()


def display_font(px: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont([DISPLAY_FONT, TEXT_FONT])
    f.setPixelSize(round(px))
    f.setWeight(weight)
    return f


def text_font(px: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont([TEXT_FONT])
    f.setPixelSize(round(px))
    f.setWeight(weight)
    return f


def _visible_on_screen(widget: QWidget) -> bool:
    window = widget.window()
    return widget.isVisible() and window.isVisible() and not window.isMinimized()


class Backdrop(QWidget):
    """Window background: soft colour blobs that drift slowly, a faint grid
    and a beam of light from the top, all in the theme hue."""

    FRAME_MS = 80

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._t0 = time.monotonic()
        self._blob_cache: list[QPixmap] = []
        self._cache_key = None
        self.beam_x = 0.6
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        THEME.changed.connect(self._invalidate)

    def _invalidate(self) -> None:
        self._cache_key = None
        self.update()

    def _tick(self) -> None:
        if _visible_on_screen(self):
            self.update()

    def _blob(self, color: QColor, size: int) -> QPixmap:
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = QRadialGradient(size / 2, size / 2, size / 2)
        g.setColorAt(0.0, with_alpha(color, 0.95))
        g.setColorAt(0.45, with_alpha(color, 0.65))
        g.setColorAt(1.0, with_alpha(color, 0.0))
        p.fillRect(pm.rect(), g)
        p.end()
        return pm

    def _layers(self) -> None:
        pal = THEME.palette
        key = (pal.hue, round(pal.down, 2), self.width(), self.height())
        if key == self._cache_key:
            return
        self._cache_key = key
        self._blob_cache = [self._blob(c, s) for c, s in zip(pal.blobs, (640, 560, 520))]
        # Grid and beam fade out from the top, so they are drawn once per size.
        w, h = max(1, self.width()), max(1, self.height())
        grid = QPixmap(w, h)
        grid.fill(Qt.GlobalColor.transparent)
        p = QPainter(grid)
        p.setPen(QPen(pal.tint, 1))
        for x in range(0, w, 40):
            p.drawLine(x, 0, x, h)
        for y in range(0, h, 40):
            p.drawLine(0, y, w, y)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        mask = QRadialGradient(w * self.beam_x, 0, w * 0.6)
        mask.setColorAt(0.0, QColor(0, 0, 0, 200))
        mask.setColorAt(0.4, QColor(0, 0, 0, 150))
        mask.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(grid.rect(), mask)
        p.end()
        beam = QPixmap(w, h)
        beam.fill(Qt.GlobalColor.transparent)
        p = QPainter(beam)
        cx, cy = w * self.beam_x, -h * 0.35
        cone = QConicalGradient(cx, cy, 0)
        side = with_alpha(pal.a2, 0.30)
        cone.setColorAt(0.0, QColor(0, 0, 0, 0))
        cone.setColorAt(0.70, QColor(0, 0, 0, 0))
        cone.setColorAt(0.728, side)
        cone.setColorAt(0.75, QColor(255, 255, 255, 215))
        cone.setColorAt(0.772, side)
        cone.setColorAt(0.80, QColor(0, 0, 0, 0))
        cone.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(beam.rect(), cone)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        fade = QLinearGradient(0, 0, 0, h * 0.75)
        fade.setColorAt(0.0, QColor(0, 0, 0, 210))
        fade.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(beam.rect(), fade)
        p.end()
        self._grid, self._beam = grid, beam

    def paintEvent(self, event) -> None:
        self._layers()
        pal = THEME.palette
        p = QPainter(self)
        p.fillRect(self.rect(), pal.bg)
        t = time.monotonic() - self._t0
        w, h = self.width(), self.height()
        spots = [
            (0.16 * w, -0.32 * h, 18.0, 0.0),
            (0.88 * w, 0.18 * h, 21.0, 2.1),
            (0.30 * w, 0.78 * h, 25.0, 4.2),
        ]
        for pm, (x, y, period, phase) in zip(self._blob_cache, spots):
            k = 2 * math.pi * t / period + phase
            dx, dy = 60 * math.sin(k), 35 * math.sin(k * 0.8 + 1)
            p.drawPixmap(QPointF(x + dx, y + dy), pm)
        p.drawPixmap(0, 0, self._grid)
        p.drawPixmap(0, 0, self._beam)


_CARDS: "weakref.WeakSet[GlassCard]" = weakref.WeakSet()


class PointerLight(QObject):
    """Moves the soft highlight of every glass surface with the mouse."""

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._last = None
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        pos = QCursor.pos()
        if pos == self._last:
            return
        self._last = pos
        for card in list(_CARDS):
            try:
                if not _visible_on_screen(card):
                    continue
            except RuntimeError:  # the Qt object is already gone
                continue
            local = QPointF(card.mapFromGlobal(pos))
            near = card.rect().adjusted(-160, -160, 160, 160).contains(local.toPoint())
            new = local if near and card.window().isActiveWindow() else None
            if new != card.light:
                card.light = new
                card.update()


class GlassCard(QFrame):
    """Translucent white surface with a light edge; a glow follows the mouse."""

    RADIUS = 18.0

    def __init__(self, parent: QWidget | None = None, radius: float = RADIUS):
        super().__init__(parent)
        self.radius = radius
        self.light: QPointF | None = None
        self.setObjectName("glass")
        _CARDS.add(self)
        THEME.changed.connect(self.update)

    def paintEvent(self, event) -> None:
        pal = THEME.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, self.radius, self.radius)
        # soft drop shadow
        for i, a in ((4, 0.035), (2, 0.05)):
            sh = QPainterPath()
            sh.addRoundedRect(r.adjusted(-0.5, i, 0.5, i), self.radius, self.radius)
            p.fillPath(sh, hsl(pal.hue, 0.5, 0.4, a))
        p.fillPath(path, QColor(255, 255, 255, 140))
        if self.light is not None:
            glow = QRadialGradient(self.light, 300)
            glow.setColorAt(0.0, QColor(255, 255, 255, 120))
            glow.setColorAt(0.35, with_alpha(pal.a2, 0.10))
            glow.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.fillPath(path, glow)
        edge = QLinearGradient(r.topLeft(), r.bottomRight())
        edge.setColorAt(0.0, QColor(255, 255, 255, 255))
        edge.setColorAt(0.45, QColor(255, 255, 255, 90))
        edge.setColorAt(1.0, hsl(pal.hue, 0.6, 0.82, 0.7))
        p.setPen(QPen(QBrush(edge), 1))
        p.drawPath(path)
        if self.light is not None:
            ring = QRadialGradient(self.light, 220)
            ring.setColorAt(0.0, with_alpha(pal.a, 0.95))
            ring.setColorAt(1.0, with_alpha(pal.a, 0.0))
            p.setPen(QPen(QBrush(ring), 1.4))
            p.drawPath(path)


class GradientLabel(QWidget):
    """Large heading whose letters are filled with the theme gradient."""

    def __init__(self, text: str, px: int, weight: QFont.Weight = QFont.Weight.Bold, align=Qt.AlignmentFlag.AlignCenter):
        super().__init__()
        self._text = text
        self.font_ = display_font(px, weight)
        self.align = align
        self.alert = False
        THEME.changed.connect(self.update)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        fm = QFontMetricsF(self.font_)
        return QSize(math.ceil(fm.horizontalAdvance(self._text)) + 8, math.ceil(fm.height()) + 4)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event) -> None:
        pal = THEME.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = QFontMetricsF(self.font_)
        width = fm.horizontalAdvance(self._text)
        if self.align & Qt.AlignmentFlag.AlignHCenter:
            x = (self.width() - width) / 2
        else:
            x = 2
        y = (self.height() + fm.ascent() - fm.descent()) / 2
        path = QPainterPath()
        path.addText(QPointF(x, y), self.font_, self._text)
        g = QLinearGradient(x, 0, x + max(1.0, width), 0)
        if self.alert:
            g.setColorAt(0.1, QColor("#4A1F2A"))
            g.setColorAt(0.6, QColor("#E05555"))
            g.setColorAt(1.0, QColor("#F6A3A3"))
        else:
            g.setColorAt(0.1, pal.deep)
            g.setColorAt(0.6, pal.mid)
            g.setColorAt(1.0, pal.a2)
        p.fillPath(path, g)


class StatusPill(QWidget):
    """Small rounded tag: coloured dot + text (e.g. "守护中 · 教学/办公区")."""

    def __init__(self, text: str = ""):
        super().__init__()
        self._text = text
        self.font_ = text_font(12)
        self.dot = OK_COLOR
        THEME.changed.connect(self.update)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text
        self.updateGeometry()
        self.update()

    def set_dot(self, color: QColor) -> None:
        self.dot = QColor(color)
        self.update()

    def sizeHint(self) -> QSize:
        fm = QFontMetricsF(self.font_)
        return QSize(math.ceil(fm.horizontalAdvance(self._text)) + 42, 26)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event) -> None:
        pal = THEME.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(pal.tint2, 1))
        p.setBrush(QColor(255, 255, 255, 155))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        c = QPointF(13.5, r.center().y())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(with_alpha(self.dot, 0.22))
        p.drawEllipse(c, 9.5, 9.5)
        p.setBrush(self.dot)
        p.drawEllipse(c, 7, 7)
        p.setPen(pal.mute)
        p.setFont(self.font_)
        p.drawText(QRectF(28, 0, self.width() - 32, self.height()), Qt.AlignmentFlag.AlignVCenter, self._text)


class ProgressRing(QWidget):
    """The big centre ring: shows the time to the next check, spins while busy."""

    def __init__(self, size: int = 250, width: float = 10.0):
        super().__init__()
        self.setFixedSize(size, size)
        self.stroke = width
        self.fraction = 1.0
        self.busy = False
        self._spin = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(900)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._set_fraction)
        THEME.changed.connect(self.update)

    def _set_fraction(self, value) -> None:
        self.fraction = float(value)
        self.update()

    def set_fraction(self, value: float, animated: bool = False) -> None:
        value = max(0.0, min(1.0, value))
        if animated and self.isVisible():
            self._anim.stop()
            self._anim.setStartValue(self.fraction)
            self._anim.setEndValue(value)
            self._anim.start()
        else:
            self._anim.stop()
            self._set_fraction(value)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        if _visible_on_screen(self):
            self._spin = (self._spin + 7) % 360
            self.update()

    def paintEvent(self, event) -> None:
        pal = THEME.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self.stroke
        r = QRectF(self.rect()).adjusted(s / 2 + 1, s / 2 + 1, -s / 2 - 1, -s / 2 - 1)
        p.setPen(QPen(pal.tint, s))
        p.drawEllipse(r)
        if pal.down > 0.5:
            color_a, color_b = QColor("#F6A3A3"), BAD_COLOR
        else:
            color_a, color_b = pal.a2, pal.a
        if self.busy:
            start, span = 90 - self._spin, -100
        else:
            start, span = 90, -360 * self.fraction
        if abs(span) < 0.5:
            return
        g = QConicalGradient(r.center(), start)
        g.setColorAt(0.0, color_b)
        g.setColorAt(min(0.99, abs(span) / 360), color_a)
        g.setColorAt(1.0, color_b)
        pen = QPen(QBrush(g), s)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, int(start * 16), int(span * 16))


class Ticker(QWidget):
    """One line that slides up to show the next recent event."""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(22)
        self.items: list[tuple[str, QColor]] = []
        self._index = 0
        self._current: tuple[str, QColor] | None = None
        self._previous: tuple[str, QColor] | None = None
        self._progress = 1.0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(600)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._frame)
        self._timer = QTimer(self)
        self._timer.setInterval(3000)
        self._timer.timeout.connect(self.advance)
        self._timer.start()
        THEME.changed.connect(self.update)

    def _frame(self, value) -> None:
        self._progress = float(value)
        self.update()

    def set_items(self, items: list[tuple[str, QColor]]) -> None:
        self.items = items
        if self._current is None or self._current not in items:
            self._index = 0
            self.advance()

    def advance(self) -> None:
        if not self.items or not _visible_on_screen(self) and self._current is not None:
            return
        item = self.items[self._index % len(self.items)]
        self._index += 1
        if item == self._current and len(self.items) > 1:
            item = self.items[self._index % len(self.items)]
            self._index += 1
        self._previous, self._current = self._current, item
        self._progress = 0.0
        self._anim.start()

    def current_text(self) -> str:
        return self._current[0] if self._current else ""

    def paintEvent(self, event) -> None:
        pal = THEME.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(text_font(12.5))
        h = self.height()
        for item, offset, alpha in ((self._previous, -h * self._progress, 1 - self._progress), (self._current, h * (1 - self._progress), self._progress)):
            if not item or alpha <= 0:
                continue
            text, color = item
            p.setOpacity(alpha)
            fm = p.fontMetrics()
            total = 14 + fm.horizontalAdvance(text)
            x = (self.width() - total) / 2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(color)
            p.drawEllipse(QPointF(x + 4, h / 2 + offset), 3.5, 3.5)
            p.setPen(pal.mute)
            p.drawText(QRectF(x + 14, offset, self.width(), h), Qt.AlignmentFlag.AlignVCenter, text)


class Island(QWidget):
    """Dynamic-Island style status bar: a small dark capsule that springs open
    into a wider panel with a mini ring and the two main actions."""

    COMPACT = QSize(268, 38)
    EXPANDED = QSize(620, 64)

    def __init__(self, parent: QWidget, on_check, on_pause):
        super().__init__(parent)
        self.status = "未开始"
        self.title = "校园网守护"
        self.subtitle = ""
        self.countdown = ""
        self.fraction = 1.0
        self.progress = 0.0
        self.alert = False
        self._wave = 0.0
        self.anchor_x = 0
        self.setMouseTracking(True)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(650)
        self._anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutBack))
        self._anim.valueChanged.connect(self._frame)
        self._collapse_timer = QTimer(self)
        self._collapse_timer.setSingleShot(True)
        self._collapse_timer.timeout.connect(lambda: self._go(0.0))
        self._wave_timer = QTimer(self)
        self._wave_timer.setInterval(50)
        self._wave_timer.timeout.connect(self._tick)
        self._wave_timer.start()
        self.check_button = self._button("立即检测", on_check, primary=True)
        self.pause_button = self._button("暂停", on_pause)
        THEME.changed.connect(self._restyle)
        self._restyle()
        self._frame(0.0)

    def _button(self, text: str, callback, primary: bool = False) -> QPushButton:
        b = QPushButton(text, self)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setProperty("islandPrimary", primary)
        b.setFixedSize(88, 34)
        b.clicked.connect(callback)
        b.hide()
        return b

    def _restyle(self) -> None:
        pal = THEME.palette
        self.check_button.setStyleSheet(
            f"QPushButton{{border:0;border-radius:17px;color:white;font-weight:600;font-size:12.5px;"
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {pal.a2.name()},stop:1 {pal.a.name()});}}"
            f"QPushButton:hover{{background:{pal.a2.name()};}}"
        )
        self.pause_button.setStyleSheet(
            "QPushButton{border:0;border-radius:17px;color:white;font-size:12.5px;background:rgba(255,255,255,26);}"
            "QPushButton:hover{background:rgba(255,255,255,50);}"
        )
        self.update()

    # -- geometry / animation ---------------------------------------------
    def place(self, center_x: int, top: int) -> None:
        self.anchor_x, self.anchor_top = center_x, top
        self._frame(self.progress)

    def _frame(self, value) -> None:
        self.progress = float(value)
        k = max(0.0, self.progress)
        w = self.COMPACT.width() + (self.EXPANDED.width() - self.COMPACT.width()) * k
        h = self.COMPACT.height() + (self.EXPANDED.height() - self.COMPACT.height()) * min(k, 1.06)
        self.setGeometry(round(self.anchor_x - w / 2), getattr(self, "anchor_top", 8), round(w), round(h))
        show = self.progress > 0.75
        for i, b in enumerate((self.pause_button, self.check_button)):
            b.setVisible(show)
            b.move(self.width() - 14 - (i + 1) * 88 - i * 6, (self.height() - 34) // 2)
        self.update()

    def _go(self, target: float) -> None:
        self._anim.stop()
        self._anim.setEasingCurve(QEasingCurve(QEasingCurve.Type.OutBack if target else QEasingCurve.Type.OutCubic))
        self._anim.setStartValue(self.progress)
        self._anim.setEndValue(target)
        self._anim.start()

    def expand(self) -> None:
        self._collapse_timer.stop()
        if self.progress < 1.0:
            self._go(1.0)

    def collapse(self, delay_ms: int = 0) -> None:
        self._collapse_timer.start(max(0, delay_ms))

    def pop(self, ms: int = 3200) -> None:
        if not _visible_on_screen(self):
            return
        self.expand()
        if not self.underMouse():
            self.collapse(ms)

    def enterEvent(self, event) -> None:
        self.expand()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.collapse(500)
        super().leaveEvent(event)

    def _tick(self) -> None:
        if _visible_on_screen(self) and self.progress < 0.6:
            self._wave += 0.05 * (2.2 if self.alert else 1.0)
            self.update()

    def set_state(self, *, status: str, title: str, subtitle: str, alert: bool) -> None:
        self.status, self.title, self.subtitle, self.alert = status, title, subtitle, alert
        self.update()

    def set_countdown(self, text: str, fraction: float) -> None:
        self.countdown, self.fraction = text, max(0.0, min(1.0, fraction))
        self.update()

    # -- painting -----------------------------------------------------------
    def paintEvent(self, event) -> None:
        pal = THEME.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pal.island)
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        status_color = BAD_COLOR if self.alert else OK_COLOR
        compact = max(0.0, 1.0 - self.progress * 1.8)
        full = max(0.0, min(1.0, (self.progress - 0.45) / 0.55))
        if compact > 0:
            p.setOpacity(compact)
            cy = r.height() / 2
            p.setBrush(with_alpha(status_color, 0.35))
            p.drawEllipse(QPointF(20, cy), 6.5, 6.5)
            p.setBrush(status_color)
            p.drawEllipse(QPointF(20, cy), 4, 4)
            p.setPen(QColor("white"))
            p.setFont(text_font(13, QFont.Weight.DemiBold))
            p.drawText(QRectF(33, 0, r.width() - 120, r.height()), Qt.AlignmentFlag.AlignVCenter, self.status)
            p.setFont(display_font(13, QFont.Weight.DemiBold))
            p.drawText(QRectF(0, 0, r.width() - 44, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self.countdown)
            wave = status_color if self.alert else pal.a2
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(wave)
            for i in range(4):
                hgt = 4 + 10 * (0.5 + 0.5 * math.sin(self._wave * 6 - i * 0.9))
                p.drawRoundedRect(QRectF(r.width() - 36 + i * 5, cy - hgt / 2, 3, hgt), 1.5, 1.5)
        if full > 0:
            p.setOpacity(full)
            cy = r.height() / 2
            ring = QRectF(14, cy - 21, 42, 42)
            p.setPen(QPen(QColor(255, 255, 255, 30), 4.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(ring)
            pen = QPen(status_color if self.alert else pal.a2, 4.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawArc(ring, 90 * 16, int(-360 * 16 * self.fraction))
            p.setPen(QColor("white"))
            p.setFont(display_font(11, QFont.Weight.DemiBold))
            p.drawText(ring, Qt.AlignmentFlag.AlignCenter, self.countdown)
            text_w = r.width() - 70 - 200
            p.setFont(text_font(15, QFont.Weight.DemiBold))
            p.drawText(QRectF(70, cy - 21, text_w, 22), Qt.AlignmentFlag.AlignVCenter, self.title)
            p.setPen(hsl(pal.hue, 0.3, 0.72))
            p.setFont(text_font(12))
            fm = p.fontMetrics()
            p.drawText(QRectF(70, cy + 1, text_w, 20), Qt.AlignmentFlag.AlignVCenter, fm.elidedText(self.subtitle, Qt.TextElideMode.ElideRight, int(text_w)))


class StatTile(GlassCard):
    """One figure on the home page: icon chip, big value, caption."""

    def __init__(self, glyph: str, value: str, caption: str):
        super().__init__()
        self.glyph, self.value, self.caption = glyph, value, caption
        self.setFixedSize(196, 70)

    def set(self, value: str, caption: str | None = None) -> None:
        self.value = value
        if caption is not None:
            self.caption = caption
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        pal = THEME.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        chip = QRectF(16, (self.height() - 36) / 2, 36, 36)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pal.tint)
        p.drawRoundedRect(chip, 11, 11)
        p.setPen(QPen(pal.a, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        c = chip.center()
        if self.glyph == "rate":
            p.drawEllipse(c, 9, 9)
            p.setBrush(pal.a)
            p.drawPie(QRectF(c.x() - 9, c.y() - 9, 18, 18), 90 * 16, -300 * 16)
        elif self.glyph == "drop":
            path = QPainterPath(QPointF(c.x() + 2, c.y() - 9))
            path.lineTo(c.x() - 5, c.y() + 1)
            path.lineTo(c.x() + 1, c.y() + 1)
            path.lineTo(c.x() - 2, c.y() + 9)
            path.lineTo(c.x() + 5, c.y() - 1)
            path.lineTo(c.x() - 1, c.y() - 1)
            path.closeSubpath()
            p.drawPath(path)
        else:
            p.drawRoundedRect(QRectF(c.x() - 9, c.y() - 7, 18, 12), 2.5, 2.5)
            p.drawLine(QPointF(c.x() - 4, c.y() + 9), QPointF(c.x() + 4, c.y() + 9))
        p.setPen(pal.deep)
        p.setFont(display_font(20, QFont.Weight.DemiBold))
        p.drawText(QRectF(64, 12, self.width() - 70, 26), Qt.AlignmentFlag.AlignVCenter, self.value)
        p.setPen(pal.mute)
        p.setFont(text_font(12))
        p.drawText(QRectF(64, 38, self.width() - 70, 20), Qt.AlignmentFlag.AlignVCenter, self.caption)


class NavItem(QPushButton):
    """Sidebar entry with a key hint on the right."""

    def __init__(self, text: str, key: str, checkable: bool = True):
        super().__init__(text)
        self.key = key
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(34)
        self._hover = False
        THEME.changed.connect(self.update)

    def enterEvent(self, event) -> None:
        self._hover = True
        self.update()

    def leaveEvent(self, event) -> None:
        self._hover = False
        self.update()

    def paintEvent(self, event) -> None:
        pal = THEME.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self.isChecked():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 220))
            p.drawRoundedRect(r, 9, 9)
        elif self._hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(pal.tint)
            p.drawRoundedRect(r, 9, 9)
        p.setPen(pal.deep if self.isChecked() or self._hover else pal.mute)
        p.setFont(text_font(13, QFont.Weight.DemiBold if self.isChecked() else QFont.Weight.Normal))
        p.drawText(r.adjusted(10, 0, -40, 0), Qt.AlignmentFlag.AlignVCenter, self.text())
        if self.key:
            p.setFont(text_font(10.5))
            box = QRectF(r.right() - 26, r.center().y() - 9, 18, 18)
            p.setPen(QPen(pal.tint2, 1))
            p.setBrush(QColor(255, 255, 255, 150))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(pal.faint)
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, self.key)


class ColorChip(QPushButton):
    """A theme colour swatch; shows a tick and a ring when selected."""

    def __init__(self, name: str, hue: int):
        super().__init__()
        self.name, self.hue = name, hue
        self.selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(86, 76)
        THEME.changed.connect(self.update)

    def paintEvent(self, event) -> None:
        pal = THEME.palette
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = QRectF(4, 4, self.width() - 8, 48)
        if self.selected:
            p.setPen(QPen(QColor("white"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(box.adjusted(-1, -1, 1, 1), 15, 15)
            p.setPen(QPen(pal.a, 2))
            p.drawRoundedRect(box.adjusted(-3, -3, 3, 3), 17, 17)
        g = QLinearGradient(box.topLeft(), box.bottomRight())
        g.setColorAt(0, hsl(self.hue, 0.86, 0.73))
        g.setColorAt(1, hsl(self.hue, 0.78, 0.57))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(box, 14, 14)
        if self.selected:
            p.setPen(QPen(QColor("white"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            c = box.center()
            p.drawPolyline([QPointF(c.x() - 7, c.y()), QPointF(c.x() - 2, c.y() + 5), QPointF(c.x() + 8, c.y() - 6)])
        p.setPen(pal.deep if self.selected else pal.mute)
        p.setFont(text_font(12, QFont.Weight.DemiBold if self.selected else QFont.Weight.Normal))
        p.drawText(QRectF(0, 54, self.width(), 20), Qt.AlignmentFlag.AlignCenter, self.name)


class HueSlider(QWidget):
    """Rainbow track for picking any theme hue."""

    hueChanged = Signal(int)

    def __init__(self):
        super().__init__()
        self.hue = DEFAULT_HUE
        self.setFixedHeight(26)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        THEME.changed.connect(self.update)

    def set_hue(self, hue: int) -> None:
        self.hue = int(hue) % 360
        self.update()

    def _track(self) -> QRectF:
        return QRectF(11, (self.height() - 12) / 2, self.width() - 22, 12)

    def _pick(self, x: float) -> None:
        t = self._track()
        hue = round(max(0.0, min(1.0, (x - t.left()) / t.width())) * 359)
        if hue != self.hue:
            self.hue = hue
            self.update()
            self.hueChanged.emit(hue)

    def mousePressEvent(self, event) -> None:
        self._pick(event.position().x())

    def mouseMoveEvent(self, event) -> None:
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._pick(event.position().x())

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self._track()
        g = QLinearGradient(t.left(), 0, t.right(), 0)
        for i in range(7):
            g.setColorAt(i / 6, hsl(i * 60, 0.78, 0.62))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(g)
        p.drawRoundedRect(t, 6, 6)
        x = t.left() + t.width() * self.hue / 359
        c = QPointF(x, t.center().y())
        p.setBrush(QColor(0, 0, 0, 40))
        p.drawEllipse(c + QPointF(0, 1.5), 11.5, 11.5)
        p.setBrush(QColor("white"))
        p.drawEllipse(c, 11, 11)
        p.setBrush(hsl(self.hue, 0.78, 0.57))
        p.drawEllipse(c, 8, 8)
