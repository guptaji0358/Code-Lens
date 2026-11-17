"""
tooltips.py - one app-wide, theme-aware tooltip component for CodeLens.

Replaces the native Qt tooltip for every widget that has tooltip text.
A single TooltipManager is installed on the QApplication; it intercepts
QEvent.ToolTip (hover), keyboard FocusIn and dismissal events for all
widgets, so no per-button implementation is needed.

Per-widget metadata is optional and stored as a dict under the
"cl_tip" dynamic property (see set_tooltip_info):
    title        short action name (defaults to the first toolTip() line)
    description  one short sentence (defaults to the remaining lines)
    shortcut     a QShortcut object, or a callable returning one. The chip
                 is shown only while that QShortcut exists, is enabled and
                 has a key, so a shortcut that is not functional never
                 appears in a tooltip.
    icon         optional QIcon shown beside the title.
Widgets with only a plain toolTip() string get that text as the title.
"""

from PySide6.QtCore import (
    Qt, QObject, QEvent, QPoint, QRect, QRectF, QSize, QTimer, QPropertyAnimation,
    QEasingCurve,
)
from PySide6.QtGui import QPainter, QColor, QPen, QFont, QFontMetrics, QKeySequence, QGuiApplication
from PySide6.QtWidgets import QWidget, QApplication, QAbstractButton, QLineEdit, QPlainTextEdit

from theme import THEMES, RADIUS_CONTROL, RADIUS_SMALL

_PROP = "cl_tip"
_MAX_TEXT_W = 240       # px - keeps the bubble compact
_PAD_X, _PAD_Y = 12, 9  # inner padding
_SHADOW = 10            # px of transparent margin around the bubble for the shadow
_RADIUS = RADIUS_CONTROL  # same corner language as menus and inputs
_SHOW_DELAY_MS = 350    # keyboard focus tips wait a moment, like hover
_FADE_MS = 140


def set_tooltip_info(widget, title=None, description=None, shortcut=None, icon=None):
    """Attach structured tooltip content to *widget*. The plain toolTip()
    text should still be set too (it remains the accessible name source)."""
    widget.setProperty(_PROP, {
        "title": title, "description": description,
        "shortcut": shortcut, "icon": icon,
    })


def _info_for(widget):
    info = widget.property(_PROP) or {}
    text = widget.toolTip() or ""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    title = info.get("title") or (lines[0] if lines else "")
    desc = info.get("description")
    if desc is None and not info.get("title") and len(lines) > 1:
        desc = "\n".join(lines[1:])
    return title, desc or "", info.get("shortcut"), info.get("icon")


def _alive(ref):
    """Return the widget behind *ref* if it still exists, else None. A plain
    reference can outlive its C++ object, so a deleted widget reads as None."""
    if ref is None:
        return None
    try:
        ref.objectName()
        return ref
    except RuntimeError:
        return None


def _live_shortcut_text(spec):
    """Resolve a shortcut spec to its display text, or '' when it is not
    registered, disabled or empty (so the chip is never shown falsely)."""
    if spec is None:
        return ""
    sc = spec() if callable(spec) and not hasattr(spec, "key") else spec
    if sc is None or not sc.isEnabled():
        return ""
    seq = sc.key()
    if seq.isEmpty():
        return ""
    return seq.toString(QKeySequence.NativeText)


