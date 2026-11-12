"""Animated widgets and helpers for the Glass interface.

Every animation here is short and stops on its own, except the status orb,
which only ticks while it is actually visible on screen so the app costs no
CPU while it sits in the tray.
"""

from __future__ import annotations

import math
import sys
import time

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
)
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QGraphicsOpacityEffect,
    QWidget,
)

ONLINE_COLOR = QColor("#299259")
OFFLINE_COLOR = QColor("#C0473A")
IDLE_COLOR = QColor("#7D8A9B")
BUSY_STATES = {"checking", "reconnecting", "restarting"}


def blend(color: QColor, other: QColor, amount: float) -> QColor:
    """Mix ``amount`` (0..1) of ``other`` into ``color``."""
    amount = max(0.0, min(1.0, amount))
    return QColor(
        round(color.red() + (other.red() - color.red()) * amount),
        round(color.green() + (other.green() - color.green()) * amount),
        round(color.blue() + (other.blue() - color.blue()) * amount),
        round(color.alpha() + (other.alpha() - color.alpha()) * amount),
    )


def surface_top_color(tint: QColor) -> QColor:
    """Colour of the top edge of the window background (and the title bar)."""
    return blend(tint, QColor("white"), 0.42)


def set_title_bar_color(widget: QWidget, color: QColor) -> bool:
    """Tint the native Windows 11 title bar so it matches the theme."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        hwnd = int(widget.winId())
        caption = ctypes.c_uint(color.red() | color.green() << 8 | color.blue() << 16)
        text = ctypes.c_uint(0x003F2B20)  # #202B3F as COLORREF (0x00BBGGRR)
        dwm = ctypes.windll.dwmapi
        ok = dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), 4) == 0
        dwm.DwmSetWindowAttribute(hwnd, 36, ctypes.byref(text), 4)
        return ok
    except (AttributeError, OSError, ValueError):
        return False


class StatusOrb(QWidget):
    """The hero icon: breathes when online, spins while working, pulses red
    when offline, and draws the countdown to the next check as a ring."""

    FRAME_MS = 33

    def __init__(self, size: int = 104):
        super().__init__()
        self.setFixedSize(size, size)
        self.state = "stopped"
        self.accent = QColor("#2F6FDB")
        self._phase = 0.0
        self._started = time.monotonic()
        self._countdown: tuple[float, float] | None = None
        self._flash = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)

    # -- state ---------------------------------------------------------
    def set_state(self, state: str) -> None:
        if state == "waiting" or state == self.state:
            return
        if state in ("online", "offline"):
            self._flash = 1.0  # brief ripple when the result arrives
        self.state = state
        if state in BUSY_STATES or state == "stopped":
            self._countdown = None
        self.update()

    def set_countdown(self, start: float | None, end: float | None) -> None:
        self._countdown = (start, end) if start and end and end > start else None
        self.update()

    def set_accent(self, color: QColor) -> None:
        self.accent = QColor(color)
        self.update()

    def color(self) -> QColor:
        if self.state == "online":
            return ONLINE_COLOR
        if self.state == "offline":
            return OFFLINE_COLOR
        if self.state in BUSY_STATES:
            return self.accent
        return IDLE_COLOR

    # -- animation loop --------------------------------------------------
    def showEvent(self, event) -> None:
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def _tick(self) -> None:
        if not self.isVisible():
            self._timer.stop()
            return
        self._phase = time.monotonic() - self._started
        self._flash = max(0.0, self._flash - self.FRAME_MS / 700)
        self.update()

    # -- painting --------------------------------------------------------
    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        center = QPointF(self.width() / 2, self.height() / 2)
        color = self.color()
        card = side * 0.70
        card_rect = QRectF(center.x() - card / 2, center.y() - card / 2, card, card)

        # Halo behind the card.
        if self.state == "online":
            wave = (math.sin(self._phase * 2.2) + 1) / 2
            self._halo(p, center, card * (0.56 + 0.05 * wave), color, 40 + 40 * wave)
        elif self.state == "offline":
            wave = (math.sin(self._phase * 5.0) + 1) / 2
            self._halo(p, center, card * (0.56 + 0.07 * wave), color, 50 + 70 * wave)
        if self._flash > 0:
            radius = card * (0.55 + (1 - self._flash) * 0.30)
            ring = QColor(color)
            ring.setAlpha(round(160 * self._flash))
            p.setPen(QPen(ring, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(center, radius, radius)

        # Glass card.
        p.setPen(QPen(QColor(255, 255, 255, 220), 1))
        p.setBrush(QColor(255, 255, 255, 150))
        p.drawRoundedRect(card_rect, card * 0.23, card * 0.23)

        # Network glyph inside the card.
        self._network_glyph(p, card_rect, color)

        # Outer ring: spinner while busy, countdown otherwise.
        ring_rect = QRectF(3, 3, side - 6, side - 6)
        track = QColor(color)
        track.setAlpha(38)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if self.state in BUSY_STATES:
            p.setPen(QPen(track, 3))
            p.drawEllipse(ring_rect)
            arc = QColor(color)
            p.setPen(QPen(arc, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            start = -int((self._phase * 300) % 360 * 16)
            span = int((70 + 50 * math.sin(self._phase * 3)) * 16)
            p.drawArc(ring_rect, start, span)
        elif self._countdown:
            begin, end = self._countdown
            progress = max(0.0, min(1.0, (time.time() - begin) / (end - begin)))
            p.setPen(QPen(track, 3))
            p.drawEllipse(ring_rect)
            arc = QColor(color)
            arc.setAlpha(200)
            p.setPen(QPen(arc, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawArc(ring_rect, 90 * 16, -int(360 * 16 * progress))

    def _halo(self, p: QPainter, center: QPointF, radius: float, color: QColor, alpha: float) -> None:
        from PySide6.QtGui import QRadialGradient

        glow = QRadialGradient(center, radius * 1.35)
        inner = QColor(color)
        inner.setAlpha(round(alpha))
        outer = QColor(color)
        outer.setAlpha(0)
        glow.setColorAt(0.55, inner)
        glow.setColorAt(1.0, outer)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(glow)
        p.drawEllipse(center, radius * 1.35, radius * 1.35)

    def _network_glyph(self, p: QPainter, rect: QRectF, color: QColor) -> None:
        p.save()
        p.translate(rect.topLeft())
        p.scale(rect.width() / 80, rect.height() / 80)
        p.setPen(QPen(color, 2.7, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(33, 17, 14, 14), 3, 3)
        if self.state != "offline":
            p.drawLine(40, 32, 40, 43)
        p.drawLine(21, 43, 59, 43)
        p.drawLine(21, 43, 21, 53)
        p.drawLine(59, 43, 59, 53)
        p.drawRoundedRect(QRectF(14, 53, 14, 14), 3, 3)
        p.drawRoundedRect(QRectF(52, 53, 14, 14), 3, 3)
        if self.state == "offline":
            p.setPen(QPen(OFFLINE_COLOR, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(QPointF(36.5, 33.5), QPointF(43.5, 40.5))
            p.drawLine(QPointF(43.5, 33.5), QPointF(36.5, 40.5))
        p.restore()


class AnimatedSwitch(QCheckBox):
    """iOS-style toggle whose knob slides instead of jumping."""

    TRACK = QSize(38, 22)

    def __init__(self, text: str, checked: bool):
        super().__init__(text)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(28)
        self.accent = QColor("#2F6FDB")
        self._position = 1.0 if checked else 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(180)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(self._set_position)
        self.toggled.connect(self._animate)

    def set_accent(self, color: QColor) -> None:
        self.accent = QColor(color)
        self.update()

    def _set_position(self, value) -> None:
        self._position = float(value)
        self.update()

    def _animate(self, checked: bool) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(1.0 if checked else 0.0)
        if self.isVisible():
            self._animation.start()
        else:
            self._set_position(1.0 if checked else 0.0)

    def sizeHint(self) -> QSize:
        metrics = QFontMetrics(self.font())
        return QSize(
            self.TRACK.width() + 10 + metrics.horizontalAdvance(self.text()) + 4,
            max(28, metrics.height() + 6),
        )

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        track = QRectF(1, (self.height() - self.TRACK.height()) / 2, self.TRACK.width(), self.TRACK.height())
        off = QColor("#A5B2C8")
        fill = blend(off, self.accent, self._position)
        if not self.isEnabled():
            fill.setAlpha(120)
        p.setPen(QPen(QColor(255, 255, 255, 160), 1))
        p.setBrush(fill)
        p.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        knob = track.height() - 6
        x = track.left() + 3 + (track.width() - knob - 6) * self._position
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 30))
        p.drawEllipse(QRectF(x, track.top() + 4, knob, knob))
        p.setBrush(QColor("white"))
        p.drawEllipse(QRectF(x, track.top() + 3, knob, knob))
        p.setPen(self.palette().color(self.foregroundRole()))
        text_rect = QRect(int(track.right()) + 10, 0, self.width() - int(track.right()) - 10, self.height())
        p.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())


class SlidingNav(QObject):
    """A pill that glides under the checked tab of a segmented control."""

    def __init__(self, container: QFrame, buttons: list[QAbstractButton]):
        super().__init__(container)
        self.container = container
        self.buttons = buttons
        self.pill = QFrame(container)
        self.pill.setObjectName("navPill")
        self.pill.lower()
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(260)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(lambda rect: self.pill.setGeometry(rect))
        container.installEventFilter(self)
        for item in buttons:
            item.toggled.connect(lambda checked: checked and self.move(animated=True))

    def eventFilter(self, watched, event) -> bool:
        if watched is self.container and event.type() in (QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest):
            QTimer.singleShot(0, lambda: self.move(animated=False))
        return False

    def target(self) -> QRect | None:
        for item in self.buttons:
            if item.isChecked():
                return item.geometry()
        return None

    def move(self, animated: bool) -> None:
        target = self.target()
        if target is None:
            return
        self.pill.show()
        self.pill.lower()
        if animated and self.container.isVisible() and self.pill.geometry().isValid():
            self._animation.stop()
            self._animation.setStartValue(self.pill.geometry())
            self._animation.setEndValue(target)
            self._animation.start()
        else:
            self._animation.stop()
            self.pill.setGeometry(target)


def fade_in(widget: QWidget, duration: int = 220) -> None:
    """Fade a page in, then drop the effect so it costs nothing afterwards."""
    if not widget.isVisible():
        return
    previous = getattr(widget, "_fade_animation", None)
    if previous is not None:
        previous.stop()
        previous.deleteLater()
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)
    animation = QVariantAnimation(widget)
    animation.setDuration(duration)
    animation.setStartValue(0.0)
    animation.setEndValue(1.0)
    animation.setEasingCurve(QEasingCurve.Type.OutCubic)

    def frame(value) -> None:
        if widget.graphicsEffect() is effect:
            effect.setOpacity(float(value))

    def done() -> None:
        if widget.graphicsEffect() is effect:
            widget.setGraphicsEffect(None)
        if getattr(widget, "_fade_animation", None) is animation:
            widget._fade_animation = None
        animation.deleteLater()

    animation.valueChanged.connect(frame)
    animation.finished.connect(done)
    widget._fade_animation = animation
    animation.start()


class HoverLift(QObject):
    """Soft shadow that grows on hover and settles when pressed."""

    def __init__(self, widget: QWidget, color: QColor | None = None):
        super().__init__(widget)
        self.widget = widget
        self.base = QColor(color or QColor(60, 80, 120))
        self.effect = QGraphicsDropShadowEffect(widget)
        self.effect.setOffset(0, 2)
        self._level = 0.0
        self._apply(0.0)
        widget.setGraphicsEffect(self.effect)
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(160)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.valueChanged.connect(lambda value: self._apply(float(value)))
        widget.installEventFilter(self)

    def set_color(self, color: QColor) -> None:
        self.base = QColor(color)
        self._apply(self._level)

    def _apply(self, level: float) -> None:
        self._level = level
        shade = QColor(self.base)
        shade.setAlpha(round(15 + 60 * level))
        self.effect.setColor(shade)
        self.effect.setBlurRadius(6 + 16 * level)
        self.effect.setOffset(0, 1 + 3 * level)

    def _go(self, level: float) -> None:
        self._animation.stop()
        self._animation.setStartValue(self._level)
        self._animation.setEndValue(level)
        self._animation.start()

    def eventFilter(self, watched, event) -> bool:
        kind = event.type()
        if not self.widget.isEnabled():
            if self._level:
                self._go(0.0)
            return False
        if kind == QEvent.Type.Enter:
            self._go(1.0)
        elif kind == QEvent.Type.Leave:
            self._go(0.0)
        elif kind == QEvent.Type.MouseButtonPress:
            self._go(0.35)
        elif kind == QEvent.Type.MouseButtonRelease:
            self._go(1.0 if self.widget.underMouse() else 0.0)
        return False


class ColorTween(QObject):
    """Animate between two colours and report each frame."""

    def __init__(self, parent: QObject, on_frame, duration: int = 420):
        super().__init__(parent)
        self.on_frame = on_frame
        self.current = QColor()
        self._animation = QVariantAnimation(self)
        self._animation.setDuration(duration)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.valueChanged.connect(self._frame)

    def _frame(self, value) -> None:
        self.current = QColor(value)
        self.on_frame(self.current)

    def go(self, color: QColor, animated: bool = True) -> None:
        self._animation.stop()
        if not animated or not self.current.isValid():
            self._frame(color)
            return
        self._animation.setStartValue(self.current)
        self._animation.setEndValue(QColor(color))
        self._animation.start()
