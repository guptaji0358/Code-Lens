"""
widgets.py - small, reusable custom widgets shared across CodeLens's
dialogs and main window (glass backdrop, animated switch/checkbox,
color swatch, hover-aware icon buttons, group-by-folder tree row).
"""

from PySide6.QtCore import (
    Qt, QPropertyAnimation, QEasingCurve, QRect, QRectF, QPoint, QPointF, QSize, Property,
    QTimer, Signal,
)
from PySide6.QtGui import QIcon, QPainter, QColor, QPen, QLinearGradient, QFontMetrics
from PySide6.QtWidgets import (
    QWidget, QLabel, QToolButton, QRadioButton, QAbstractButton, QGraphicsDropShadowEffect,
    QFrame, QVBoxLayout, QProgressBar, QPushButton,
)

from theme import THEMES, stable_icon, contrast_fg


class GlassBackdrop(QWidget):
    """
    Paints a soft gradient plus a couple of large, blurred-looking color
    blobs so the translucent glass panels above have something colorful
    to show through - a flat single color behind glass panels doesn't
    read as "glass" at all. When glass mode is off this just renders a
    plain flat theme background instead.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme = THEMES["dark"]
        self.glass = True
        self.accent = self.theme["accent"]

    def set_theme(self, t, accent, glass):
        self.theme = t
        self.accent = accent
        self.glass = glass
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        grad = QLinearGradient(0, 0, rect.width(), rect.height())
        grad.setColorAt(0, QColor(self.theme["bg_a"]))
        grad.setColorAt(1, QColor(self.theme["bg_b"]))
        p.fillRect(rect, grad)

        if not self.glass:
            return

        def blob(cx, cy, r, color, alpha):
            c = QColor(color)
            c.setAlpha(alpha)
            p.setPen(Qt.NoPen)
            p.setBrush(c)
            p.drawEllipse(QPoint(int(cx), int(cy)), int(r), int(r))

        w, h = rect.width(), rect.height()
        blob(w * 0.15, h * 0.1, max(w, h) * 0.28, self.accent, 55)
        blob(w * 0.9, h * 0.25, max(w, h) * 0.22, self.theme["secondary"], 45)
        blob(w * 0.75, h * 0.95, max(w, h) * 0.3, self.theme["accent"], 35)


# --------------------------------------------------------------------------
# GLASS CARD SHADOW HELPER
# --------------------------------------------------------------------------
def apply_glass_shadow(widget, color="#000000", blur=36, alpha=110, offset=(0, 10)):
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(*offset)
    c = QColor(color)
    c.setAlpha(alpha)
    effect.setColor(c)
    widget.setGraphicsEffect(effect)


# --------------------------------------------------------------------------
# HOVER-AWARE ICON WIDGETS
#
# A themed icon tinted to match the button's *resting* background
# disappears the instant the button's hover/checked state paints a
# solid accent/danger/warn background behind it (same color on same
# color). These mixins swap to a fixed white icon (Assets/HoverIcons/)
# whenever the button is hovered or checked, and back to the normal
# themed/tinted icon otherwise.
# --------------------------------------------------------------------------
class HoverIconMixin:
    def _init_hover_icons(self):
        self._normal_icon = QIcon()
        self._hover_icon = QIcon()

    def set_icons(self, normal_icon, hover_icon):
        self._normal_icon = stable_icon(normal_icon)
        self._hover_icon = stable_icon(hover_icon)
        self._refresh_icon()

    def _refresh_icon(self):
        active = self.underMouse() or (self.isCheckable() and self.isChecked())
        self.setIcon(self._hover_icon if active else self._normal_icon)

    def enterEvent(self, event):
        self._refresh_icon()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._refresh_icon()
        super().leaveEvent(event)


class TreeGuideRow(QWidget):
    """
    A row in the Group-by-folder view that paints VS Code/Solution
    Explorer-style vertical indent-guide lines - one thin line per
    ancestor nesting level, running the full height of the row - behind
    its child widgets (chevron/icon/label). Without these the row is
    just indented whitespace, which reads as "misaligned" rather than
    "a tree" once there's more than one level of nesting.
    """
    def __init__(self, depth, indent_px, guide_color, parent=None, on_click=None,
                 content_left=0, content_height=None):
        super().__init__(parent)
        self._depth = depth
        self._indent_px = indent_px
        self._pen = QPen(QColor(guide_color))
        self._pen.setWidth(1)
        self._on_click = on_click
        self._selected = False
        self._selected_brush = None
        # Where this row's own text/icon actually start (past the tree
        # indent/chevron column) and how tall that content really is -
        # the widget itself is stretched to the list's full row height by
        # Qt's item padding, which is taller than the content, so the
        # selection fill needs its own size instead of just self.rect()
        # or it comes out oversized and bleeding left into the guide lines.
        self._content_left = content_left
        self._content_height = content_height
        if on_click is not None:
            self.setCursor(Qt.PointingHandCursor)

    def setSelected(self, selected, accent_color=None):
        # QListWidget::item:selected's background is drawn by the item
        # delegate *behind* this row, but this row is a full-rect item
        # widget (setItemWidget) sitting on top of it, so that highlight
        # never actually reaches the screen - this row has to paint its
        # own selection fill to show it at all. That's why a selected
        # file inside a group used to look unselected/uncolored.
        self._selected = selected
        if accent_color is not None:
            self._selected_brush = QColor(accent_color)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        if self._selected and self._selected_brush is not None:
            # A flat, edge-to-edge, unrounded tint spanning the row's full
            # widget rect - a normal file-tree row selection (VS Code,
            # Explorer) is a plain full-row highlight, not an inset rounded
            # chip. The row widget's own rect *is* the row's real geometry
            # (Qt stretches it to the item's full width/height already),
            # so no separate inset/rounded rect is needed here at all.
            tint = QColor(self._selected_brush)
            tint.setAlpha(46)
            p.fillRect(self.rect(), tint)
        # Guide lines stay visible under the translucent selection tint (a
        # solid fill would fully hide them, breaking the tree's visual
        # continuity for a selected row) so the hierarchy still reads.
        if self._depth > 0:
            p.setPen(self._pen)
            for level in range(self._depth):
                x = 4 + level * self._indent_px + self._indent_px // 2
                p.drawLine(x, 0, x, self.height())
        p.end()

    def mousePressEvent(self, event):
        # Lets clicking anywhere on a folder row (icon, name, empty space)
        # collapse/expand it, not just the tiny 16px chevron - the chevron
        # still works too since it stops the click before it reaches here.
        if self._on_click is not None and event.button() == Qt.LeftButton:
            self._on_click()
        super().mousePressEvent(event)


class ElidedLabel(QLabel):
    """
    A QLabel that elides its own text to whatever width it is actually
    given, re-measuring on every paint instead of once at construction
    time. The group-by-folder view used to bake elided text in at build
    time using the panel's width *then* - correct at that instant, but
    permanently wrong (clipped/overlapping-looking) the moment the panel
    was resized afterward, since nothing re-measured it. Self-eliding
    sidesteps that without needing to rebuild/reflow the whole file list
    on every resize (which risked feedback loops with the scrollbar).
    """
    def __init__(self, full_text, color, parent=None):
        super().__init__(parent)
        self._full_text = full_text
        self._color = QColor(color)
        self._selected_color = None
        self._selected = False
        self.setToolTip(full_text)
        self.setMinimumWidth(0)

    def setFullText(self, text):
        self._full_text = text
        self.setToolTip(text)
        self.update()

    def setSelected(self, selected, selected_color=None):
        # Since this label paints its own text (see paintEvent), the
        # QListWidget::item:selected stylesheet rule never reaches it -
        # that rule only recolors text that's part of the item itself.
        # The row's selection highlight (painted by the list behind this
        # widget) still shows, but without this the label kept its normal
        # foreground color on top of it, which is how grouped-view
        # selection ended up looking wrong/low-contrast compared to the
        # ungrouped list. Callers must re-apply this whenever selection
        # changes, since the label has no way to know on its own.
        self._selected = selected
        if selected_color is not None:
            self._selected_color = QColor(selected_color)
        self.update()

    def paintEvent(self, event):
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(self._full_text, Qt.ElideRight, max(self.width(), 0))
        p = QPainter(self)
        p.setFont(self.font())
        if self._selected and self._selected_color is not None:
            p.setPen(self._selected_color)
        else:
            p.setPen(self._color)
        p.drawText(self.rect(), int(Qt.AlignLeft | Qt.AlignVCenter), elided)
        p.end()

    def sizeHint(self):
        return QSize(0, QFontMetrics(self.font()).height())


class HoverToolButton(HoverIconMixin, QToolButton):
    def __init__(self, *args, **kwargs):
        QToolButton.__init__(self, *args, **kwargs)
        self._init_hover_icons()
        self.toggled.connect(lambda _c: self._refresh_icon())


class HoverRadioButton(HoverIconMixin, QRadioButton):
    def __init__(self, *args, **kwargs):
        QRadioButton.__init__(self, *args, **kwargs)
        self._init_hover_icons()
        self.toggled.connect(lambda _c: self._refresh_icon())


# --------------------------------------------------------------------------
# ANIMATED DARK/LIGHT TOGGLE SWITCH (reused for any on/off setting)
# --------------------------------------------------------------------------
class GlassSwitch(QAbstractButton):
    """A pill-shaped, animated on/off toggle - the knob glides across on
    click via QPropertyAnimation instead of snapping, and the track
    color eases between "off" and "on" colors at the same time. Used
    for both the dark/light toggle and the glass-effect toggle."""

    def __init__(self, parent=None, checked=False, off_color="#3a3f55", on_color="#4fd1ff",
                 glyphs=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self._pos = 1.0 if checked else 0.0
        self._off_color = QColor(off_color)
        self._on_color = QColor(on_color)
        self._glyphs = glyphs  # optional ("off_svg_name", "on_svg_name")-like callables
        self.setFixedSize(58, 30)
        self.setCursor(Qt.PointingHandCursor)
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)

    def set_colors(self, off_color, on_color):
        self._off_color = QColor(off_color)
        self._on_color = QColor(on_color)
        self.update()

    def _animate(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def getKnobPos(self):
        return self._pos

    def setKnobPos(self, value):
        self._pos = value
        self.update()

    knobPos = Property(float, getKnobPos, setKnobPos)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()

        o, n = self._off_color, self._on_color
        track = QColor(
            round(o.red() + (n.red() - o.red()) * self._pos),
            round(o.green() + (n.green() - o.green()) * self._pos),
            round(o.blue() + (n.blue() - o.blue()) * self._pos),
        )
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(0, 0, w, h, h / 2, h / 2)

        knob_d = h - 6
        knob_x = 3 + (w - h) * self._pos
        p.setBrush(QColor(255, 255, 255, 235))
        p.drawEllipse(int(knob_x), 3, knob_d, knob_d)


# --------------------------------------------------------------------------
# CUSTOM GLASSY CHECKBOX (replaces the stock QCheckBox everywhere)
# --------------------------------------------------------------------------
class GlassCheckbox(QAbstractButton):
    """A rounded, glassy checkbox that fills with the accent color and
    pops a checkmark in with an OutBack ease, instead of the native OS
    checkbox square. Drop-in for QCheckBox: supports text/isChecked/
    setChecked/toggled the same way."""

    def __init__(self, text="", parent=None, accent="#4fd1ff", fg="#e6e9f0"):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self._text = text
        self._accent = QColor(accent)
        self._fg = QColor(fg)
        self._progress = 0.0
        self._anim = QPropertyAnimation(self, b"checkProgress", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutBack)
        self.toggled.connect(self._animate)
        self.setMinimumHeight(22)

    def setText(self, text):
        self._text = text
        self.updateGeometry()
        self.update()

    def text(self):
        return self._text

    def set_colors(self, accent_hex, fg_hex):
        self._accent = QColor(accent_hex)
        self._fg = QColor(fg_hex)
        self.update()

    def _animate(self, checked):
        self._anim.stop()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def getCheckProgress(self):
        return self._progress

    def setCheckProgress(self, value):
        self._progress = max(0.0, min(1.0, value))
        self.update()

    checkProgress = Property(float, getCheckProgress, setCheckProgress)

    def sizeHint(self):
        fm = self.fontMetrics()
        w = 18 + 8 + fm.horizontalAdvance(self._text) + 6
        return QSize(w, max(22, fm.height() + 6))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        box = 18
        box_rect = QRectF(1, (self.height() - box) / 2, box, box)
        prog = self._progress

        base = QColor(255, 255, 255, 30)
        fill = QColor(
            round(base.red() + (self._accent.red() - base.red()) * prog),
            round(base.green() + (self._accent.green() - base.green()) * prog),
            round(base.blue() + (self._accent.blue() - base.blue()) * prog),
            round(base.alpha() + (255 - base.alpha()) * prog),
        )
        border = self._accent if prog > 0.05 else QColor(255, 255, 255, 90)
        p.setPen(QPen(border, 1.6))
        p.setBrush(fill)
        p.drawRoundedRect(box_rect, 5, 5)

        if prog > 0.02:
            check_color = contrast_fg(self._accent.name())
            pen = QPen(QColor(check_color), 2.2)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            p.setPen(pen)
            p.setOpacity(min(1.0, prog * 1.3))
            l, t = box_rect.left(), box_rect.top()
            pts = [QPointF(l + 4, t + 9.5), QPointF(l + 7.5, t + 13), QPointF(l + 14, t + 5)]
            p.drawPolyline(pts)
            p.setOpacity(1.0)

        p.setPen(self._fg)
        text_rect = QRectF(box + 8, 0, self.width() - box - 8, self.height())
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, self._text)


# --------------------------------------------------------------------------
# CUSTOM GLASSY COLOR PICKER (replaces the stock QColorDialog)
# --------------------------------------------------------------------------
class ColorSwatch(QAbstractButton):
    """One round, glassy accent-color swatch - a soft radial highlight
    plus a glowing ring when selected, instead of a flat square."""

    def __init__(self, color_hex, parent=None):
        super().__init__(parent)
        self.color_hex = color_hex
        self.setCheckable(True)
        self.setFixedSize(34, 34)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(3, 3, -3, -3)
        base = QColor(self.color_hex)

        grad = QLinearGradient(r.topLeft(), r.bottomRight())
        grad.setColorAt(0, base.lighter(130))
        grad.setColorAt(1, base.darker(110))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawEllipse(r)

        # glassy highlight arc
        p.setBrush(QColor(255, 255, 255, 90))
        p.drawEllipse(r.adjusted(4, 3, -12, -14))

        if self.isChecked():
            pen = QPen(QColor(255, 255, 255, 230), 2.4)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


# --------------------------------------------------------------------------
# ICON BUTTON HELPER
# --------------------------------------------------------------------------
def make_tool_button(text, icon_name, on_click, object_name=None, tooltip=None):
    btn = HoverToolButton()
    btn.setText(" " + text)
    btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    btn.setIconSize(QSize(18, 18))
    btn.setCursor(Qt.PointingHandCursor)
    btn.clicked.connect(on_click)
    if object_name:
        btn.setObjectName(object_name)
    if tooltip:
        btn.setToolTip(tooltip)
    btn._icon_name = icon_name
    return btn


# --------------------------------------------------------------------------
# GLOBAL BUSY OVERLAY (one instance covers the whole window for every
# background task - search, folder loads, Group By's tree rebuild, ...)
# --------------------------------------------------------------------------
class _SpinnerDial(QWidget):
    """A small rotating arc, repainted by BusyOverlay's own QTimer - no
    image assets, cheap enough (one QPainter.drawArc call) to redraw at
    ~30fps without any measurable CPU cost, and it keeps animating for
    the whole time a background QThread is running since nothing about
    it touches that thread."""

    def __init__(self, parent=None, diameter=34, color="#4fd1ff"):
        super().__init__(parent)
        self._angle = 0
        self._color = QColor(color)
        self.setFixedSize(diameter, diameter)

    def set_angle(self, angle):
        self._angle = angle
        self.update()

    def set_color(self, color_hex):
        self._color = QColor(color_hex)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self._color)
        pen.setWidth(4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        rect = self.rect().adjusted(3, 3, -3, -3)
        p.drawArc(rect, -self._angle * 16, 110 * 16)


class BusyOverlay(QWidget):
    """
    Full-window translucent overlay shown for the duration of any
    background task run through FinderWindow._run_busy - a directory
    scan, a search, Group By's tree rebuild, anything. Centers a small
    animated spinner, a status message, and (only when the running task
    is cancellable) a progress bar and Cancel button, over a dimmed
    background that swallows mouse/keyboard input so a second click or
    view switch can't race the task already running underneath it.

    The spin timer keeps firing (and this widget keeps repainting) for
    as long as the overlay is visible regardless of what the background
    QThread is doing - painting only ever happens on the UI thread's own
    event loop, which is exactly why this can never itself cause a "Not
    Responding" state the way blocking work on that thread would.
    """

    cancelRequested = Signal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setVisible(False)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setFocusPolicy(Qt.StrongFocus)  # so it - not whatever's behind it - eats key presses

        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._spin)
        self._angle = 0

        self._card = QFrame(self)
        self._card.setObjectName("busyOverlayCard")
        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(30, 24, 30, 22)
        card_layout.setSpacing(12)

        self._spinner = _SpinnerDial(self._card)
        card_layout.addWidget(self._spinner, 0, Qt.AlignHCenter)

        self._label = QLabel("Working...")
        self._label.setObjectName("busyOverlayLabel")
        self._label.setAlignment(Qt.AlignHCenter)
        self._label.setWordWrap(True)
        self._label.setFixedWidth(240)
        card_layout.addWidget(self._label)

        self._bar = QProgressBar()
        self._bar.setObjectName("busyOverlayBar")
        self._bar.setFixedWidth(200)
        self._bar.setFixedHeight(6)
        self._bar.setTextVisible(False)
        self._bar.hide()
        card_layout.addWidget(self._bar, 0, Qt.AlignHCenter)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setObjectName("busyOverlayCancel")
        self._cancel_btn.setCursor(Qt.PointingHandCursor)
        self._cancel_btn.hide()
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        card_layout.addWidget(self._cancel_btn, 0, Qt.AlignHCenter)

    def apply_theme(self, accent_hex, card_bg, border_hex, fg_hex):
        self._spinner.set_color(accent_hex)
        self._card.setStyleSheet(
            f"#busyOverlayCard {{ background: {card_bg}; border: 1px solid {border_hex}; border-radius: 14px; }}"
        )
        self._label.setStyleSheet(f"color: {fg_hex}; background: transparent; border: none;")
        self._bar.setStyleSheet(
            f"QProgressBar#busyOverlayBar {{ background: {border_hex}; border: none; border-radius: 3px; }}"
            f"QProgressBar#busyOverlayBar::chunk {{ background: {accent_hex}; border-radius: 3px; }}"
        )
        self._cancel_btn.setStyleSheet(f"""
            QPushButton#busyOverlayCancel {{ background: transparent; color: {accent_hex};
                border: 1px solid {accent_hex}; border-radius: 8px; padding: 5px 16px; font-weight: 600; }}
            QPushButton#busyOverlayCancel:hover {{ background: {accent_hex}; color: {card_bg}; }}
        """)

    def _on_cancel_clicked(self):
        self._cancel_btn.setEnabled(False)
        self._label.setText("Cancelling...")
        self.cancelRequested.emit()

    def _spin(self):
        self._angle = (self._angle + 8) % 360
        self._spinner.set_angle(self._angle)

    def start(self, message, cancellable=False):
        self._label.setText(message)
        self._cancel_btn.setVisible(cancellable)
        self._cancel_btn.setEnabled(True)
        self._bar.setVisible(cancellable)
        self._bar.setRange(0, 0)
        if self.parentWidget() is not None:
            self.setGeometry(self.parentWidget().rect())
        self._reposition_card()
        self.show()
        self.raise_()
        self.setFocus(Qt.OtherFocusReason)
        self._timer.start()

    def set_progress(self, current, total, message=None):
        if message is not None:
            self._label.setText(message)
        if total > 0:
            self._bar.setRange(0, total)
            self._bar.setValue(current)
        else:
            self._bar.setRange(0, 0)
        self._reposition_card()

    def stop(self):
        self._timer.stop()
        self.hide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition_card()

    def _reposition_card(self):
        self._card.adjustSize()
        x = max(0, (self.width() - self._card.width()) // 2)
        y = max(0, (self.height() - self._card.height()) // 2)
        self._card.move(x, y)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 110))

    # Swallow every input event so nothing behind the overlay can react
    # to a click/keystroke while a background task owns the screen.
    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseMoveEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        event.accept()

    def keyPressEvent(self, event):
        event.accept()