class CodeLensTooltip(QWidget):
    """Frameless, translucent, non-activating bubble. Paints itself from the
    active theme tokens, so it always matches light or dark mode."""

    def __init__(self):
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._theme = THEMES["dark"]
        self._title = ""
        self._desc = ""
        self._shortcut = ""
        self._icon = None
        self._title_font = QFont(self.font())
        self._title_font.setBold(True)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(_FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)

    def set_content(self, theme, title, desc, shortcut, icon):
        self._theme = theme
        self._title, self._desc, self._shortcut, self._icon = title, desc, shortcut, icon
        self._resize_to_content()

    def _resize_to_content(self):
        fm_t = QFontMetrics(self._title_font)
        fm_d = QFontMetrics(self.font())
        icon_w = 18 + 8 if self._icon is not None else 0
        chip_w = (QFontMetrics(self.font()).horizontalAdvance(self._shortcut) + 12 + 8) if self._shortcut else 0
        title_w = min(fm_t.horizontalAdvance(self._title), _MAX_TEXT_W - icon_w - chip_w) if self._title else 0
        inner_w = icon_w + title_w + chip_w
        if self._desc:
            br = fm_d.boundingRect(QRect(0, 0, _MAX_TEXT_W, 10000),
                                   int(Qt.TextWordWrap), self._desc)
            inner_w = max(inner_w, min(br.width(), _MAX_TEXT_W))
            desc_h = br.height() + 4
        else:
            desc_h = 0
        title_h = max(fm_t.height(), 18 if self._icon is not None else 0)
        w = max(inner_w, 1) + 2 * _PAD_X + 2 * _SHADOW
        h = title_h + desc_h + 2 * _PAD_Y + 2 * _SHADOW
        self.setFixedSize(QSize(int(w), int(h)))

    def paintEvent(self, _event):
        t = self._theme
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        body = QRectF(self.rect()).adjusted(_SHADOW, _SHADOW, -_SHADOW, -_SHADOW)
        # Subtle shadow: a few faint rounded rects stacked outward (cheap, no effect object).
        shadow = QColor(0, 0, 0)
        for i in range(_SHADOW, 0, -2):
            shadow.setAlpha(5 if t is THEMES["dark"] else 3)
            p.setPen(Qt.NoPen)
            p.setBrush(shadow)
            p.drawRoundedRect(body.adjusted(-i, -i + 2, i, i + 2), _RADIUS + i, _RADIUS + i)
        # Bubble body and border.
        p.setPen(QPen(QColor(t["border_soft"]), 1))  # faint edge, not an outline
        p.setBrush(QColor(t["bg_panel"]))
        p.drawRoundedRect(body, _RADIUS, _RADIUS)

        x = body.left() + _PAD_X
        y = body.top() + _PAD_Y
        fm_t = QFontMetrics(self._title_font)
        if self._icon is not None:
            self._icon.paint(p, int(x), int(y + (fm_t.height() - 18) / 2), 18, 18)
            x += 26
        if self._title:
            p.setFont(self._title_font)
            p.setPen(QColor(t["fg"]))
            p.drawText(QRectF(x, y, _MAX_TEXT_W, fm_t.height()), int(Qt.AlignLeft | Qt.AlignVCenter), self._title)
        if self._shortcut:
            chip_fm = QFontMetrics(self.font())
            cw = chip_fm.horizontalAdvance(self._shortcut) + 12
            chip = QRectF(body.right() - _PAD_X - cw, y + (fm_t.height() - 18) / 2, cw, 18)
            p.setPen(QPen(QColor(t["border_soft"]), 1))
            p.setBrush(QColor(t["bg_card"]))
            p.drawRoundedRect(chip, RADIUS_SMALL, RADIUS_SMALL)
            p.setPen(QColor(t["fg_dim"]))
            p.setFont(self.font())
            p.drawText(chip, int(Qt.AlignCenter), self._shortcut)
        if self._desc:
            p.setFont(self.font())
            p.setPen(QColor(t["fg_dim"]))
            dy = y + fm_t.height() + 3
            p.drawText(QRectF(body.left() + _PAD_X, dy, body.width() - 2 * _PAD_X, body.bottom() - dy - _PAD_Y),
                       int(Qt.AlignLeft | Qt.TextWordWrap), self._desc)
        p.end()

    def fade_in(self):
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()


