"""
main_window.py - FinderWindow, CodeLens's main application window, plus
the small background-worker helper it uses for off-UI-thread work.
"""

import datetime
import gc
import os
import re
import subprocess
import sys
import threading
import traceback

from PySide6.QtCore import (
    Qt, QTimer, QThread, QObject, QPropertyAnimation, QEasingCurve, QRect, QRectF, QPoint, QPointF, QSize,
    Signal, QSequentialAnimationGroup, Property, QParallelAnimationGroup, QFileSystemWatcher, QFileInfo,
)
from PySide6.QtGui import (
    QIcon, QPainter, QColor, QPen, QPixmap, QLinearGradient, QShortcut, QKeySequence, QFont,
    QFontMetrics,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QToolButton, QListWidget, QListWidgetItem, QLineEdit,
    QTextEdit, QPlainTextEdit, QFileDialog, QSplitter, QFrame, QCheckBox,
    QRadioButton, QButtonGroup, QDialog, QGraphicsOpacityEffect,
    QGraphicsDropShadowEffect, QAbstractButton, QStackedWidget, QKeySequenceEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QMessageBox,
    QProgressBar, QMenu, QFileIconProvider, QWidgetAction,
)

import CodeLensCLI as core
from theme import (
    APP_ICON_PATH, ACCENT_ICON_NAMES, THEMES, FONT_MONO,
    icon, real_file_icon, tinted_icon, with_alpha, contrast_fg, build_stylesheet,
)
from widgets import (
    GlassBackdrop, GlassCheckbox, HoverToolButton, HoverRadioButton, TreeGuideRow, ElidedLabel,
    make_tool_button,
)
from dialogs import AccentColorPicker, ShortcutsDialog, PreviewDialog, FileDetailsDialog, AnimatedDialog, SettingsPanel
from persistence import (
    SHORTCUT_ACTIONS, load_shortcuts, save_shortcuts,
    load_recent_folders, save_recent_folders, remember_recent_folder,
    load_view_settings, save_view_setting,
)
from results_render import _html_render_file_card

APP_NAME = "CodeLens"
APP_TAGLINE = "Precision line & symbol search"


class _AsyncWorker(QObject):
    """Runs one callable on a background QThread so slow disk/regex work
    (loading many/large files, reloading, scanning big searches) can't
    freeze the Qt event loop and trigger Windows' "Not Responding" state.

    When wants_progress=True, `fn` is called as fn(progress_cb,
    should_cancel) instead of fn() - progress_cb(phase, current, total)
    re-emits as the `progress` signal (Qt marshals it back to the UI
    thread automatically, the same way finished/failed already are),
    and should_cancel() reports whether cancel() has been called. This
    is a plain threading.Event rather than anything Qt-specific because
    it's polled from inside `fn` while it's running on the worker
    thread - a bound Python callable that only ever reads one flag is
    simpler and cheaper here than routing a second signal back down to
    the worker."""

    finished = Signal(object)
    failed = Signal(str)
    progress = Signal(str, int, int)

    def __init__(self, fn, wants_progress=False):
        super().__init__()
        self._fn = fn
        self._wants_progress = wants_progress
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancel_event.set()

    def is_cancelled(self):
        return self._cancel_event.is_set()

    def run(self):
        try:
            if self._wants_progress:
                result = self._fn(self.progress.emit, self.is_cancelled)
            else:
                result = self._fn()
        except Exception:
            self.failed.emit(traceback.format_exc())
        else:
            self.finished.emit(result)


class FinderWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.state = core.FileState()
        self.theme = "dark"
        self.glass_enabled = True
        self.custom_accent = None  # None = use theme's default accent
        self.view_mode = "context"
        self.hide_unsupported = load_view_settings()["hide_unsupported"]
        self._active_extensions = None  # None = no filter, show every extension
        self._collapsed_groups = set()  # relative folder-path tuples collapsed in Group-by-folder view
        self._path_row_labels = []  # (item, ElidedLabel, TreeGuideRow) triples for the "path"
                                     # view's file rows, so selection can be re-applied on every change
        self.explorer_visible = True
        self._explorer_saved_width = 300
        self.results_zoom_steps = 0
        self.shortcut_keys = load_shortcuts()
        self._busy_threads = []  # keep QThread/worker refs alive until finished
        self._results_buffer = []

        # Live solution-explorer sync: watches every folder a set of
        # files was loaded from (plus each loaded file itself) and
        # re-syncs the list when something changes on disk - files
        # added/removed/edited outside the app show up without a manual
        # Reload, the way VS Code's Explorer does.
        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(self._on_fs_event)
        self._fs_watcher.fileChanged.connect(self._on_fs_event)
        self._fs_debounce = QTimer(self)
        self._fs_debounce.setSingleShot(True)
        self._fs_debounce.timeout.connect(self._do_live_rescan)
        self._live_rescanning = False

        self.setWindowTitle(f"{APP_NAME} - {APP_TAGLINE}")
        self.resize(1220, 780)
        self.setMinimumSize(880, 560)
        self.setAcceptDrops(True)

        self._tool_buttons = []

        self._build_ui()
        self._install_shortcuts()
        self._refresh_tooltips()
        self._apply_theme(animate=False)
        self._refresh_file_list()
        self._set_status("Ready - press Alt+A or click Add to load a file.", "ok")

        startup_paths = [p for p in sys.argv[1:] if os.path.isfile(p) or os.path.isdir(p)]
        if startup_paths:
            # Deferred rather than called directly: this runs from inside
            # __init__, before main() has called win.show() and before the
            # window's real on-screen geometry exists. If the load hits an
            # error and _report_add_result pops an AnimatedDialog, that
            # dialog's showEvent positions itself off the (not-yet-valid)
            # parent geometry - the same "modal but never actually visible"
            # failure mode as two AnimatedDialogs firing back-to-back (see
            # on_change). Deferring to the next event loop iteration lets
            # the window finish showing first.
            QTimer.singleShot(0, lambda: self._load_startup_paths(startup_paths))

    def _load_startup_paths(self, paths):
        """Loads file(s)/folder(s) passed on the command line, e.g. from
        the File Explorer 'Open with CodeLens' / 'Open folder with
        CodeLens' context menu entries. Folders are expanded into every
        text-based file under them."""
        self._remember_folders(paths)
        self._cancel_pending_loads()

        def work(progress_cb, should_cancel):
            return core.add_paths_to_state(paths, self.state, progress_cb=progress_cb, should_cancel=should_cancel)

        def done(result):
            self._report_add_result(result, "startup")

        self._run_busy(work, done, "Loading file(s)...", cancellable=True)

    # ------------------------------------------------------------- UI --
    def _build_ui(self):
        self.backdrop = GlassBackdrop(self)
        self.setCentralWidget(self.backdrop)

        root = QWidget(self.backdrop)
        root.setObjectName("root")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(14, 12, 14, 10)
        outer.setSpacing(8)

        def relayout_root():
            root.setGeometry(self.backdrop.rect())
        self.backdrop.resizeEvent = lambda e: relayout_root()

        # ---- brand row ---------------------------------------------------
        brand_row = QHBoxLayout()
        brand_icon = QLabel()
        brand_icon.setObjectName("brandIcon")
        self._brand_icon_lbl = brand_icon
        brand_row.addWidget(brand_icon)
        brand_col = QVBoxLayout()
        brand_col.setSpacing(0)
        name_lbl = QLabel(APP_NAME)
        name_lbl.setObjectName("brand")
        tag_lbl = QLabel(APP_TAGLINE)
        tag_lbl.setObjectName("brandTag")
        brand_col.addWidget(name_lbl)
        brand_col.addWidget(tag_lbl)
        brand_row.addLayout(brand_col)
        brand_row.addStretch()
        outer.addLayout(brand_row)

        # ---- toolbar ---------------------------------------------------
        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)

        self.btn_explorer = make_tool_button("", "menu", self.on_toggle_explorer,
                                              tooltip="Hide/show the file list panel")
        self.btn_explorer.setObjectName("viewToggle")
        self.btn_explorer.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.btn_explorer.setFixedSize(34, 34)
        toolbar.addWidget(self.btn_explorer)
        self._tool_buttons.append(self.btn_explorer)

        self.btn_add = make_tool_button("Add", "add", self.on_add,
                                         tooltip="Add file(s) to the working set")
        self.btn_remove = make_tool_button("Remove", "remove", self.on_remove, "danger",
                                            tooltip="Remove the selected file(s) from the working set")
        self.btn_change = make_tool_button("New Set", "change", self.on_change,
                                            tooltip="Clear everything and start a fresh file set")
        self.btn_reload = make_tool_button("Reload", "reload", self.on_reload,
                                            tooltip="Reload every loaded file from disk")
        self.btn_reset = make_tool_button("Reset", "reset", self.on_reset, "warn",
                                           tooltip="Reset search state, or clear everything")
        self.btn_clear = make_tool_button("Clear Results", "clear", self.on_clear_results,
                                           tooltip="Clear the results pane")
        self.btn_details = make_tool_button("Details", "details", self.on_show_details,
                                             tooltip="View full details for every loaded file")
        self.btn_recent = make_tool_button("Recent", "lines", self.on_show_recent,
                                            tooltip="Reopen a recently used folder")
        for b in (self.btn_add, self.btn_remove, self.btn_change, self.btn_recent, self.btn_reload,
                  self.btn_reset, self.btn_clear, self.btn_details):
            toolbar.addWidget(b)
            self._tool_buttons.append(b)
        toolbar.addStretch()

        self.btn_settings = make_tool_button("Settings", "settings", self.on_toggle_settings,
                                              tooltip="Open theme, glass, accent, and shortcut settings")
        toolbar.addWidget(self.btn_settings)
        self._tool_buttons.append(self.btn_settings)

        outer.addLayout(toolbar)

        # ---- settings (collapsible) ------------------------------------
        self.settings_panel = SettingsPanel(root, self.theme, self.glass_enabled, self.shortcut_keys,
                                             self.hide_unsupported)
        self.settings_panel.themeChanged.connect(self.set_theme)
        self.settings_panel.glassChanged.connect(self.set_glass)
        self.settings_panel.hideUnsupportedChanged.connect(self.set_hide_unsupported)
        self.settings_panel.accentRequested.connect(self.on_pick_accent)
        self.settings_panel.shortcutsRequested.connect(self.on_edit_shortcuts)
        self.btn_accent = self.settings_panel.btn_accent
        outer.addWidget(self.settings_panel)

        # ---- body: crossfade container ---------------------------------
        self.fade_widget = QWidget()
        self.fade_effect = QGraphicsOpacityEffect(self.fade_widget)
        self.fade_widget.setGraphicsEffect(self.fade_effect)
        self.fade_effect.setOpacity(1.0)
        body_outer = QVBoxLayout(self.fade_widget)
        body_outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.fade_widget, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        splitter = self.splitter
        splitter.setHandleWidth(8)
        splitter.setStyleSheet("QSplitter::handle { background: transparent; }")
        body_outer.addWidget(splitter)

        # left: file list
        left = QFrame()
        self.left_panel = left
        left.setObjectName("panel")
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(14, 14, 14, 14)

        header_row = QHBoxLayout()
        lbl = QLabel("LOADED FILES")
        lbl.setObjectName("sectionLabel")
        header_row.addWidget(lbl)
        header_row.addStretch()

        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        self.view_buttons = {}
        for value, icon_name, tip in (
            ("context", "lines", "Detail list view"),
            ("grid", "grid", "Grid view"),
            ("path", "folder", "Group by folder"),
        ):
            vb = HoverToolButton()
            vb.setObjectName("viewToggle")
            vb.setCheckable(True)
            vb.setToolButtonStyle(Qt.ToolButtonIconOnly)
            vb.setIconSize(QSize(15, 15))
            vb.setFixedSize(28, 28)
            vb.setCursor(Qt.PointingHandCursor)
            vb.setToolTip(tip)
            vb._icon_name = icon_name
            self.view_group.addButton(vb)
            self.view_buttons[value] = vb
            self._tool_buttons.append(vb)
            header_row.addWidget(vb)
        self.view_buttons[self.view_mode].setChecked(True)
        self.view_group.buttonToggled.connect(self._on_view_mode_change)

        self.filter_btn = HoverToolButton()
        self.filter_btn.setObjectName("viewToggle")
        self.filter_btn.setText(" Filter")
        self.filter_btn.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.filter_btn.setIconSize(QSize(15, 15))
        self.filter_btn.setFixedHeight(28)
        self.filter_btn.setCursor(Qt.PointingHandCursor)
        self.filter_btn.setToolTip("Filter the file list by extension")
        self.filter_btn._icon_name = "filter"
        self.filter_btn.setPopupMode(QToolButton.InstantPopup)
        self._tool_buttons.append(self.filter_btn)
        header_row.addWidget(self.filter_btn)

        self.filter_menu = QMenu(self)
        self.filter_btn.setMenu(self.filter_menu)
        self.filter_menu.aboutToShow.connect(self._populate_filter_menu)

        left_layout.addLayout(header_row)

        # ---- background folder-load progress (hidden until a scan/load
        # of a large directory is actually running) --------------------
        self.load_progress_row = QFrame()
        lp_layout = QHBoxLayout(self.load_progress_row)
        lp_layout.setContentsMargins(0, 0, 0, 8)
        lp_layout.setSpacing(8)
        self.load_progress_label = QLabel("")
        self.load_progress_label.setObjectName("summary")
        self.load_progress_bar = QProgressBar()
        self.load_progress_bar.setObjectName("loadProgress")
        self.load_progress_bar.setFixedHeight(6)
        self.load_progress_bar.setTextVisible(False)
        self.load_progress_bar.setRange(0, 0)  # indeterminate until a real total is known
        self.load_progress_cancel_btn = make_tool_button(
            "Cancel", "remove", self._on_cancel_load,
            tooltip="Stop the current folder scan/load - files already loaded are kept")
        self.load_progress_cancel_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        lp_layout.addWidget(self.load_progress_label, 1)
        lp_layout.addWidget(self.load_progress_bar, 2)
        lp_layout.addWidget(self.load_progress_cancel_btn)
        self.load_progress_row.hide()
        left_layout.addWidget(self.load_progress_row)

        self.file_list = QListWidget()
        self.file_list.setObjectName("fileList")
        self.file_list.setToolTip(
            "Files currently loaded for searching - double-click to preview, "
            "select to scope a line-range search. Drag & drop files here to add them."
        )
        self.file_list.itemDoubleClicked.connect(self._on_file_double_clicked)
        self.file_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self._on_file_list_context_menu)
        self.file_list.itemSelectionChanged.connect(self._update_path_selection_colors)
        left_layout.addWidget(self.file_list, 1)
        self.file_summary = QLabel("0 files loaded")
        self.file_summary.setObjectName("summary")
        left_layout.addWidget(self.file_summary)
        splitter.addWidget(left)
        self._glass_cards = [left]

        # right: search + results - a vertical splitter so the search
        # box and results box can be resized against each other, the
        # same "drag the divider" interaction as the file-explorer
        # splitter on the left.
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        right_splitter = QSplitter(Qt.Vertical)
        right_splitter.setHandleWidth(8)
        right_splitter.setStyleSheet("QSplitter::handle { background: transparent; }")
        right_layout.addWidget(right_splitter)

        search_card = QFrame()
        search_card.setObjectName("card")
        sc_layout = QVBoxLayout(search_card)
        sc_layout.setContentsMargins(16, 14, 16, 14)
        self._glass_cards.append(search_card)

        mode_row = QHBoxLayout()
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.mode_buttons = {}
        for value, icon_name, label, tip in (
            ("one", "search", "One Line", "Literal match on a single line (whitespace-insensitive)"),
            ("multi", "multi", "Multi Line", "Whitespace/blank-line-insensitive snippet match"),
            ("range", "lines", "Line Range", "Show a specific range of lines from one file"),
        ):
            rb = HoverRadioButton(" " + label)
            rb.setCursor(Qt.PointingHandCursor)
            rb.setToolTip(tip)
            rb._icon_name = icon_name
            self.mode_group.addButton(rb)
            self.mode_buttons[value] = rb
            mode_row.addWidget(rb)
        self.mode_buttons["one"].setChecked(True)
        self.mode_group.buttonToggled.connect(self._on_mode_change)
        mode_row.addStretch()

        self.case_chk = GlassCheckbox("Case sensitive")
        mode_row.addWidget(self.case_chk)
        sc_layout.addLayout(mode_row)

        input_row = QHBoxLayout()
        self.query_entry = QLineEdit()
        self.query_entry.setPlaceholderText("Type exact text to search for...")
        self.query_entry.returnPressed.connect(self.on_search)

        self.query_multiline = QPlainTextEdit()
        self.query_multiline.setPlaceholderText("Paste or type a multi-line snippet...")
        self.query_multiline.setMinimumHeight(84)

        self.range_entry = QLineEdit()
        self.range_entry.setPlaceholderText("e.g. 1500-3000")
        self.range_entry.returnPressed.connect(self.on_search)

        self.search_btn = make_tool_button("Search", "search", self.on_search,
                                            tooltip="Run the search")
        self.search_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.search_btn.setText("Search")

        self.input_stack = QStackedWidget()
        self.input_stack.addWidget(self.query_entry)
        self.input_stack.addWidget(self.query_multiline)
        self.input_stack.addWidget(self.range_entry)
        # QStackedWidget's own sizeHint/minimumSizeHint is the max over all
        # pages, so without this it stays stuck at the multiline page's
        # height even while showing a single-line entry. Computed from font
        # metrics (+ the QSS's own 8px padding/1px border on each edge)
        # rather than query_entry.sizeHint() - at this point in _build_ui
        # the stylesheet hasn't been applied yet (_apply_theme runs after),
        # so sizeHint() here reflects an unstyled QLineEdit and undersizes
        # the box once the real padding/border kick in.
        self._one_line_height = self.query_entry.fontMetrics().height() + 2 * (8 + 1) + 4
        self.input_stack.setFixedHeight(self._one_line_height)
        input_row.addWidget(self.input_stack, 1)
        input_row.addWidget(self.search_btn)
        sc_layout.addLayout(input_row)

        right_splitter.addWidget(search_card)

        results_card = QFrame()
        results_card.setObjectName("card")
        rc_layout = QVBoxLayout(results_card)
        rc_layout.setContentsMargins(16, 12, 16, 12)
        self._glass_cards.append(results_card)

        rc_header = QHBoxLayout()
        rlbl = QLabel("RESULTS")
        rlbl.setObjectName("sectionLabel")
        rc_header.addWidget(rlbl)
        rc_header.addStretch()
        self.results_summary = QLabel("")
        self.results_summary.setObjectName("summary")
        rc_header.addWidget(self.results_summary)

        rc_header.addSpacing(14)
        self.btn_zoom_out = make_tool_button("", "zoomout", self.on_zoom_out,
                                              tooltip="Zoom out results  (Ctrl+-)")
        self.btn_zoom_out.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.btn_zoom_out.setFixedSize(28, 28)
        self.zoom_lbl = QLabel("100%")
        self.zoom_lbl.setObjectName("summary")
        self.zoom_lbl.setFixedWidth(38)
        self.zoom_lbl.setAlignment(Qt.AlignCenter)
        self.btn_zoom_in = make_tool_button("", "zoomin", self.on_zoom_in,
                                             tooltip="Zoom in results  (Ctrl++)")
        self.btn_zoom_in.setToolButtonStyle(Qt.ToolButtonIconOnly)
        self.btn_zoom_in.setFixedSize(28, 28)
        for b in (self.btn_zoom_out, self.btn_zoom_in):
            b.setObjectName("viewToggle")
            self._tool_buttons.append(b)
        rc_header.addWidget(self.btn_zoom_out)
        rc_header.addWidget(self.zoom_lbl)
        rc_header.addWidget(self.btn_zoom_in)
        rc_layout.addLayout(rc_header)

        self.results = QTextEdit()
        self.results.setObjectName("results")
        self.results.setReadOnly(True)
        self.results.setLineWrapMode(QTextEdit.NoWrap)
        rc_layout.addWidget(self.results, 1)

        right_splitter.addWidget(results_card)
        right_splitter.setStretchFactor(0, 0)
        right_splitter.setStretchFactor(1, 1)
        right_splitter.setSizes([180, 560])

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 900])

        # ---- status bar --------------------------------------------------
        self.status = QLabel("")
        self.status.setObjectName("statusBar")
        outer.addWidget(self.status)

        relayout_root()

    # ---------------------------------------------------------- theming --
    def _effective_theme(self):
        t = dict(THEMES[self.theme])
        if self.custom_accent:
            t["accent"] = self.custom_accent
            t["accent_fg"] = contrast_fg(self.custom_accent)
        return t

    def _apply_theme(self, animate=True):
        def swap():
            t = self._effective_theme()
            self.setStyleSheet(build_stylesheet(t, self.glass_enabled))
            self.backdrop.set_theme(THEMES[self.theme], t["accent"], self.glass_enabled)

            hover_icon_cache = {}

            def hover_icon(name):
                if name not in hover_icon_cache:
                    hover_icon_cache[name] = icon(name, "hover")
                return hover_icon_cache[name]

            for b in self._tool_buttons:
                if b._icon_name in ACCENT_ICON_NAMES and self.custom_accent:
                    normal = tinted_icon(b._icon_name, self.theme, self.custom_accent)
                else:
                    normal = icon(b._icon_name, self.theme)
                b.set_icons(normal, hover_icon(b._icon_name))
            for rb in self.mode_buttons.values():
                if rb._icon_name in ACCENT_ICON_NAMES and self.custom_accent:
                    normal = tinted_icon(rb._icon_name, self.theme, self.custom_accent)
                else:
                    normal = icon(rb._icon_name, self.theme)
                rb.set_icons(normal, hover_icon(rb._icon_name))

            self.settings_panel.set_theme_label(self.theme)
            self.settings_panel.set_glass_label(self.glass_enabled)
            self.settings_panel.set_accent_button_style(t["accent"], t["fg_dim"], t["bg_panel"], t["fg"])
            self.case_chk.set_colors(t["accent"], t["fg"])
            self._refresh_file_list()

            # NOTE: deliberately no QGraphicsDropShadowEffect on these
            # cards - Qt renders a translucent-QSS QFrame blank when a
            # drop-shadow effect sits on it while it also hosts child
            # widgets (splitter panel / list / text edit). The glass
            # look instead comes from the translucent background plus
            # the soft highlight border in build_stylesheet().

            self._brand_icon_lbl.setPixmap(QIcon(APP_ICON_PATH).pixmap(34, 34))
            try:
                self.setWindowIcon(QIcon(APP_ICON_PATH))
            except Exception:
                pass

        if not animate:
            swap()
            return

        fade_out = QPropertyAnimation(self.fade_effect, b"opacity", self)
        fade_out.setDuration(140)
        fade_out.setStartValue(1.0)
        fade_out.setEndValue(0.2)
        fade_out.setEasingCurve(QEasingCurve.InCubic)

        fade_in = QPropertyAnimation(self.fade_effect, b"opacity", self)
        fade_in.setDuration(200)
        fade_in.setStartValue(0.2)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.OutCubic)

        seq = QSequentialAnimationGroup(self)
        seq.addAnimation(fade_out)
        seq.addAnimation(fade_in)
        fade_out.finished.connect(swap)
        seq.start(QPropertyAnimation.DeleteWhenStopped)
        self._theme_anim = seq

    def set_theme(self, theme):
        if theme == self.theme:
            return
        self.theme = theme
        self._apply_theme(animate=True)
        self._set_status(f"Switched to {theme} mode.", "ok")

    def set_glass(self, enabled):
        if enabled == self.glass_enabled:
            return
        self.glass_enabled = enabled
        self._apply_theme(animate=True)
        self._set_status(f"Glass effect turned {'on' if enabled else 'off'}.", "ok")

    def set_hide_unsupported(self, enabled):
        if enabled == self.hide_unsupported:
            return
        self.hide_unsupported = enabled
        save_view_setting("hide_unsupported", enabled)
        self._refresh_file_list()
        self._set_status(
            f"Unsupported files {'hidden' if enabled else 'shown'} in Solution Explorer.", "ok")

    def on_pick_accent(self):
        current = self.custom_accent or THEMES[self.theme]["accent"]
        chosen = AccentColorPicker.pick(self, self._effective_theme(), current)
        if chosen:
            self.custom_accent = chosen
            self._apply_theme(animate=True)
            self._set_status(f"Accent color set to {chosen}.", "ok")

    def on_toggle_settings(self):
        self.settings_panel.toggle()

    def on_toggle_explorer(self):
        self.explorer_visible = not self.explorer_visible
        if self.explorer_visible:
            self.left_panel.show()
            sizes = self.splitter.sizes()
            total = sum(sizes) or 1
            self.splitter.setSizes([max(240, self._explorer_saved_width), total - max(240, self._explorer_saved_width)])
        else:
            sizes = self.splitter.sizes()
            self._explorer_saved_width = sizes[0] if sizes and sizes[0] > 0 else 300
            self.left_panel.hide()

    # ---------------------------------------------------------- shortcuts --
    def _install_shortcuts(self):
        handlers = {
            "search": self.on_search,
            "add": self.on_add,
            "reload": self.on_reload,
            "reset": self.on_reset,
            "clear_results": self.on_clear_results,
            "case_sensitive": lambda: self.case_chk.setChecked(not self.case_chk.isChecked()),
        }
        self._shortcut_objs = {}
        for key, _label, default in SHORTCUT_ACTIONS:
            sc = QShortcut(QKeySequence(self.shortcut_keys.get(key, default)), self)
            sc.activated.connect(handlers[key])
            self._shortcut_objs[key] = sc
        # Numpad Enter sends a different key than the main Return key in
        # Qt, so Ctrl+Enter needs its own (non-editable) binding to the
        # same search action alongside the editable "search" shortcut.
        numpad = QShortcut(QKeySequence("Ctrl+Enter"), self)
        numpad.activated.connect(self.on_search)
        self._shortcut_objs["search_numpad"] = numpad

        # Results zoom - fixed, not part of the editable shortcut list.
        for seq in ("Ctrl+=", "Ctrl++"):
            sc = QShortcut(QKeySequence(seq), self)
            sc.activated.connect(self.on_zoom_in)
        QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(self.on_zoom_out)
        QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(self.on_zoom_reset)

        # Search mode switching - fixed, not part of the editable shortcut list.
        QShortcut(QKeySequence("Ctrl+1"), self).activated.connect(lambda: self._set_search_mode("one"))
        QShortcut(QKeySequence("Ctrl+2"), self).activated.connect(lambda: self._set_search_mode("multi"))
        QShortcut(QKeySequence("Ctrl+3"), self).activated.connect(lambda: self._set_search_mode("range"))

    def _set_search_mode(self, value):
        self.mode_buttons[value].setChecked(True)

    def _apply_shortcut(self, key, sequence_text):
        self.shortcut_keys[key] = sequence_text
        if key in self._shortcut_objs:
            self._shortcut_objs[key].setKey(QKeySequence(sequence_text))
        save_shortcuts(self.shortcut_keys)

    def _refresh_tooltips(self):
        keys = self.shortcut_keys
        self.btn_add.setToolTip(f"Add file(s) to the working set  ({keys['add']})")
        self.btn_reload.setToolTip(f"Reload every loaded file from disk  ({keys['reload']})")
        self.btn_reset.setToolTip(f"Reset search state, or clear everything  ({keys['reset']})")
        self.btn_clear.setToolTip(f"Clear the results pane  ({keys['clear_results']})")
        self.search_btn.setToolTip(f"Run the search  ({keys['search']})")
        self.case_chk.setToolTip(f"Toggle case-sensitive matching  ({keys['case_sensitive']})")

    def on_edit_shortcuts(self):
        result = ShortcutsDialog.edit(self, self._effective_theme(), self.glass_enabled, self.shortcut_keys)
        if result:
            for key, seq in result.items():
                self._apply_shortcut(key, seq)
            self._refresh_tooltips()
            self.settings_panel.set_shortcuts_summary(self.shortcut_keys)
            self._set_status("Shortcuts updated.", "ok")

    # ---------------------------------------------------------- status --
    def _set_status(self, text, kind="dim"):
        t = self._effective_theme()
        color = {"ok": t["ok"], "warn": t["warn"], "error": t["danger"], "dim": t["fg_dim"]}.get(kind, t["fg_dim"])
        self.status.setText(text)
        panel_bg = with_alpha(t["bg_panel"], 130) if self.glass_enabled else t["bg_panel"]
        self.status.setStyleSheet(f"color: {color}; background: {panel_bg}; padding: 6px 12px; border-radius: 8px;")

    # ---------------------------------------------------------- filter --
    def _entry_extension(self, entry):
        return os.path.splitext(entry.path)[1].lower() or "(no extension)"

    def _entry_passes_filter(self, entry):
        return self._active_extensions is None or self._entry_extension(entry) in self._active_extensions

    def _populate_filter_menu(self):
        self.filter_menu.clear()
        exts = sorted({self._entry_extension(e) for e in self.state.files})
        all_action = self.filter_menu.addAction(f"All file types ({len(self.state.files)})")
        all_action.setCheckable(True)
        all_action.setChecked(self._active_extensions is None)
        all_action.triggered.connect(self._clear_extension_filter)
        self.filter_menu.addSeparator()
        if not exts:
            empty = self.filter_menu.addAction("No files loaded")
            empty.setEnabled(False)
            return
        for ext in exts:
            count = sum(1 for e in self.state.files if self._entry_extension(e) == ext)
            act = self.filter_menu.addAction(f"{ext}  ({count})")
            act.setCheckable(True)
            act.setChecked(self._active_extensions is not None and ext in self._active_extensions)
            act.triggered.connect(lambda checked, e=ext: self._toggle_extension_filter(e, checked))

    def _clear_extension_filter(self):
        self._active_extensions = None
        self._refresh_file_list()

    def _toggle_extension_filter(self, ext, checked):
        active = set(self._active_extensions) if self._active_extensions else set()
        if checked:
            active.add(ext)
        else:
            active.discard(ext)
        self._active_extensions = active or None
        self._refresh_file_list()

    def _toggle_group(self, group_key):
        if group_key in self._collapsed_groups:
            self._collapsed_groups.discard(group_key)
        else:
            self._collapsed_groups.add(group_key)
        self._refresh_file_list()

    # Shown in every view for a display-only (non-editable) entry, and
    # used to strip interaction off its QListWidgetItem so it can be
    # looked at but never selected, double-clicked, or opened - it
    # exists in the list purely to show the file is there.
    _VIEW_ONLY_TIP = "Unsupported file type - shown for reference only, cannot be opened or edited."

    def _disable_view_only_item(self, item):
        item.setFlags(item.flags() & ~Qt.ItemIsEnabled & ~Qt.ItemIsSelectable)
        item.setToolTip(self._VIEW_ONLY_TIP)

    def _refresh_file_list(self):
        """
        Renders self.state.files (editable, searchable) and
        self.state.display_files (view-only - unsupported files kept
        only to show they exist) into the left panel according to
        self.view_mode:
          - "context": one row per file with line count/size/encoding/
            modified time - the original detailed list.
          - "grid": file tiles in an icon grid.
          - "path": files grouped under a non-selectable header item per
            parent directory, so a folder's contents (text and
            non-text alike) read as one real hierarchy.
        Every real file item carries its path in Qt.UserRole so
        selection-dependent actions (remove, line-range target) work
        the same regardless of which view is active. display_files
        entries are rendered dimmed and stripped of Qt.ItemIsEnabled/
        Qt.ItemIsSelectable, so they can never be selected, opened, or
        acted on - only text-based files are ever editable.
        """
        # clear() wipes selection along with the items - every search
        # (each of which now calls this to refresh line counts) was
        # silently deselecting whatever file the user had picked, so
        # Line Range would then reject an apparently-selected file with
        # "select a file first". Capture by path and restore it below.
        selected_paths = {e.path for e in self._selected_entries()}
        self.file_list.clear()
        self._path_row_labels = []
        t = self._effective_theme()
        folder_icon = icon("folder", self.theme)
        mode = self.view_mode
        if mode != "path":
            self.file_list.setProperty("pathMode", False)
            self.file_list.style().unpolish(self.file_list)
            self.file_list.style().polish(self.file_list)
        shown = [e for e in self.state.files if self._entry_passes_filter(e)]
        shown_display = [] if self.hide_unsupported else \
            [e for e in self.state.display_files if self._entry_passes_filter(e)]

        if mode == "grid":
            self.file_list.setViewMode(QListWidget.IconMode)
            self.file_list.setFlow(QListWidget.LeftToRight)
            self.file_list.setWrapping(True)
            self.file_list.setResizeMode(QListWidget.Adjust)
            self.file_list.setIconSize(QSize(40, 40))
            self.file_list.setSpacing(12)
            self.file_list.setMovement(QListWidget.Static)
            for entry in shown:
                item = QListWidgetItem(real_file_icon(entry.path), os.path.basename(entry.path))
                item.setData(Qt.UserRole, entry.path)
                item.setToolTip(entry.path)
                item.setTextAlignment(Qt.AlignHCenter)
                self.file_list.addItem(item)
            for entry in shown_display:
                item = QListWidgetItem(real_file_icon(entry.path), os.path.basename(entry.path))
                item.setTextAlignment(Qt.AlignHCenter)
                item.setForeground(QColor(t["fg_dim"]))
                self._disable_view_only_item(item)
                self.file_list.addItem(item)

        elif mode == "path":
            # The path/tree rows paint their own selection fill (TreeGuideRow),
            # but QListWidget::item:selected's native padding/radius/background
            # (theme.py) still paints behind every setItemWidget row regardless -
            # that leftover inset rounded block is what showed through as a
            # "shadow" layered under the row's own highlight. This property
            # flips a QSS attribute selector (see #fileList[pathMode="true"] in
            # theme.py) that zeroes the native item background/padding/radius
            # only while the tree view is active, leaving TreeGuideRow's flat
            # fill as the sole selection paint.
            self.file_list.setProperty("pathMode", True)
            self.file_list.style().unpolish(self.file_list)
            self.file_list.style().polish(self.file_list)
            self.file_list.setViewMode(QListWidget.ListMode)
            self.file_list.setFlow(QListWidget.TopToBottom)
            self.file_list.setWrapping(False)
            self.file_list.setSpacing(0)
            INDENT_PX = 18       # per nesting level, same idea as VS Code's tree indent guide
            CHEVRON_COL_PX = 22  # chevron width + spacing - files indent this much extra so their
                                 # icon lines up under the folder's icon, not its collapse arrow

            # Build a real nested tree (folders containing subfolders and
            # files) instead of one flat section per exact parent
            # directory string, so files pulled in from "Open folder"
            # read as an actual hierarchy - subfolders indented under
            # their parent, not each listed as its own top-level group.
            all_shown = shown + shown_display
            paths = [entry.path for entry in all_shown]
            if not paths:
                common_root = None
            else:
                try:
                    common_root = os.path.commonpath(paths) if len(paths) > 1 else os.path.dirname(paths[0])
                except ValueError:
                    common_root = None  # entries span different drives

            root = {"dirs": {}, "files": []}
            for entry in all_shown:
                rel = os.path.relpath(entry.path, common_root) if common_root else entry.path
                parts = [p for p in rel.split(os.sep) if p not in ("", ".")]
                node = root
                for part in parts[:-1]:
                    node = node["dirs"].setdefault(part, {"dirs": {}, "files": []})
                node["files"].append(entry)

            def count_all(node):
                total = len(node["files"])
                for child in node["dirs"].values():
                    total += count_all(child)
                return total

            # ElidedLabel reports a 0-width sizeHint (it has no minimum text
            # it needs to show), which made row.sizeHint() collapse to a
            # sliver a few pixels tall - just enough for the guide-line
            # dashes and the top fringe of the icon/text, clipping
            # everything else. Give every row an explicit fixed height
            # instead of trusting the composed sizeHint.
            ROW_HEIGHT = max(20, QFontMetrics(self.file_list.font()).height() + 8)

            def add_row(depth, left_px, on_click=None):
                row = TreeGuideRow(depth, INDENT_PX, t["border"], on_click=on_click,
                                    content_left=4 + left_px, content_height=ROW_HEIGHT - 6)
                row.setFixedHeight(ROW_HEIGHT)
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(4 + left_px, 3, 6, 3)
                row_layout.setSpacing(6)
                return row, row_layout

            # Deep/long paths otherwise ran the label text right off the
            # edge of the panel with no ellipsis - it just got clipped
            # mid-character, or bled past its row into whatever sat next
            # to it. ElidedLabel re-elides itself against its own current
            # width on every paint, so it stays correct as the panel is
            # resized instead of being baked in once against whatever
            # width happened to be current at build time.
            def render(node, depth, parent_parts):
                for name in sorted(node["dirs"], key=str.lower):
                    child = node["dirs"][name]
                    group_key = tuple(parent_parts + [name])
                    collapsed = group_key in self._collapsed_groups

                    header_item = QListWidgetItem()
                    header_item.setFlags(Qt.ItemIsEnabled)
                    row, lay = add_row(depth, depth * INDENT_PX,
                                        on_click=lambda k=group_key: self._toggle_group(k))

                    chevron = QToolButton()
                    chevron.setText("▸" if collapsed else "▾")  # ▸ / ▾
                    chevron.setFixedSize(16, 16)
                    chevron.setCursor(Qt.PointingHandCursor)
                    chevron.setStyleSheet(
                        "QToolButton { border: none; background: transparent; padding: 0; }"
                        f"QToolButton {{ color: {t['fg_dim']}; }}"
                    )
                    chevron.clicked.connect(lambda checked=False, k=group_key: self._toggle_group(k))
                    lay.addWidget(chevron)

                    icon_lbl = QLabel()
                    icon_lbl.setPixmap(folder_icon.pixmap(16, 16))
                    lay.addWidget(icon_lbl)

                    full_text = f"{name}   ({count_all(child)})"
                    text_lbl = ElidedLabel(full_text, t["fg_dim"])
                    lay.addWidget(text_lbl, 1)

                    self.file_list.addItem(header_item)
                    header_item.setSizeHint(QSize(0, ROW_HEIGHT))
                    self.file_list.setItemWidget(header_item, row)

                    if not collapsed:
                        render(child, depth + 1, parent_parts + [name])

                for entry in sorted(node["files"], key=lambda e: os.path.basename(e.path).lower()):
                    size = core.format_size(os.path.getsize(entry.path)) if os.path.isfile(entry.path) else "?"

                    item = QListWidgetItem()
                    row, lay = add_row(depth, depth * INDENT_PX + CHEVRON_COL_PX)

                    icon_lbl = QLabel()
                    icon_lbl.setPixmap(real_file_icon(entry.path).pixmap(16, 16))
                    lay.addWidget(icon_lbl)

                    if entry.editable:
                        item.setData(Qt.UserRole, entry.path)
                        item.setToolTip(entry.path)
                        full_text = f"{os.path.basename(entry.path)}   {len(entry.lines):,} lines - {size}"
                        text_lbl = ElidedLabel(full_text, t["fg"])
                        self._path_row_labels.append((item, text_lbl, row))
                    else:
                        self._disable_view_only_item(item)
                        full_text = f"{os.path.basename(entry.path)}   {size} - unsupported"
                        text_lbl = ElidedLabel(full_text, t["fg_dim"])
                    lay.addWidget(text_lbl, 1)

                    self.file_list.addItem(item)
                    item.setSizeHint(QSize(0, ROW_HEIGHT))
                    self.file_list.setItemWidget(item, row)

            render(root, 0, [])

        else:  # "context" - detailed list (default)
            self.file_list.setViewMode(QListWidget.ListMode)
            self.file_list.setFlow(QListWidget.TopToBottom)
            self.file_list.setWrapping(False)
            self.file_list.setIconSize(QSize(20, 20))
            self.file_list.setSpacing(2)
            for entry in shown:
                size = core.format_size(os.path.getsize(entry.path)) if os.path.isfile(entry.path) else "?"
                try:
                    modified = datetime.datetime.fromtimestamp(
                        os.path.getmtime(entry.path)).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    modified = "?"
                item = QListWidgetItem(
                    real_file_icon(entry.path),
                    f"{os.path.basename(entry.path)}\n"
                    f"{len(entry.lines):,} lines - {size} - {entry.encoding} - {modified}",
                )
                item.setData(Qt.UserRole, entry.path)
                item.setToolTip(entry.path)
                self.file_list.addItem(item)
            for entry in shown_display:
                size = core.format_size(os.path.getsize(entry.path)) if os.path.isfile(entry.path) else "?"
                try:
                    modified = datetime.datetime.fromtimestamp(
                        os.path.getmtime(entry.path)).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    modified = "?"
                item = QListWidgetItem(
                    real_file_icon(entry.path),
                    f"{os.path.basename(entry.path)}\n"
                    f"unsupported - {size} - {modified}",
                )
                item.setForeground(QColor(t["fg_dim"]))
                self._disable_view_only_item(item)
                self.file_list.addItem(item)

        if selected_paths:
            for i in range(self.file_list.count()):
                item = self.file_list.item(i)
                if item.data(Qt.UserRole) in selected_paths:
                    item.setSelected(True)

        n_total = len(self.state.files)
        n_shown = len(shown)
        n_view_only = len(shown_display)
        view_only_suffix = f" (+{n_view_only} view-only)" if n_view_only else ""
        if self._active_extensions is not None and n_shown != n_total:
            self.file_summary.setText(
                f"{n_shown} of {n_total} file{'s' if n_total != 1 else ''} shown{view_only_suffix}")
        else:
            self.file_summary.setText(
                f"{n_total} file{'s' if n_total != 1 else ''} loaded{view_only_suffix}")

        if self._active_extensions:
            self.filter_btn.setText(f" Filter ({len(self._active_extensions)})")
        else:
            self.filter_btn.setText(" Filter")

        self._update_path_selection_colors()

    def _update_path_selection_colors(self):
        # The "path" (group-by-folder) view renders each file row as a
        # custom widget (setItemWidget) instead of native item text, so
        # the QListWidget::item:selected stylesheet rule - which only
        # recolors an item's own text - never applies to it. The
        # selection highlight itself still paints correctly behind the
        # widget, but the label kept its normal foreground color on top,
        # which is what made a selected grouped file look wrong compared
        # to a selected file in the other views. Re-apply the right
        # color to every path-view label whenever selection changes.
        if not self._path_row_labels:
            return
        t = self._effective_theme()
        for item, label, row in self._path_row_labels:
            selected = item.isSelected()
            # Text keeps its normal color rather than swapping to accent_fg:
            # the selection fill is now a soft translucent tint (see
            # TreeGuideRow.paintEvent), not a solid accent block, so the
            # high-contrast "text on solid accent" color read as washed out
            # against it. The slim accent bar + tint carry the "selected"
            # signal instead.
            label.setSelected(selected, t["fg"])
            row.setSelected(selected, t["accent"])

    def _on_view_mode_change(self, button, checked):
        if not checked:
            return
        for value, b in self.view_buttons.items():
            if b is button:
                self.view_mode = value
                break
        self._refresh_file_list()

    def _selected_entries(self):
        """Loaded files currently selected in the file list, resolved by
        path (set on every real item's Qt.UserRole) rather than row
        index - the path/grid views insert header items or reorder rows,
        so row index alone can't identify which file was picked."""
        paths = {item.data(Qt.UserRole) for item in self.file_list.selectedItems()
                 if item.data(Qt.UserRole)}
        return [entry for entry in self.state.files if entry.path in paths]

    # ---------------------------------------------------------- background work --
    def _run_busy(self, work_fn, on_success, busy_text="Working...", quiet=False, cancellable=False):
        """Run work_fn() off the UI thread; call on_success(result) back on
        the UI thread when done. Keeps the window responsive (no "Not
        Responding") during slow disk I/O or large regex scans.

        IMPORTANT: worker.finished/failed/progress are connected to real
        bound methods of self (a QObject that was created on the UI
        thread), not to local closures. Qt/PySide only auto-detects a
        signal's target thread from a *bound method of a QObject* -
        connecting to a plain closure/lambda instead has no such
        receiver to inspect, so Qt invokes it directly on whichever
        thread emitted the signal (here, the worker thread) instead of
        queuing it back to the UI thread. That silently ran GUI calls
        (setEnabled, dialogs, text insertion) off the UI thread, which
        is undefined behavior in Qt and is what caused the window to
        lock up into unclickable "label" widgets with stray Windows
        error beeps.

        quiet=True skips the wait-cursor/disable/status-line side effects
        - for background syncs (the live filesystem watcher) that should
        never block or visibly interrupt whatever the user is doing.

        cancellable=True is for large folder scans/loads: work_fn must
        accept (progress_cb, should_cancel) instead of taking no
        arguments (see core.add_paths_to_state). The whole window is
        *not* disabled in this mode - unlike the plain busy state, the
        point is to let the user keep working (or hit Cancel) while a
        big directory is still being walked/loaded - and the progress
        row (bar + label + Cancel button) is shown instead of the
        static status-line text."""
        if cancellable:
            self.load_progress_bar.setRange(0, 0)
            self.load_progress_label.setText(busy_text)
            self.load_progress_row.show()
        elif not quiet:
            self.setCursor(Qt.WaitCursor)
            self._set_status(busy_text, "dim")
            self.setEnabled(False)

        thread = QThread(self)
        worker = _AsyncWorker(work_fn, wants_progress=cancellable)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        self._busy_threads.append({
            "thread": thread, "worker": worker, "on_success": on_success,
            "quiet": quiet, "cancellable": cancellable,
        })
        worker.finished.connect(self._on_busy_finished)
        worker.failed.connect(self._on_busy_failed)
        if cancellable:
            worker.progress.connect(self._on_busy_progress)
        thread.start()

    def _find_busy_entry(self, worker):
        for entry in self._busy_threads:
            if entry["worker"] is worker:
                return entry
        return None

    def _cancel_pending_loads(self):
        """Cancels any cancellable load (folder scan/import) still in
        flight - called before starting a new one, so switching folders
        mid-scan (Add -> pick another folder, drop a different one,
        open a different recent entry) stops the stale scan instead of
        letting it keep walking disk in the background after its result
        would just be discarded anyway."""
        for entry in self._busy_threads:
            if entry["cancellable"]:
                entry["worker"].cancel()

    def _on_cancel_load(self):
        # Several cancellable loads can't really overlap in practice (the
        # explorer/dialogs are modal while one runs), but guard for it
        # anyway rather than assuming there's exactly one.
        for entry in self._busy_threads:
            if entry["cancellable"]:
                entry["worker"].cancel()
        self.load_progress_cancel_btn.setEnabled(False)
        self.load_progress_label.setText("Cancelling...")

    def _on_busy_progress(self, phase, current, total):
        if total > 0:
            self.load_progress_bar.setRange(0, total)
            self.load_progress_bar.setValue(current)
            verb = "Scanning" if phase == "scanning" else "Loading"
            self.load_progress_label.setText(f"{verb} {current:,} of {total:,} file(s)...")
        else:
            self.load_progress_bar.setRange(0, 0)  # indeterminate - still walking the tree
            self.load_progress_label.setText(f"Scanning... {current:,} file(s) found so far")

    def _busy_cleanup(self, entry):
        if entry["cancellable"]:
            self.load_progress_row.hide()
            self.load_progress_cancel_btn.setEnabled(True)
        elif not entry["quiet"]:
            self.setEnabled(True)
            self.unsetCursor()
        entry["thread"].quit()
        entry["thread"].wait()
        self._busy_threads.remove(entry)

    def _on_busy_finished(self, result):
        entry = self._find_busy_entry(self.sender())
        if entry is None:
            return
        on_success = entry["on_success"]
        self._busy_cleanup(entry)
        on_success(result)

    def _on_busy_failed(self, tb_text):
        entry = self._find_busy_entry(self.sender())
        if entry is None:
            return
        self._busy_cleanup(entry)
        self._error("Unexpected error", tb_text)
        self._set_status("Action failed - see error details.", "error")

    def closeEvent(self, event):
        # Cancel any in-flight cancellable load (a big folder scan) and
        # wait for its thread to actually stop before letting the window
        # close - destroying a running QThread out from under its worker
        # is undefined behavior (and a common source of the "app closed
        # but a python.exe process lingers" symptom), not just a matter
        # of losing the partial results.
        for entry in list(self._busy_threads):
            if entry["cancellable"]:
                entry["worker"].cancel()
            entry["thread"].quit()
            entry["thread"].wait()
        super().closeEvent(event)

    # ---------------------------------------------------------- dialogs helper --
    def _dialog(self, kind, title, message, buttons=None):
        return AnimatedDialog.show(self, self._effective_theme(), self.theme, self.glass_enabled,
                                    kind, title, message, buttons)

    def _info(self, title, message):
        self._dialog("info", title, message)

    def _error(self, title, message):
        self._dialog("error", title, message)

    def _confirm(self, title, message, yes_label="Yes", no_label="Cancel"):
        return self._dialog("question", title, message,
                             buttons=[(no_label, False, False), (yes_label, True, True)])

    # ---------------------------------------------------------- drag & drop --
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_status("Drop to add file(s)...", "ok")

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._set_status("Ready.", "dim")

    def dropEvent(self, event):
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        paths = [p for p in paths if os.path.isfile(p) or os.path.isdir(p)]
        if not paths:
            self._set_status("Nothing droppable - only local files/folders can be added this way.", "warn")
            return
        self._remember_folders(paths)
        self._cancel_pending_loads()

        def work(progress_cb, should_cancel):
            return core.add_paths_to_state(paths, self.state, progress_cb=progress_cb, should_cancel=should_cancel)

        def done(result):
            self._report_add_result(result, "drag & drop")

        self._run_busy(work, done, "Loading dropped file(s)...", cancellable=True)

    def _sync_fs_watcher(self):
        """Rebuilds the watch list from self.state (sources + loaded
        files) after anything changes it. Runs on the UI thread, so this
        must stay cheap regardless of folder size: only the source
        folders themselves and each loaded file's immediate directory
        are watched, not every subdirectory. An earlier version walked
        the whole tree here to watch every subfolder for new files -
        that recursive os.walk ran synchronously on the UI thread on
        every add, and on a real project folder that alone froze the
        window for as long as collect_text_files used to. New files
        several levels deep in a folder that has no loaded file yet are
        picked up on the next full add/rescan rather than instantly -
        an acceptable trade for never blocking the UI here."""
        old_dirs = self._fs_watcher.directories()
        old_files = self._fs_watcher.files()
        if old_dirs:
            self._fs_watcher.removePaths(old_dirs)
        if old_files:
            self._fs_watcher.removePaths(old_files)

        dirs = {p for p in self.state.sources if os.path.isdir(p)}
        for f in self.state.files:
            dirs.add(os.path.dirname(f.path))
        for f in self.state.display_files:
            dirs.add(os.path.dirname(f.path))

        if dirs:
            self._fs_watcher.addPaths(list(dirs))
        files = [f.path for f in self.state.files]
        if files:
            self._fs_watcher.addPaths(files)

    def _on_fs_event(self, _path=None):
        # Coalesce bursts of events (a save touches a file and its
        # directory's mtime, a build can touch dozens of files at once)
        # into one rescan instead of one per signal.
        self._fs_debounce.start(400)

    def _do_live_rescan(self):
        if self._live_rescanning:
            self._fs_debounce.start(400)  # re-arm; a fresh change arrived mid-scan
            return
        if not self.state.sources:
            return
        self._live_rescanning = True

        def work():
            try:
                return core.rescan_sources(self.state)
            except OSError:
                return False

        def done(changed):
            self._live_rescanning = False
            if changed:
                self._refresh_file_list()
                self._set_status("Solution explorer updated - files changed on disk.", "dim")
            self._sync_fs_watcher()

        self._run_busy(work, done, quiet=True)

    def _report_add_result(self, result, source):
        """Shared summary for on_add / dropEvent / startup-path loads:
        non-text files are no longer failures - they're recorded as
        view-only entries (state.display_files) so the file/folder view
        can show they exist, so only genuine load errors pop a dialog."""
        ok_count, messages, view_only_count = result
        self._refresh_file_list()
        self._sync_fs_watcher()
        cancel_msgs = [m for m in messages if m.startswith("Load cancelled")]
        messages = [m for m in messages if not m.startswith("Load cancelled")]
        if messages:
            self._error("Some files failed to load", "\n".join(messages))
        if cancel_msgs:
            self._set_status(cancel_msgs[0], "warn")
            return
        if ok_count or view_only_count:
            suffix = f" ({view_only_count} shown as view-only, unsupported for editing)" if view_only_count else ""
            self._set_status(f"Added {ok_count} file(s) via {source}.{suffix}", "ok")
        elif skipped and not errors:
            self._set_status(f"No text-based files found - skipped {len(skipped)} non-text file(s).", "warn")
        elif not errors:
            self._set_status("Ready.", "dim")

    # ---------------------------------------------------------- toolbar --
    def on_add(self):
        choice = self._dialog(
            "question", "Add",
            "Add individual file(s), or an entire folder? Folders are expanded "
            "into every text-based file under them, recursively - the same "
            "thing the Explorer right-click \"Open with CodeLens\" does.",
            buttons=[("Folder...", "folder", False), ("File(s)...", "files", True)],
        )
        if choice == "folder":
            folder = QFileDialog.getExistingDirectory(self, "Select a folder to add")
            if not folder:
                return
            paths = [folder]
        elif choice == "files":
            paths, _ = QFileDialog.getOpenFileNames(self, "Select file(s) to add")
            if not paths:
                return
        else:
            return

        self._remember_folders(paths)
        self._cancel_pending_loads()

        def work(progress_cb, should_cancel):
            return core.add_paths_to_state(paths, self.state, progress_cb=progress_cb, should_cancel=should_cancel)

        def done(result):
            self._report_add_result(result, "Add")

        self._run_busy(work, done, "Loading file(s)...", cancellable=True)

    def _on_file_list_context_menu(self, pos):
        item = self.file_list.itemAt(pos)
        if item is None or item.data(Qt.UserRole) is None:
            return  # empty area, or a non-selectable folder-header row in tree view
        if item not in self.file_list.selectedItems():
            self.file_list.clearSelection()
            item.setSelected(True)
        selected = self._selected_entries()
        if not selected:
            return

        menu = QMenu(self)
        remove_label = f"Remove {len(selected)} file(s)" if len(selected) > 1 else "Remove file"
        menu.addAction(remove_label).triggered.connect(self.on_remove)

        reveal_action = menu.addAction("Reveal in Explorer")
        reveal_action.triggered.connect(lambda checked=False, e=selected[0]: self._reveal_in_explorer(e.path))

        open_with_menu = menu.addMenu("Open with")
        editors = core.find_installed_editors()
        if editors:
            icon_provider = QFileIconProvider()
            for name, exe_path in editors:
                # The exe's own real Windows icon (same one Explorer shows
                # for it), not a generic/text-only menu entry.
                action = open_with_menu.addAction(icon_provider.icon(QFileInfo(exe_path)), name)
                action.triggered.connect(
                    lambda checked=False, e=exe_path, files=selected: self._open_with_editor(e, files))
        else:
            no_editors = open_with_menu.addAction("No editors found on this system")
            no_editors.setEnabled(False)

        menu.exec(self.file_list.viewport().mapToGlobal(pos))

    def _open_with_editor(self, exe_path, entries):
        try:
            subprocess.Popen([exe_path] + [e.path for e in entries])
        except OSError as e:
            self._error("Couldn't launch editor", str(e))

    def _reveal_in_explorer(self, path):
        if not core.reveal_in_explorer(path):
            self._error("Couldn't reveal file",
                        f"This file no longer exists on disk:\n\n{path}")

    def on_remove(self):
        selected = self._selected_entries()
        if not selected:
            self._info("Remove file", "Select one or more files in the list first.")
            return
        names = [os.path.basename(e.path) for e in selected]
        if not self._confirm(
            "Remove file(s)",
            f"Remove {len(selected)} file(s) from the loaded set?\n\n" + "\n".join(names),
            yes_label="Remove",
        ):
            return
        self.state.files = [e for e in self.state.files if e not in selected]
        self.state.reset_search()
        gc.collect()
        self._refresh_file_list()
        self._sync_fs_watcher()
        self._set_status(f"Removed {len(selected)} file(s).", "warn")

    def _remember_folders(self, paths):
        """Records every directory in `paths` in the recent-folders MRU -
        the folder path only (like Visual Studio's devenv recent-folder
        list), not an entry per file it expanded into."""
        for p in paths:
            if os.path.isdir(p):
                remember_recent_folder(p)

    def on_show_recent(self):
        menu = self._build_recent_menu()
        menu.exec(self.btn_recent.mapToGlobal(self.btn_recent.rect().bottomLeft()))

    def _build_recent_menu(self):
        t = self._effective_theme()
        folders = load_recent_folders()
        menu = QMenu(self)
        if not folders:
            action = menu.addAction("No recent folders")
            action.setEnabled(False)
        else:
            for folder in folders:
                display = folder if len(folder) <= 60 else "..." + folder[-57:]

                row = QWidget()
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(10, 4, 6, 4)
                row_layout.setSpacing(8)

                label = QLabel(display)
                label.setToolTip(folder)
                row_layout.addWidget(label, 1)

                remove_btn = QToolButton()
                remove_btn.setIcon(icon("remove", self.theme))
                remove_btn.setIconSize(QSize(12, 12))
                remove_btn.setFixedSize(20, 20)
                remove_btn.setCursor(Qt.PointingHandCursor)
                remove_btn.setToolTip("Remove from recent folders")
                remove_btn.setStyleSheet(
                    "QToolButton { border: none; background: transparent; }"
                    f"QToolButton:hover {{ background: {t['bg_card_hover']}; border-radius: 4px; }}"
                )
                row_layout.addWidget(remove_btn)

                action = QWidgetAction(menu)
                action.setDefaultWidget(row)
                menu.addAction(action)

                action.triggered.connect(lambda checked=False, f=folder: self._open_recent_folder(f))
                row.mouseReleaseEvent = lambda e, a=action: (a.trigger(), menu.close()) if e.button() == Qt.LeftButton else None
                remove_btn.clicked.connect(lambda checked=False, f=folder: self._remove_recent_folder(f, menu))

            menu.addSeparator()
            clear_action = menu.addAction(icon("clear", self.theme), "Clear All")
            clear_action.triggered.connect(self._clear_recent_folders)
        return menu

    def _remove_recent_folder(self, folder, menu):
        save_recent_folders([f for f in load_recent_folders()
                                   if os.path.normcase(f) != os.path.normcase(folder)])
        menu.close()
        QTimer.singleShot(0, self.on_show_recent)

    def _clear_recent_folders(self):
        if not self._confirm(
            "Clear recent folders",
            "This removes every folder from your recent-folders list. Continue?",
            yes_label="Clear",
        ):
            return
        save_recent_folders([])

    def _open_recent_folder(self, folder):
        if not os.path.isdir(folder):
            self._error("Folder not found", f"This folder no longer exists:\n\n{folder}")
            save_recent_folders([f for f in load_recent_folders()
                                       if os.path.normcase(f) != os.path.normcase(folder)])
            return
        if self.state.files and not self._confirm(
            "Start a new file set",
            "This clears every currently loaded file and loads the selected recent folder. Continue?",
            yes_label="Continue",
        ):
            return
        self.state.clear_files()
        self._refresh_file_list()
        self._sync_fs_watcher()
        self._clear_results()
        self._remember_folders([folder])
        self._cancel_pending_loads()

        def work(progress_cb, should_cancel):
            return core.add_paths_to_state([folder], self.state, progress_cb=progress_cb, should_cancel=should_cancel)

        def done(result):
            self._report_add_result(result, "Recent")

        self._run_busy(work, done, "Loading folder...", cancellable=True)

    def on_change(self):
        if self.state.files and not self._confirm(
            "Start a new file set",
            "This clears every currently loaded file and lets you pick a fresh set. Continue?",
            yes_label="Continue",
        ):
            return
        self.state.clear_files()
        self._refresh_file_list()
        self._sync_fs_watcher()
        self._clear_results()
        # on_add() now shows its own AnimatedDialog (Files vs Folder) -
        # calling it directly here would fire that dialog back-to-back
        # with this method's own confirm dialog, with zero gap between
        # one modal QDialog's exec() returning and the next one's exec()
        # starting. That back-to-back chaining left the second dialog
        # modal (blocking all input) but never actually faded in/became
        # visible - a stuck, invisible-but-blocking window. Deferring to
        # the next event loop iteration gives the first dialog's close
        # animation/teardown a chance to fully finish first.
        QTimer.singleShot(0, self.on_add)

    def on_reload(self):
        if not self.state.is_loaded():
            self._set_status("No files loaded.", "warn")
            return
        def work():
            errors = []
            new_files = list(self.state.files)
            for i, entry in enumerate(new_files):
                new_entry, msg = core.load_single_file(entry.path)
                if new_entry:
                    new_files[i] = new_entry
                else:
                    errors.append(msg)
            return new_files, errors

        def done(result):
            new_files, errors = result
            self.state.files = new_files
            self.state.reset_search()
            gc.collect()
            self._refresh_file_list()
            if errors:
                self._error("Reload finished with errors", "\n".join(errors))
                self._set_status("Reload finished with errors.", "error")
            else:
                self._set_status(f"Reloaded {len(self.state.files)} file(s) from disk.", "ok")

        self._run_busy(work, done, "Reloading file(s) from disk...")

    def on_reset(self):
        if not self._confirm(
            "Reset search state",
            "This clears the current search state (your last search term/snippet "
            "will be forgotten). This cannot be undone.",
            yes_label="Reset",
        ):
            self._set_status("Reset cancelled.", "dim")
            return

        if self.state.is_loaded() and self._confirm(
            "Reset scope",
            f"Also remove ALL {len(self.state.files)} loaded file(s) and start over?\n\n"
            f"Yes = remove everything and relaunch file selection.\n"
            f"No = just reset the search state, keep files loaded.",
            yes_label="Remove all",
        ):
            self.state.clear_files()
            self._refresh_file_list()
            self._clear_results()
            self._set_status("All files removed - relaunching file selection.", "warn")
            QTimer.singleShot(0, self.on_add)  # see on_change's comment on why this is deferred
            return

        self.state.reset_search()
        self._clear_results()
        self._set_status("Search state reset.", "ok")

    def on_show_details(self):
        if not self.state.is_loaded():
            self._info("File Details", "No files loaded yet - add some first.")
            return
        FileDetailsDialog.show_details(self, self._effective_theme(), self.state.files)

    def on_clear_results(self):
        self._clear_results()
        self._set_status("Results cleared.", "dim")

    def _clear_results(self):
        self.results.clear()
        self.results_summary.setText("")
        self._results_buffer = []

    # ---------------------------------------------------------- zoom --
    # QTextEdit.setFont() alone doesn't work here: the global stylesheet
    # sets font-family on #results, and once a widget has any styled
    # font property Qt's style engine keeps re-resolving the widget's
    # font on every polish, silently discarding a plain setFont() point
    # size change. QTextEdit/QPlainTextEdit's built-in zoomIn()/zoomOut()
    # bypass that - they scale the document's rendered font size
    # directly - so those are used instead, with a step counter just for
    # the on-screen "N%" label and for zooming back out exactly on reset.
    ZOOM_MIN_STEPS, ZOOM_MAX_STEPS = -5, 20

    def on_zoom_in(self):
        if self.results_zoom_steps >= self.ZOOM_MAX_STEPS:
            return
        self.results.zoomIn(1)
        self.results_zoom_steps += 1
        self._update_zoom_label()

    def on_zoom_out(self):
        if self.results_zoom_steps <= self.ZOOM_MIN_STEPS:
            return
        self.results.zoomOut(1)
        self.results_zoom_steps -= 1
        self._update_zoom_label()

    def on_zoom_reset(self):
        if self.results_zoom_steps > 0:
            self.results.zoomOut(self.results_zoom_steps)
        elif self.results_zoom_steps < 0:
            self.results.zoomIn(-self.results_zoom_steps)
        self.results_zoom_steps = 0
        self._update_zoom_label()

    def _update_zoom_label(self):
        self.zoom_lbl.setText(f"{100 + self.results_zoom_steps * 10}%")

    # ---------------------------------------------------------- preview --
    def _on_file_double_clicked(self, item):
        path = item.data(Qt.UserRole)
        if not path:
            return
        entry = next((e for e in self.state.files if e.path == path), None)
        if entry is None:
            if any(e.path == path for e in self.state.display_files):
                self._set_status(self._VIEW_ONLY_TIP, "dim")
            return
        PreviewDialog.preview(self, self._effective_theme(), self.theme, self.glass_enabled, entry)

    # ---------------------------------------------------------- search --
    def _on_mode_change(self, button, checked):
        if not checked:
            return
        mode = self._mode_value()
        index = {"one": 0, "multi": 1, "range": 2}[mode]
        self.input_stack.setCurrentIndex(index)
        self.input_stack.setFixedHeight(96 if mode == "multi" else self._one_line_height)
        # Line Range needs one specific file - flag that up front when
        # switching into the mode with several files loaded and none
        # selected yet, instead of only after the user types a range and
        # clicks Search and gets what looks like a rejection/error.
        if mode == "range" and len(self.state.files) > 1 and not self._selected_entries():
            self._set_status("Select a file in the list on the left to show a line range from.", "warn")

    def _mode_value(self):
        for value, rb in self.mode_buttons.items():
            if rb.isChecked():
                return value
        return "one"

    def on_search(self):
        if not self.state.is_loaded():
            self._info("No files loaded", "Add at least one file first.")
            return
        mode = self._mode_value()
        if mode == "one":
            self._search_one_line()
        elif mode == "multi":
            self._search_multi_line()
        else:
            self._search_line_range()

    def _t(self):
        return self._effective_theme()

    def _write(self, text, color=None, bold=False, mono=True, bg=None):
        text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    .replace("\n", "<br>"))
        style = []
        if color:
            style.append(f"color:{color}")
        if bg:
            style.append(f"background-color:{bg}")
        if bold:
            style.append("font-weight:600")
        if mono:
            style.append(f"font-family:'{FONT_MONO}'")
        style_attr = f' style="{";".join(style)}"' if style else ""
        # Buffered rather than a live self.results.insertHtml() call: each
        # insertHtml() forces QTextEdit to re-lay out the whole document,
        # so calling it per match/line (thousands of times for a big line
        # range or a common search term) turned into an O(n^2) UI stall -
        # the "becomes unresponsive" reports. One flush() at the end is
        # a single layout pass no matter how many lines were written.
        self._results_buffer.append(f"<span{style_attr}>{text}</span>")

    def _flush_results(self):
        if self._results_buffer:
            self.results.insertHtml("".join(self._results_buffer))
            self._results_buffer = []

    def _search_one_line(self):
        term = self.query_entry.text()
        if not term:
            self._info("Empty search", "Type something to search for.")
            return
        flags = 0 if self.case_chk.isChecked() else re.IGNORECASE
        try:
            pattern = re.compile(core.build_flexible_pattern(term), flags)
        except re.error as e:
            self._error("Pattern error", str(e))
            return
        self.state.last_search = f"one: '{term}'"

        t = self._t()
        state = self.state

        # Its own background thread: auto-reload-from-disk, matching, and
        # HTML building all happen off the UI thread. Only the final
        # buffer touches a widget, in done() below, on the UI thread.
        def work():
            core.auto_reload_check(state)
            files = state.files
            buffer = []
            total, files_hit = 0, 0
            for i, entry in enumerate(files, start=1):
                results = core._find_matches(entry.lines, pattern, multiline=False)
                if results:
                    total += len(results)
                    files_hit += 1
                _html_render_file_card(buffer, t, i, len(files), entry, results)
            return buffer, total, files_hit

        def done(result):
            buffer, total, files_hit = result
            self._refresh_file_list()
            self._clear_results()
            self._results_buffer = buffer
            self._finish_search(total, files_hit)

        self._run_busy(work, done, "Searching...")

    def _search_multi_line(self):
        snippet = self.query_multiline.toPlainText()
        if not snippet.strip():
            self._info("Empty search", "Type or paste a snippet to search for.")
            return
        flags = 0 if self.case_chk.isChecked() else re.IGNORECASE
        try:
            pattern_as_typed = re.compile(core.build_flexible_pattern(snippet), flags)
        except re.error as e:
            self._error("Pattern error", str(e))
            return

        pattern_stripped_snippet = None
        if core.has_comments(snippet):
            stripped = core.COMMENT_MARKER_RE.sub("", snippet)
            if stripped.strip():
                try:
                    pattern_stripped_snippet = re.compile(core.build_flexible_pattern(stripped), flags)
                except re.error:
                    pattern_stripped_snippet = None

        self.state.last_search = f"multi: {snippet!r}"
        t = self._t()
        state = self.state

        def work():
            core.auto_reload_check(state)
            files = state.files
            buffer = []
            total, files_hit = 0, 0
            for i, entry in enumerate(files, start=1):
                results, note = core._search_file_with_comment_fallback(
                    entry, pattern_as_typed, pattern_stripped_snippet)
                if results:
                    total += len(results)
                    files_hit += 1
                _html_render_file_card(buffer, t, i, len(files), entry, results, note=note)
            return buffer, total, files_hit

        def done(result):
            buffer, total, files_hit = result
            self._refresh_file_list()
            self._clear_results()
            self._results_buffer = buffer
            self._finish_search(total, files_hit)

        self._run_busy(work, done, "Searching...")

    def _search_line_range(self):
        entry = self._pick_range_file()
        if entry is None:
            return
        raw = self.range_entry.text()
        parsed = core.parse_range(raw, len(entry.lines))
        if not parsed:
            self._error("Invalid range", "Use e.g. '1500-3000' or a single line number.")
            return
        start, end = parsed
        if start > len(entry.lines) or end < 1:
            self._error("Out of bounds", f"File only has {len(entry.lines)} lines.")
            return

        path = entry.path
        state = self.state

        # A big range (e.g. an entire large file) still froze the window
        # even after buffering: building one <span> pair of HTML per line
        # for tens/hundreds of thousands of lines makes QTextEdit's HTML
        # parser itself the bottleneck, not the insertHtml() call count.
        # Plain text has no per-line markup to parse, so build and insert
        # the body as plain text instead - the string building also runs
        # off the UI thread since it's pure Python with no widget access.
        def work():
            core.auto_reload_check(state)
            current = next((e for e in state.files if e.path == path), entry)
            lo = max(1, min(start, len(current.lines)))
            hi = max(1, min(end, len(current.lines)))
            width = len(str(len(current.lines)))
            lines = current.lines[lo - 1:hi]
            body = "\n".join(
                f"{str(lo + i).rjust(width)}: {line}" for i, line in enumerate(lines)
            )
            return current, lo, hi, body

        def done(result):
            current, lo, hi, body = result
            self._refresh_file_list()
            self._clear_results()
            t = self._t()
            self._write(f"{os.path.basename(current.path)}: lines {lo}-{hi}\n", color=t["secondary"], bold=True)
            self._write(f"{current.path}\n\n", color=t["fg_dim"])
            self._flush_results()
            self.results.insertPlainText(body)
            self.results_summary.setText(f"{hi - lo + 1} line(s) shown")
            self._set_status(f"Showing lines {lo}-{hi} of {os.path.basename(current.path)}.", "ok")

        self._run_busy(work, done, "Loading line range...")

    def _pick_range_file(self):
        if len(self.state.files) == 1:
            return self.state.files[0]
        selected = self._selected_entries()
        if selected:
            return selected[0]
        self._info("Pick a file",
                   "Select which loaded file to show a line range from in the file list on the left.")
        return None

    def _finish_search(self, total, files_hit):
        n = len(self.state.files)
        if total:
            self.results_summary.setText(f"{total} match(es) in {files_hit}/{n} file(s)")
            self._set_status(f"Found {total} matching occurrence(s) in {files_hit}/{n} file(s).", "ok")
        else:
            self.results_summary.setText("0 matches")
            self._set_status("No matches found in any loaded file.", "warn")
            self._write("No matches found in any loaded file.\n", color=self._t()["warn"])
        self._flush_results()
