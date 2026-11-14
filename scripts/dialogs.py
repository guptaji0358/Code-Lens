"""
dialogs.py - every popup/dialog window CodeLens uses: the accent color
picker, shortcuts editor, file preview, file details table, the
animated replacement for QMessageBox, and the slide-down settings
panel.
"""

import datetime
import html
import os
import re

from PySide6.QtCore import (
    Qt, QTimer, QRect, QPoint, QSize, QEasingCurve, QPropertyAnimation,
    QParallelAnimationGroup, Signal,
)
from PySide6.QtGui import QIcon, QKeySequence, QShortcut, QFont, QColor
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QLineEdit, QTextEdit, QPlainTextEdit, QButtonGroup,
    QStackedWidget, QKeySequenceEdit, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QGraphicsOpacityEffect, QListWidget, QListWidgetItem, QTextBrowser,
    QSplitter,
)

import CodeLensCLI as core
from codelens_agent import reference
from theme import ACCENT_PRESETS, FONT_MONO, THEMES, with_alpha, contrast_fg, icon
from widgets import ColorSwatch, GlassSwitch, GlassCheckbox, HoverToolButton, apply_glass_shadow
from persistence import SHORTCUT_ACTIONS


class AccentColorPicker(QDialog):
    """
    A frameless, glass-styled popup for choosing the app's accent color
    - a grid of preset glassy swatches plus a hex entry for anything
    custom, replacing the plain system color dialog everywhere the app
    needs an accent pick.
    """

    def __init__(self, parent, t, current_hex):
        super().__init__(parent)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.chosen = current_hex

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        card = QFrame(self)
        card.setStyleSheet(f"""
            QFrame {{
                background: {with_alpha(t['bg_panel'], 235)};
                border-radius: 16px; border: 1px solid {with_alpha('#ffffff', 40)};
            }}
        """)
        apply_glass_shadow(card, blur=40, alpha=140)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        title = QLabel("Accent Color")
        title.setStyleSheet(f"color:{t['fg']}; font-weight:700; font-size:11pt;")
        lay.addWidget(title)

        grid = QGridLayout()
        grid.setSpacing(8)
        self.swatches = []
        for i, color_hex in enumerate(ACCENT_PRESETS):
            sw = ColorSwatch(color_hex)
            sw.setChecked(color_hex.lower() == current_hex.lower())
            sw.clicked.connect(lambda _c=False, h=color_hex: self._pick(h))
            grid.addWidget(sw, i // 5, i % 5)
            self.swatches.append(sw)
        lay.addLayout(grid)

        custom_row = QHBoxLayout()
        self.hex_entry = QLineEdit(current_hex)
        self.hex_entry.setStyleSheet(f"""
            QLineEdit {{ background: {with_alpha(t['bg_card'], 200)}; color:{t['fg']};
                border: 1px solid {with_alpha('#ffffff', 40)}; border-radius: 8px; padding: 6px 8px; }}
        """)
        self.preview = ColorSwatch(current_hex)
        self.preview.setEnabled(False)
        custom_row.addWidget(self.preview)
        custom_row.addWidget(self.hex_entry, 1)
        self.hex_entry.textChanged.connect(self._on_hex_edit)
        lay.addLayout(custom_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        apply_btn = QPushButton("Apply")
        apply_btn.setCursor(Qt.PointingHandCursor)
        apply_btn.setStyleSheet(f"""
            QPushButton {{ background: {current_hex}; color: {contrast_fg(current_hex)};
                border: none; border-radius: 8px; padding: 7px 18px; font-weight: 600; }}
        """)
        apply_btn.clicked.connect(self.accept)
        btn_row.addWidget(apply_btn)
        lay.addLayout(btn_row)
        self._apply_btn = apply_btn

    def _pick(self, color_hex):
        self.chosen = color_hex
        self.hex_entry.setText(color_hex)
        for sw in self.swatches:
            sw.setChecked(sw.color_hex.lower() == color_hex.lower())
        self._refresh_preview()

    def _on_hex_edit(self, text):
        text = text.strip()
        if re.fullmatch(r"#[0-9a-fA-F]{6}", text):
            self.chosen = text
            for sw in self.swatches:
                sw.setChecked(sw.color_hex.lower() == text.lower())
            self._refresh_preview()

    def _refresh_preview(self):
        self.preview.color_hex = self.chosen
        self.preview.update()
        self._apply_btn.setStyleSheet(f"""
            QPushButton {{ background: {self.chosen}; color: {contrast_fg(self.chosen)};
                border: none; border-radius: 8px; padding: 7px 18px; font-weight: 600; }}
        """)

    @staticmethod
    def pick(parent, t, current_hex):
        dlg = AccentColorPicker(parent, t, current_hex)
        dlg.adjustSize()
        btn = parent.btn_accent if hasattr(parent, "btn_accent") else None
        if btn is not None:
            gp = btn.mapToGlobal(QPoint(0, btn.height() + 6))
            dlg.move(gp)
        if dlg.exec() == QDialog.Accepted:
            return dlg.chosen
        return None


# --------------------------------------------------------------------------
# SHORTCUTS EDITOR (glass-styled, replaces a plain settings table)
# --------------------------------------------------------------------------
class ShortcutsDialog(QDialog):
    """Lists every editable shortcut with a QKeySequenceEdit box - click
    a box and press the new combination, then Save to rebind and persist
    it to shortcuts.json."""

    def __init__(self, parent, t, glass, current):
        super().__init__(parent)
        # WindowStaysOnTopHint: a frameless, translucent Qt.Dialog on
        # Windows has been observed opening genuinely behind whatever
        # window currently has focus instead of coming to the front.
        # Because it's still modal, that leaves it silently blocking all
        # input to this app while sitting somewhere the user can't see or
        # find it (it also gets no Alt+Tab entry, being frameless) -
        # indistinguishable from a total freeze.
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        # See AnimatedDialog's __init__ for why WA_TranslucentBackground is
        # deliberately not used here: on this machine that compositing path
        # was directly confirmed to paint nothing at all (verified by
        # reading actual on-screen pixels), leaving an invisible-but-modal
        # dialog that looks exactly like a hang.
        self.setModal(True)
        self.result_map = None

        self.setStyleSheet(f"background: {t['bg_panel']};")
        self.setAutoFillBackground(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        card = QFrame(self)
        # See AnimatedDialog's card for why this is a plain opaque
        # background with no QGraphicsDropShadowEffect.
        card.setStyleSheet(f"""
            QFrame {{ background: {t['bg_panel']}; border-radius: 16px; border: 1px solid {t['accent']}; }}
        """)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)

        title = QLabel("Keyboard Shortcuts")
        title.setStyleSheet(f"color: {t['fg']}; font-size: 12.5pt; font-weight: 600;")
        lay.addWidget(title)
        hint = QLabel("Click a box below and press a new key combination to rebind it.")
        hint.setStyleSheet(f"color: {t['fg_dim']}; font-size: 9pt;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(10)
        self.edits = {}
        for row, (key, label, default) in enumerate(SHORTCUT_ACTIONS):
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {t['fg']};")
            grid.addWidget(lbl, row, 0)
            edit = QKeySequenceEdit(QKeySequence(current.get(key, default)))
            edit.setStyleSheet(f"""
                QKeySequenceEdit {{ background: {with_alpha(t['bg_card'], 200)}; color: {t['fg']};
                    border: 1px solid {with_alpha('#ffffff', 40)}; border-radius: 8px; padding: 4px 8px; }}
            """)
            grid.addWidget(edit, row, 1)
            self.edits[key] = edit
        lay.addLayout(grid)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {t['fg_dim']};
                border: 1px solid {t['border']}; border-radius: 8px; padding: 7px 16px; }}
            QPushButton:hover {{ background: {t['bg_card']}; color: {t['fg']}; }}
        """)
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{ background: {t['accent']}; color: {contrast_fg(t['accent'])};
                border: none; border-radius: 8px; padding: 7px 18px; font-weight: 600; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(save_btn)
        lay.addLayout(btn_row)

    def _save(self):
        defaults = {key: default for key, _label, default in SHORTCUT_ACTIONS}
        self.result_map = {key: edit.keySequence().toString() or defaults[key]
                            for key, edit in self.edits.items()}
        self.accept()

    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parent()
        self.adjustSize()
        if parent is not None:
            pg = parent.geometry()
            w, h = self.width(), self.height()
            self.setGeometry(pg.center().x() - w // 2, pg.center().y() - h // 2, w, h)

    @staticmethod
    def edit(parent, t, glass, current):
        dlg = ShortcutsDialog(parent, t, glass, current)
        if dlg.exec() == QDialog.Accepted:
            return dlg.result_map
        return None


# --------------------------------------------------------------------------
# FILE PREVIEW DIALOG - multiple view modes + zoom
# --------------------------------------------------------------------------
class PreviewDialog(QDialog):
    """
    Opened by double-clicking a loaded file in the left panel. Gives two
    ways to look at the same file (Formatted, with line numbers colored
    like the results pane; Raw, exact plain text for easy copying),
    independent line-wrap, and zoom in/out - the things people actually
    reach for when a file is hard to read at the default size/format.
    A normal (non-frameless) resizable window, since a content viewer
    benefits from OS resize/maximize more than from matching the small
    glass popups used for quick confirmations elsewhere in the app.
    """

    ZOOM_MIN_STEPS, ZOOM_MAX_STEPS = -5, 20

    def __init__(self, parent, t, icon_theme, glass, entry):
        super().__init__(parent)
        self.entry = entry
        self.t = t
        self.zoom_steps = 0

        self.setWindowTitle(f"Preview - {os.path.basename(entry.path)}")
        self.resize(920, 680)
        self.setStyleSheet(f"""
            QDialog {{ background: {t['bg_panel']}; }}
            QLabel {{ color: {t['fg']}; }}
            QToolButton {{ background: {t['bg_card']}; border: 1px solid {t['border']};
                border-radius: 8px; color: {t['fg']}; padding: 6px; }}
            QToolButton:hover {{ background: {t['accent']}; border-color: {t['accent']}; }}
            QToolButton:checked {{ background: {t['accent']}; border-color: {t['accent']}; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel(os.path.basename(entry.path))
        title.setStyleSheet(f"font-weight: 700; font-size: 12.5pt; color: {t['fg']};")
        header.addWidget(title)
        path_lbl = QLabel(entry.path)
        path_lbl.setStyleSheet(f"color: {t['fg_dim']}; font-size: 9pt;")
        header.addWidget(path_lbl, 1)
        meta_lbl = QLabel(f"{len(entry.lines):,} lines - {entry.encoding}")
        meta_lbl.setStyleSheet(f"color: {t['fg_dim']}; font-size: 9pt;")
        header.addWidget(meta_lbl)
        layout.addLayout(header)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.btn_formatted = self._small_button("lines", icon_theme, "Formatted view (with line numbers)")
        self.btn_raw = self._small_button("file", icon_theme, "Raw text view (exact file content)")
        self.btn_formatted.setChecked(True)
        self.view_group.addButton(self.btn_formatted)
        self.view_group.addButton(self.btn_raw)
        self.view_group.buttonToggled.connect(self._on_view_toggle)
        toolbar.addWidget(self.btn_formatted)
        toolbar.addWidget(self.btn_raw)

        toolbar.addSpacing(10)
        self.wrap_chk = GlassCheckbox("Wrap", accent=t["accent"], fg=t["fg"])
        self.wrap_chk.toggled.connect(self._apply_wrap)
        toolbar.addWidget(self.wrap_chk)

        toolbar.addStretch()
        self.btn_zoom_out = self._small_button("zoomout", icon_theme, "Zoom out  (Ctrl+-)")
        self.btn_zoom_out.setCheckable(False)
        self.btn_zoom_out.clicked.connect(self._zoom_out)
        self.zoom_lbl = QLabel("100%")
        self.zoom_lbl.setFixedWidth(42)
        self.zoom_lbl.setAlignment(Qt.AlignCenter)
        self.btn_zoom_in = self._small_button("zoomin", icon_theme, "Zoom in  (Ctrl++)")
        self.btn_zoom_in.setCheckable(False)
        self.btn_zoom_in.clicked.connect(self._zoom_in)
        toolbar.addWidget(self.btn_zoom_out)
        toolbar.addWidget(self.zoom_lbl)
        toolbar.addWidget(self.btn_zoom_in)
        layout.addLayout(toolbar)

        self.stack = QStackedWidget()
        self.formatted_view = QTextEdit()
        self.formatted_view.setReadOnly(True)
        self.raw_view = QPlainTextEdit()
        self.raw_view.setReadOnly(True)
        self.stack.addWidget(self.formatted_view)
        self.stack.addWidget(self.raw_view)
        layout.addWidget(self.stack, 1)

        content_style = (f"background: {t['bg_card']}; color: {t['fg']}; "
                          f"border: 1px solid {t['border']}; border-radius: 8px; "
                          f"font-family: '{FONT_MONO}'; padding: 6px;")
        self.formatted_view.setStyleSheet(content_style)
        self.raw_view.setStyleSheet(content_style)

        self._build_formatted()
        self.raw_view.setPlainText("\n".join(entry.lines))
        self._apply_wrap(False)

        for seq in ("Ctrl+=", "Ctrl++"):
            QShortcut(QKeySequence(seq), self).activated.connect(self._zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(self._zoom_out)
        QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(self._zoom_reset)

    def _small_button(self, icon_name, icon_theme, tooltip):
        b = HoverToolButton()
        b.setCheckable(True)
        b.setToolButtonStyle(Qt.ToolButtonIconOnly)
        b.setIconSize(QSize(15, 15))
        b.setFixedSize(30, 30)
        b.setCursor(Qt.PointingHandCursor)
        b.setToolTip(tooltip)
        b.set_icons(icon(icon_name, icon_theme), icon(icon_name, "hover"))
        return b

    def _build_formatted(self):
        width = len(str(max(len(self.entry.lines), 1)))
        accent = self.t["accent"]
        parts = []
        for i, line in enumerate(self.entry.lines, start=1):
            num = str(i).rjust(width)
            esc = (line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            parts.append(f'<span style="color:{accent}">{num}:</span> {esc}')
        self.formatted_view.setHtml("<br>".join(parts) if parts else "")

    def _on_view_toggle(self, button, checked):
        if not checked:
            return
        self.stack.setCurrentIndex(0 if button is self.btn_formatted else 1)

    def _apply_wrap(self, checked):
        self.formatted_view.setLineWrapMode(QTextEdit.WidgetWidth if checked else QTextEdit.NoWrap)
        self.raw_view.setLineWrapMode(QPlainTextEdit.WidgetWidth if checked else QPlainTextEdit.NoWrap)

    # See FinderWindow's zoom methods for why zoomIn()/zoomOut() (not
    # setFont) are used - the content_style stylesheet above sets
    # font-family on these widgets, which defeats a plain setFont()
    # point-size change.
    def _zoom_in(self):
        if self.zoom_steps >= self.ZOOM_MAX_STEPS:
            return
        for w in (self.formatted_view, self.raw_view):
            w.zoomIn(1)
        self.zoom_steps += 1
        self.zoom_lbl.setText(f"{100 + self.zoom_steps * 10}%")

    def _zoom_out(self):
        if self.zoom_steps <= self.ZOOM_MIN_STEPS:
            return
        for w in (self.formatted_view, self.raw_view):
            w.zoomOut(1)
        self.zoom_steps -= 1
        self.zoom_lbl.setText(f"{100 + self.zoom_steps * 10}%")

    def _zoom_reset(self):
        for w in (self.formatted_view, self.raw_view):
            if self.zoom_steps > 0:
                w.zoomOut(self.zoom_steps)
            elif self.zoom_steps < 0:
                w.zoomIn(-self.zoom_steps)
        self.zoom_steps = 0
        self.zoom_lbl.setText("100%")

    @staticmethod
    def preview(parent, t, icon_theme, glass, entry):
        dlg = PreviewDialog(parent, t, icon_theme, glass, entry)
        dlg.exec()


# --------------------------------------------------------------------------
# FILE DETAILS DIALOG - full metadata for every loaded file, in one place
# --------------------------------------------------------------------------
class FileDetailsDialog(QDialog):
    """A separate window listing every loaded file's full metadata
    (lines, size, encoding, last-modified, full path) in a sortable
    table - opened via the toolbar's Details button rather than only
    getting a truncated one-line summary per file in the main list.
    Files are organized into sections by file-type label (derived from
    each file's extension - .h files under "H", .cpp under "CPP", etc.)
    instead of one continuous list."""

    # Friendly section names for extensions that don't read well
    # uppercased as-is (".cpp" -> "CPP" is fine; a few common ones read
    # better spelled out).
    TYPE_LABELS = {
        "H": "H (Header)", "HPP": "HPP (Header)", "HH": "HH (Header)",
        "CPP": "C++", "CXX": "C++", "CC": "C++",
        "C": "C", "PY": "Python", "JS": "JavaScript", "TS": "TypeScript",
        "": "No Extension",
    }

    def __init__(self, parent, t, files):
        super().__init__(parent)
        self.setWindowTitle("File Details")
        self.resize(820, 440)
        self.setStyleSheet(f"""
            QDialog {{ background: {t['bg_panel']}; }}
            QLabel {{ color: {t['fg']}; }}
            QTableWidget {{ background: {t['bg_card']}; color: {t['fg']};
                gridline-color: {t['border']}; border: 1px solid {t['border']};
                border-radius: 8px; selection-background-color: {t['accent']};
                selection-color: {contrast_fg(t['accent'])}; }}
            QHeaderView::section {{ background: {t['bg_panel']}; color: {t['fg_dim']};
                padding: 6px; border: none; border-bottom: 1px solid {t['border']}; }}
            QPushButton {{ background: {t['accent']}; color: {contrast_fg(t['accent'])};
                border: none; border-radius: 8px; padding: 7px 18px; font-weight: 600; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        title = QLabel(f"File Details  -  {len(files)} loaded")
        title.setStyleSheet(f"font-weight: 700; font-size: 12.5pt; color: {t['fg']};")
        layout.addWidget(title)

        columns = ["Name", "Lines", "Size", "Encoding", "Modified", "Full Path"]
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setAlternatingRowColors(False)
        table.horizontalHeader().setStretchLastSection(True)
        # Section header rows (below) are plain, non-selectable, full-width
        # cells - a native column re-sort would happily scramble those in
        # among the data rows, so sorting stays off; ordering is grouping
        # (by file type) then name, both applied explicitly below instead.
        table.setSortingEnabled(False)

        # Group related files together under a section header instead of
        # one continuous flat list, same idea as the main window's
        # group-by-folder view - grouped by file type here (every .h
        # under one "H" section, every .cpp under "C++", etc.) since
        # Details spans every loaded file regardless of folder.
        section_font = QFont(table.font())
        section_font.setBold(True)
        sections = {}
        for entry in files:
            ext = os.path.splitext(entry.path)[1].lstrip(".").upper()
            label = self.TYPE_LABELS.get(ext, ext)
            sections.setdefault(label, []).append(entry)

        row = 0
        for section_name in sorted(sections, key=str.lower):
            section_files = sorted(sections[section_name], key=lambda e: os.path.basename(e.path).lower())
            table.insertRow(row)
            header_item = QTableWidgetItem(f"{section_name}   ({len(section_files)})")
            header_item.setFlags(Qt.ItemIsEnabled)
            header_item.setFont(section_font)
            header_item.setForeground(QColor(t["fg_dim"]))
            header_item.setBackground(QColor(t["bg_panel"]))
            table.setItem(row, 0, header_item)
            table.setSpan(row, 0, 1, len(columns))
            row += 1

            for entry in section_files:
                size = core.format_size(entry.size) if entry.size is not None else "?"
                modified = (datetime.datetime.fromtimestamp(entry.mtime).strftime("%Y-%m-%d %H:%M")
                            if entry.mtime is not None else "?")
                values = [os.path.basename(entry.path), f"{len(entry.lines):,}", size,
                          entry.encoding or "?", modified, entry.path]
                table.insertRow(row)
                for col, val in enumerate(values):
                    item = QTableWidgetItem(val)
                    item.setToolTip(entry.path)
                    table.setItem(row, col, item)
                row += 1

        table.resizeColumnsToContents()
        layout.addWidget(table, 1)
        self._table = table

        btn_row = QHBoxLayout()
        reveal_btn = QPushButton("Open in Explorer")
        reveal_btn.setCursor(Qt.PointingHandCursor)
        reveal_btn.setEnabled(False)
        reveal_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {t['fg']};
                border: 1px solid {t['border']}; border-radius: 8px; padding: 7px 16px; font-weight: 600; }}
            QPushButton:hover:!disabled {{ background: {t['bg_card']}; }}
            QPushButton:disabled {{ color: {t['fg_dim']}; }}
        """)
        reveal_btn.clicked.connect(self._reveal_selected)
        table.itemSelectionChanged.connect(
            lambda: reveal_btn.setEnabled(self._selected_path() is not None))
        self._reveal_btn = reveal_btn
        btn_row.addWidget(reveal_btn)
        btn_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _selected_path(self):
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        path_item = self._table.item(rows[0].row(), self._table.columnCount() - 1)
        # Section header rows span every column as one item in column 0 -
        # a real data row's last column always holds the full path.
        if path_item is None or self._table.columnSpan(rows[0].row(), 0) > 1:
            return None
        return path_item.text()

    def _reveal_selected(self):
        path = self._selected_path()
        if path and not core.reveal_in_explorer(path):
            parent = self.parent()
            if parent is not None and hasattr(parent, "_error"):
                parent._error("Couldn't reveal file", f"This file no longer exists on disk:\n\n{path}")

    @staticmethod
    def show_details(parent, t, files):
        dlg = FileDetailsDialog(parent, t, files)
        dlg.exec()


class CommandReferenceDialog(QDialog):
    """AI Command Reference: the read-only agent interface (MCP tools and CLI
    commands) browsable in the GUI. All content comes from
    codelens_agent.reference, which derives it from the live argparse parser,
    MCP tool schemas and ops metadata - nothing is described here by hand.
    Two categories (MCP Tools / CLI Commands), a search box that filters the
    entry list, and copy buttons for the invocation syntax and the example."""

    def __init__(self, parent, t, entries=None):
        super().__init__(parent)
        self._t = t
        self._entries = entries if entries is not None else reference.build_catalogue()
        self._kind = reference.MCP
        self._shown = []
        self.setWindowTitle("AI Command Reference")
        self.resize(980, 600)
        self.setStyleSheet(f"""
            QDialog {{ background: {t['bg_panel']}; }}
            QLabel {{ color: {t['fg']}; }}
            QLineEdit {{ background: {t['bg_card']}; color: {t['fg']};
                border: 1px solid {t['border']}; border-radius: 8px; padding: 7px 10px; }}
            QListWidget, QTextBrowser {{ background: {t['bg_card']}; color: {t['fg']};
                border: 1px solid {t['border']}; border-radius: 8px; }}
            QSplitter::handle {{ background: transparent; }}
            QListWidget::item {{ padding: 7px 8px; }}
            QListWidget::item:selected {{ background: {t['accent']};
                color: {contrast_fg(t['accent'])}; }}
            QPushButton {{ background: {t['accent']}; color: {contrast_fg(t['accent'])};
                border: none; border-radius: 8px; padding: 7px 18px; font-weight: 600; }}
            QPushButton:disabled {{ background: {t['bg_card']}; color: {t['fg_dim']}; }}
        """)
        ghost = (f"QPushButton {{ background: transparent; color: {t['fg']};"
                 f" border: 1px solid {t['border']}; }}"
                 f"QPushButton:hover:!disabled {{ background: {t['bg_card']}; }}"
                 f"QPushButton:checked {{ background: {t['accent']};"
                 f" color: {contrast_fg(t['accent'])}; border-color: {t['accent']}; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        title = QLabel("AI Command Reference")
        title.setStyleSheet(f"font-weight: 700; font-size: 12.5pt; color: {t['fg']};")
        layout.addWidget(title)

        top = QHBoxLayout()
        self._cat_buttons = {}
        group = QButtonGroup(self)
        for kind in (reference.MCP, reference.CLI):
            b = QPushButton()
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(ghost)
            b.clicked.connect(lambda checked=False, k=kind: self.set_category(k))
            group.addButton(b)
            top.addWidget(b)
            self._cat_buttons[kind] = b
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search commands, arguments, descriptions...")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._refilter)
        top.addWidget(self._search, 1)
        layout.addLayout(top)

        # A splitter (not a fixed-width list) so narrow windows keep a readable
        # detail pane; the minimum dialog size below guarantees that floor.
        body = QSplitter(Qt.Horizontal)
        body.setChildrenCollapsible(False)
        body.setHandleWidth(8)
        self._list = QListWidget()
        self._list.setMinimumWidth(150)
        self._list.currentRowChanged.connect(self._show_row)
        body.addWidget(self._list)
        self._detail = QTextBrowser()
        self._detail.setOpenLinks(False)
        self._detail.setMinimumWidth(330)
        body.addWidget(self._detail)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([220, 740])
        layout.addWidget(body, 1)
        self.setMinimumSize(560, 400)

        row = QHBoxLayout()
        self._copy_syntax = QPushButton("Copy syntax")
        self._copy_example = QPushButton("Copy example")
        for b, what in ((self._copy_syntax, "syntax"), (self._copy_example, "example")):
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda checked=False, w=what: self.copy(w))
            row.addWidget(b)
        self._status = QLabel("")
        self._status.setStyleSheet(f"color: {t['fg_dim']};")
        row.addWidget(self._status, 1)
        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        row.addWidget(close_btn)
        layout.addLayout(row)

        self.set_category(self._kind)

    # -- state ---------------------------------------------------------------
    def current_entry(self):
        r = self._list.currentRow()
        return self._shown[r] if 0 <= r < len(self._shown) else None

    def visible_entries(self):
        return list(self._shown)

    def set_category(self, kind):
        self._kind = kind
        for k, b in self._cat_buttons.items():
            n = len(reference.filter_entries(self._entries, "", k))
            b.setText(f"{reference.CATEGORY_TITLES[k]} ({n})")
            b.setChecked(k == kind)
        self._refilter()

    def _refilter(self, *_):
        self._shown = reference.filter_entries(self._entries, self._search.text(), self._kind)
        self._list.blockSignals(True)
        self._list.clear()
        for e in self._shown:
            item = QListWidgetItem(f"{e['label']}\n{e['identifier']}")
            item.setToolTip(e["summary"])
            self._list.addItem(item)
        self._list.blockSignals(False)
        if self._shown:
            self._list.setCurrentRow(0)
            self._show_row(0)
        else:
            self._show_row(-1)

    def _show_row(self, row):
        e = self._shown[row] if 0 <= row < len(self._shown) else None
        self._copy_syntax.setEnabled(e is not None)
        self._copy_example.setEnabled(e is not None)
        self._status.setText("")
        self._detail.setHtml(self._render(e) if e else
                             f"<p style='color:{self._t['fg_dim']}'>No commands match.</p>")

    def copy(self, what="syntax"):
        e = self.current_entry()
        if e is None:
            return False
        QApplication.clipboard().setText(reference.copy_text(e, what))
        self._status.setText(f"Copied {what}.")
        return True

    # -- rendering -----------------------------------------------------------
    def _render(self, e):
        t = self._t
        esc = html.escape
        mono = f"font-family:{FONT_MONO}; background:{t['bg_panel']};"
        def pre(text):
            return (f"<pre style='{mono} padding:8px; white-space:pre-wrap;'>"
                    f"{esc(text)}</pre>")
        def h(text):
            return f"<h4 style='color:{t['accent']}; margin:14px 0 4px 0;'>{text}</h4>"
        rows = []
        for a in e["args"]:
            default = "-" if a["default"] is None else str(a["default"])
            req = "required" if a["required"] else "optional"
            kind = a["type"] + (" (repeatable)" if a["repeatable"] and
                                not a["type"].startswith("array") else "")
            extra = []
            if a.get("choices"):
                extra.append("one of: " + ", ".join(map(str, a["choices"])))
            if a.get("bounds"):
                extra.append(", ".join(f"{k}={v}" for k, v in a["bounds"].items()))
            desc = " ".join([a["description"]] + [f"[{x}]" for x in extra]).strip()
            ident = a.get("form") or a["name"]
            rows.append("<tr>" + "".join(
                f"<td style='padding:3px 8px; border-bottom:1px solid {t['border']};'>{c}</td>"
                for c in (f"<code>{esc(ident)}</code>", esc(kind), esc(default), req, esc(desc))
            ) + "</tr>")
        table = ("<table cellspacing='0' width='100%'><tr>" + "".join(
            f"<th align='left' style='padding:3px 8px; color:{t['fg_dim']};'>{c}</th>"
            for c in ("Argument", "Type", "Default", "Status", "Description"))
            + "</tr>" + "".join(rows) + "</table>") if rows else "<p>No arguments.</p>"
        notes = "".join(f"<li>{esc(n)}</li>" for n in e["notes"])
        return (f"<h3 style='margin:0;'>{esc(e['label'])}</h3>"
                f"<p><code>{esc(e['identifier'])}</code> &nbsp;|&nbsp; "
                f"<b>{esc(e['permission'])}</b> - {esc(e['permission_note'])}</p>"
                f"<p>{esc(e['summary'])}</p>"
                + h("Syntax") + pre(e["syntax"])
                + h("Arguments") + table
                + h("Example") + pre(e["example"])
                + h("Expected output") + pre(e["output"])
                + h("Notes") + f"<ul>{notes}</ul>")

    @staticmethod
    def show_reference(parent, t):
        CommandReferenceDialog(parent, t).exec()


# --------------------------------------------------------------------------
# CUSTOM ANIMATED DIALOG (replaces stock QMessageBox everywhere)
# --------------------------------------------------------------------------
class AnimatedDialog(QDialog):
    """
    A frameless, rounded, glass-matched popup that fades and scales in
    instead of snapping open like a native message box. Used for every
    info/warning/error/confirm in the app so the whole UI feels like
    one designed surface instead of Qt/Windows chrome interrupting it.
    """

    ICONS = {"info": "info", "warn": "reset", "error": "remove", "question": "settings"}
    COLOR_KEYS = {"info": "accent", "warn": "warn", "error": "danger", "question": "secondary"}

    def __init__(self, parent, t, icon_theme, glass, kind, title, message, buttons):
        super().__init__(parent)
        # WindowStaysOnTopHint: a frameless, translucent Qt.Dialog on
        # Windows has been observed opening genuinely behind whatever
        # window currently has focus instead of coming to the front.
        # Because it's still modal, that leaves it silently blocking all
        # input to this app while sitting somewhere the user can't see or
        # find it (it also gets no Alt+Tab entry, being frameless) -
        # indistinguishable from a total freeze.
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        # WA_TranslucentBackground makes this a layered/composited window on
        # Windows - on this machine that compositing has been directly
        # confirmed broken: reading the actual on-screen pixels at this
        # dialog's coordinates while it was open showed nothing but the
        # window behind it, uniformly, everywhere (not just the card - the
        # whole translucent surface). The window exists, is flagged visible,
        # sits at the right position, and is genuinely waiting for a click
        # in its modal loop - it just never composites a single visible
        # pixel, which is indistinguishable from a hang to the user. Falling
        # back to a normal opaque background sidesteps that broken
        # compositing path entirely (square corners instead of rounded, but
        # actually visible) rather than depending on a code-level fix for a
        # system-level rendering failure.
        self.setModal(True)
        self.result_value = None
        color = t[self.COLOR_KEYS.get(kind, "accent")]

        self.setStyleSheet(f"background: {t['bg_panel']};")
        self.setAutoFillBackground(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)

        card = QFrame(self)
        card.setObjectName("dialogCard")
        # Plain opaque background, no QGraphicsDropShadowEffect: this dialog
        # no longer uses WA_TranslucentBackground (see __init__ above - that
        # compositing was confirmed broken on this machine, rendering the
        # whole dialog blank). QGraphicsDropShadowEffect needs the same kind
        # of off-screen compositing pass, and applying it here reproduced
        # the identical symptom one level down: the outer dialog painted
        # fine, but this card - carrying every actual label/button - came
        # out as a flat, blank rectangle. Dropping the shadow effect lets it
        # paint as a normal, uncomposited widget.
        card.setStyleSheet(f"""
            QFrame#dialogCard {{
                background: {t['bg_panel']}; border-radius: 16px;
                border: 1px solid {color};
            }}
        """)
        outer.addWidget(card)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 18)
        card_layout.setSpacing(10)

        head = QHBoxLayout()
        icon_lbl = QLabel()
        icon_lbl.setPixmap(icon(self.ICONS.get(kind, "info"), icon_theme).pixmap(28, 28))
        head.addWidget(icon_lbl)
        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"color: {t['fg']}; font-size: 12.5pt; font-weight: 600;")
        head.addWidget(title_lbl)
        head.addStretch()
        card_layout.addLayout(head)

        msg_lbl = QLabel(message)
        msg_lbl.setWordWrap(True)
        msg_lbl.setStyleSheet(f"color: {t['fg_dim']}; font-size: 10pt;")
        msg_lbl.setMinimumWidth(320)
        card_layout.addWidget(msg_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        for label, value, primary in buttons:
            b = QPushButton(label)
            b.setCursor(Qt.PointingHandCursor)
            if primary:
                b.setStyleSheet(f"""
                    QPushButton {{ background: {color}; color: {contrast_fg(color)};
                        border: none; border-radius: 8px; padding: 8px 18px; font-weight: 600; }}
                    QPushButton:hover {{ background: {t['fg']}; color: {t['bg_panel']}; }}
                """)
            else:
                b.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {t['fg_dim']};
                        border: 1px solid {t['border']}; border-radius: 8px; padding: 8px 18px; }}
                    QPushButton:hover {{ background: {t['bg_card']}; color: {t['fg']}; }}
                """)
            b.clicked.connect(lambda _c=False, v=value: self._finish(v))
            btn_row.addWidget(b)
        card_layout.addLayout(btn_row)

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0)

    def _finish(self, value):
        self.result_value = value
        self._play(reverse=True, on_done=self.accept)

    def _play(self, reverse=False, on_done=None):
        start_geo = self.geometry()
        cx, cy = start_geo.center().x(), start_geo.center().y()
        big = start_geo
        small = QRect(0, 0, int(start_geo.width() * 0.92), int(start_geo.height() * 0.92))
        small.moveCenter(QPoint(cx, cy))

        fade = QPropertyAnimation(self._opacity, b"opacity", self)
        fade.setDuration(180)
        geo = QPropertyAnimation(self, b"geometry", self)
        geo.setDuration(180)
        geo.setEasingCurve(QEasingCurve.OutCubic if not reverse else QEasingCurve.InCubic)

        if reverse:
            fade.setStartValue(1); fade.setEndValue(0)
            geo.setStartValue(big); geo.setEndValue(small)
        else:
            fade.setStartValue(0); fade.setEndValue(1)
            geo.setStartValue(small); geo.setEndValue(big)

        group = QParallelAnimationGroup(self)
        group.addAnimation(fade)
        group.addAnimation(geo)
        if on_done:
            group.finished.connect(on_done)
        group.start(QPropertyAnimation.DeleteWhenStopped)
        self._active_anim = group

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
        w, h = self.sizeHint().width(), self.sizeHint().height()
        parent = self.parent()
        pg = parent.geometry() if parent is not None else None
        # A parent window that hasn't actually been shown/positioned yet
        # (e.g. a dialog fired during __init__, before the caller has
        # called show()) can report a degenerate geometry - zero-sized,
        # or parked at some placeholder origin. Trusting that blindly
        # would put this dialog off-screen: modal (blocking all input)
        # but nowhere the user could ever see or reach it. Fall back to
        # centering on the primary screen instead.
        if pg is not None and pg.width() > 50 and pg.height() > 50:
            center = pg.center()
        else:
            screen = QApplication.primaryScreen()
            center = screen.availableGeometry().center() if screen else QPoint(w, h)
        self.setGeometry(center.x() - w // 2, center.y() - h // 2, w, h)
        QTimer.singleShot(0, lambda: self._play(reverse=False))
        # Safety net: this dialog starts at opacity 0 and is only ever made
        # visible by the fade-in QPropertyAnimation above completing. That
        # animation has been observed to silently never run/finish in some
        # environments (seen in testing: the dialog's HWND is created and
        # reports itself visible to Windows, but nothing is ever painted -
        # a real screen capture showed straight through to the window
        # behind it). Since this is a modal dialog, that leaves it stuck
        # fully blocking input while being genuinely invisible - indistin-
        # guishable from a hang to the user, with no exception or crash to
        # diagnose. Force full opacity/geometry after a deadline well past
        # the animation's own 180ms duration so the dialog can never be
        # permanently invisible-yet-modal, regardless of why the animation
        # didn't complete.
        QTimer.singleShot(400, self._ensure_visible)

    def _ensure_visible(self):
        if self._opacity.opacity() < 1:
            self._opacity.setOpacity(1)
            w, h = self.sizeHint().width(), self.sizeHint().height()
            parent = self.parent()
            pg = parent.geometry() if parent is not None else None
            if pg is not None and pg.width() > 50 and pg.height() > 50:
                center = pg.center()
            else:
                screen = QApplication.primaryScreen()
                center = screen.availableGeometry().center() if screen else QPoint(w, h)
            self.setGeometry(center.x() - w // 2, center.y() - h // 2, w, h)

    @staticmethod
    def show(parent, t, icon_theme, glass, kind, title, message, buttons=None):
        if buttons is None:
            buttons = [("OK", True, True)]
        dlg = AnimatedDialog(parent, t, icon_theme, glass, kind, title, message, buttons)

        # Most callers of this dialog are a toolbar button's `clicked` slot,
        # so we're still inside that button's own mouse press/release
        # handling when this runs. On Windows that button can be left
        # holding an implicit mouse grab from its own click cycle; if this
        # new dialog opens while that grab is still held, real mouse clicks
        # keep being routed to the old button/window instead of this dialog
        # - confirmed directly: the dialog rendered and accepted keyboard
        # input (Tab+Enter worked) but ignored real mouse clicks entirely,
        # the exact signature of a stale grab rather than a rendering or
        # focus problem. Releasing whatever currently holds the mouse
        # before showing this dialog closes that gap.
        grabber = QWidget.mouseGrabber()
        if grabber is not None:
            grabber.releaseMouse()

        # The main window has OLE drag-and-drop enabled (setAcceptDrops),
        # which on Windows means any other process poking this window's
        # UI Automation/accessibility tree while a modal nested loop is
        # running below (dlg.exec()) can hit RPC_E_CANTCALLOUT_ININPUTSYNCCALL
        # (0x8001010d) - a native OS-level fault, not a Python exception,
        # so it can't be caught here and has been seen wedging the whole
        # UI thread mid-dialog. Dropping drag-accept for just the exec()
        # call shrinks that window to zero for the one thing we control;
        # it can't stop every external cause, but it removes this app's
        # own contribution to the conflict.
        accepts_drops = parent is not None and parent.acceptDrops()
        if accepts_drops:
            parent.setAcceptDrops(False)
        try:
            dlg.exec()
        finally:
            if accepts_drops:
                parent.setAcceptDrops(True)
        return dlg.result_value


# --------------------------------------------------------------------------
# SETTINGS PANEL (slide-down)
# --------------------------------------------------------------------------
class SettingsPanel(QFrame):
    themeChanged = Signal(str)
    glassChanged = Signal(bool)
    hideUnsupportedChanged = Signal(bool)
    accentRequested = Signal()
    shortcutsRequested = Signal()

    def __init__(self, parent, theme, glass_enabled, shortcut_keys, hide_unsupported=False):
        super().__init__(parent)
        self.setObjectName("panel")
        self._max_height = 180

        outer_v = QVBoxLayout(self)
        outer_v.setContentsMargins(18, 12, 18, 12)
        outer_v.setSpacing(8)

        title = QLabel("SETTINGS")
        title.setObjectName("sectionLabel")
        outer_v.addWidget(title)

        row1 = QHBoxLayout()
        row1.setSpacing(28)

        col1 = QVBoxLayout()
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Theme"))
        self.theme_switch = GlassSwitch(self, checked=(theme == "light"),
                                         off_color="#232840", on_color="#ffd166")
        self.theme_switch.setToolTip("Switch between dark and light theme")
        self.theme_switch.toggled.connect(lambda checked: self.themeChanged.emit("light" if checked else "dark"))
        theme_row.addWidget(self.theme_switch)
        self.mode_lbl = QLabel("Dark mode" if theme == "dark" else "Light mode")
        self.mode_lbl.setStyleSheet("font-weight: 600;")
        theme_row.addWidget(self.mode_lbl)
        theme_row.addStretch()
        col1.addLayout(theme_row)
        row1.addLayout(col1)

        col2 = QVBoxLayout()
        glass_row = QHBoxLayout()
        glass_row.addWidget(QLabel("Glass Effect"))
        self.glass_switch = GlassSwitch(self, checked=glass_enabled,
                                         off_color="#3a3f55", on_color="#4fd1ff")
        self.glass_switch.setToolTip("Turn the frosted-glass look on or off")
        self.glass_switch.toggled.connect(self.glassChanged.emit)
        glass_row.addWidget(self.glass_switch)
        self.glass_lbl = QLabel("On" if glass_enabled else "Off")
        self.glass_lbl.setStyleSheet("font-weight: 600;")
        glass_row.addWidget(self.glass_lbl)
        glass_row.addStretch()
        col2.addLayout(glass_row)
        row1.addLayout(col2)

        col3 = QVBoxLayout()
        self.btn_accent = QPushButton("  Accent Color")
        self.btn_accent.setCursor(Qt.PointingHandCursor)
        self.btn_accent.setToolTip("Pick a custom accent color for buttons and highlights")
        self.btn_accent.clicked.connect(self.accentRequested.emit)
        col3.addWidget(self.btn_accent)
        row1.addLayout(col3)

        row1.addStretch()
        outer_v.addLayout(row1)

        explorer_row = QHBoxLayout()
        self.hide_unsupported_chk = GlassCheckbox("Hide unsupported files in Solution Explorer",
                                                    accent=THEMES[theme]["accent"], fg=THEMES[theme]["fg"])
        self.hide_unsupported_chk.setChecked(hide_unsupported)
        self.hide_unsupported_chk.setToolTip(
            "Hide files the search engine can't read (binaries, unknown encodings) from the file list")
        self.hide_unsupported_chk.toggled.connect(self.hideUnsupportedChanged.emit)
        explorer_row.addWidget(self.hide_unsupported_chk)
        explorer_row.addStretch()
        outer_v.addLayout(explorer_row)

        row2 = QHBoxLayout()
        row2.setSpacing(12)
        sc_title = QLabel("Shortcuts:")
        sc_title.setStyleSheet("font-weight: 600;")
        row2.addWidget(sc_title)
        self.shortcuts_lbl = QLabel()
        self.shortcuts_lbl.setObjectName("summary")
        self.shortcuts_lbl.setWordWrap(True)
        row2.addWidget(self.shortcuts_lbl, 1)
        self.btn_shortcuts = QPushButton("  Edit Shortcuts")
        self.btn_shortcuts.setCursor(Qt.PointingHandCursor)
        self.btn_shortcuts.setToolTip("View and rebind every keyboard shortcut")
        self.btn_shortcuts.clicked.connect(self.shortcutsRequested.emit)
        row2.addWidget(self.btn_shortcuts)
        outer_v.addLayout(row2)
        self.set_shortcuts_summary(shortcut_keys)

        self.setMaximumHeight(0)
        self.setMinimumHeight(0)
        self._anim = QPropertyAnimation(self, b"maximumHeight", self)
        self._anim.setDuration(220)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._visible = False

    def set_theme_label(self, theme):
        self.mode_lbl.setText("Dark mode" if theme == "dark" else "Light mode")

    def set_glass_label(self, glass_enabled):
        self.glass_lbl.setText("On" if glass_enabled else "Off")

    def set_shortcuts_summary(self, shortcut_keys):
        parts = [f"{label} · {shortcut_keys.get(key, default)}"
                 for key, label, default in SHORTCUT_ACTIONS]
        self.shortcuts_lbl.setText("   |   ".join(parts))

    def set_accent_button_style(self, accent_hex, dim, panel_bg, fg=None):
        self.btn_accent.setIcon(QIcon())
        self.btn_accent.setStyleSheet(f"""
            QPushButton {{ background: {accent_hex}; color: {contrast_fg(accent_hex)};
                border: none; border-radius: 8px; padding: 7px 14px; font-weight: 600; }}
            QPushButton:hover {{ background: {dim}; }}
        """)
        self.btn_shortcuts.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {accent_hex};
                border: 1px solid {accent_hex}; border-radius: 8px; padding: 6px 12px; font-weight: 600; }}
            QPushButton:hover {{ background: {accent_hex}; color: {contrast_fg(accent_hex)}; }}
        """)
        self.hide_unsupported_chk.set_colors(accent_hex, fg if fg is not None else contrast_fg(panel_bg))

    def toggle(self):
        self._visible = not self._visible
        self._anim.stop()
        self._anim.setStartValue(self.maximumHeight())
        self._anim.setEndValue(self._max_height if self._visible else 0)
        self._anim.start()