class TooltipManager(QObject):
    """App-wide controller. Install once with TooltipManager.install(); it
    filters every widget's events, so there is nothing to attach per button."""

    _instance = None

    def __init__(self, app, theme_provider):
        super().__init__(app)
        self._theme_provider = theme_provider  # callable -> theme dict
        self._bubble = CodeLensTooltip()
        self._target = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show_pending)
        self._pending = None
        app.installEventFilter(self)

    @classmethod
    def install(cls, app, theme_provider):
        """Create the app-wide manager once. Every call rebinds the theme
        provider, so a newly built window (or a test's window) is the one
        whose theme is read - never a stale earlier window's."""
        if cls._instance is None:
            cls._instance = cls(app, theme_provider)
        cls._instance._theme_provider = theme_provider
        return cls._instance

    @classmethod
    def instance(cls):
        return cls._instance

    # ----- public -----
    def refresh_theme(self):
        """Call on theme change: any visible bubble is dismissed so the next
        one is painted with the new tokens."""
        self.hide_tip()

    def current_target(self):
        return _alive(self._target)

    def is_visible(self):
        return self._bubble.isVisible()

    def show_for(self, widget):
        """Show the tip for *widget* now (used by hover, focus and tests)."""
        title, desc, shortcut, icon = _info_for(widget)
        if not title and not desc:
            self.hide_tip()
            return False
        t = self._theme_provider()
        shortcut_text = _live_shortcut_text((widget.property(_PROP) or {}).get("shortcut"))
        self._bubble.set_content(t, title, desc, shortcut_text, icon)
        self._target = widget
        self._place(widget)
        was_visible = self._bubble.isVisible()
        self._bubble.show()
        if not was_visible:
            self._bubble.fade_in()
        return True

    def hide_tip(self):
        self._timer.stop()
        self._pending = None
        if self._bubble.isVisible():
            self._bubble.hide()
        self._target = None

    # ----- placement -----
    def _place(self, widget):
        """Below the widget, flipped above when there's no room; clamped to
        the screen so it never leaves the visible area. Never overlaps the
        widget itself (it sits in the gap beyond the widget's rect)."""
        gap = 6
        top_left = widget.mapToGlobal(QPoint(0, 0))
        wr = QRect(top_left, widget.size())
        screen = QGuiApplication.screenAt(wr.center()) or widget.screen()
        avail = screen.availableGeometry()
        bs = self._bubble.size()
        x = wr.center().x() - bs.width() // 2
        y = wr.bottom() + 1 - _SHADOW + gap
        if y + bs.height() > avail.bottom():
            y = wr.top() - bs.height() + _SHADOW - gap
        x = max(avail.left(), min(x, avail.right() - bs.width() + 1))
        y = max(avail.top(), min(y, avail.bottom() - bs.height() + 1))
        self._bubble.move(x, y)

    # ----- event filter -----
    def eventFilter(self, obj, event):
        if not isinstance(obj, QWidget) or obj is self._bubble:
            return False
        et = event.type()
        if et == QEvent.ToolTip:
            if self._has_tip(obj):
                self._pending = None
                self._timer.stop()
                self.show_for(obj)
                return True  # consume: no native tooltip
            return False
        if et == QEvent.FocusIn:
            if event.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason) \
                    and self._has_tip(obj):
                self._pending = obj
                self._timer.start(_SHOW_DELAY_MS)
            return False
        if et in (QEvent.FocusOut, QEvent.Leave, QEvent.Hide, QEvent.Wheel,
                  QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.WindowDeactivate):
            if _alive(self._target) is obj or et in (
                    QEvent.MouseButtonPress, QEvent.KeyPress, QEvent.Wheel, QEvent.WindowDeactivate):
                self.hide_tip()
        return False

    def _has_tip(self, widget):
        if isinstance(widget, (QLineEdit, QPlainTextEdit)) and not widget.toolTip():
            return False
        return bool((widget.toolTip() or "") or widget.property(_PROP))

    def _show_pending(self):
        target = _alive(self._pending)
        self._pending = None
        if target is not None and target.isVisible() and target.hasFocus():
            self.show_for(target)
