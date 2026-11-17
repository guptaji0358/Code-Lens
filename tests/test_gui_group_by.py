"""Regression tests: Group-by-folder view must stay responsive on big folders
(it used to build a widget per file synchronously and froze the UI)."""

import os
import sys
import tempfile
import time
import unittest

from agent_helpers import SCRIPTS

if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt, QTimer, QEventLoop
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False


def pump(ms=60):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class GroupByFolderTests(unittest.TestCase):
    N_DIRS = 10
    PER_DIR = 300

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        import persistence
        import CodeLensCLI as core
        from main_window import FinderWindow
        cls.tmp = tempfile.TemporaryDirectory()
        cls._orig = persistence.VIEW_SETTINGS_FILE
        persistence.VIEW_SETTINGS_FILE = os.path.join(cls.tmp.name, "v.json")
        cls.persistence = persistence
        root = os.path.join(cls.tmp.name, "proj")
        for d in range(cls.N_DIRS):
            os.makedirs(os.path.join(root, f"d{d}"))
            for i in range(cls.PER_DIR):
                with open(os.path.join(root, f"d{d}", f"f{i:04d}.py"), "w") as fh:
                    fh.write("x = 1\n")
        cls.win = FinderWindow()
        cls.win.resize(1300, 800)
        cls.win.show()
        pump(400)  # let any argv-supplied paths finish loading, then start clean
        cls.win.state.files = []
        cls.win.state.display_files = []
        core.add_paths_to_state([root], cls.win.state)
        cls.win.view_mode = "path"

    @classmethod
    def tearDownClass(cls):
        cls.win.close()
        cls.persistence.VIEW_SETTINGS_FILE = cls._orig
        cls.tmp.cleanup()

    def setUp(self):
        self.win._collapsed_groups = set()
        self.win.view_mode = "path"

    def test_render_is_fast_and_only_visible_rows_get_widgets(self):
        w = self.win
        t = time.time()
        w._refresh_file_list()
        elapsed = time.time() - t
        total = self.N_DIRS * self.PER_DIR
        self.assertEqual(w.file_list.count(), total + self.N_DIRS)
        self.assertLess(elapsed, 3.0, "3000-file Group By render took %.1fs" % elapsed)
        built = sum(1 for i in range(w.file_list.count()) if w.file_list.itemWidget(w.file_list.item(i)))
        self.assertGreater(built, 0)
        self.assertLess(built, 300, "rows must be built lazily, not one per file")

    def test_scrolling_materialises_rows(self):
        w = self.win
        w._refresh_file_list()
        last = w.file_list.count() - 1
        self.assertIsNone(w.file_list.itemWidget(w.file_list.item(last)))
        sb = w.file_list.verticalScrollBar()
        sb.setValue(sb.maximum())
        pump(40)
        self.assertIsNotNone(w.file_list.itemWidget(w.file_list.item(last)))
        sb.setValue(0)

    def test_collapse_and_expand_group(self):
        w = self.win
        w._refresh_file_list()
        full = w.file_list.count()
        key = ("d0",)
        w._toggle_group(key)
        self.assertEqual(w.file_list.count(), full - self.PER_DIR)
        w._toggle_group(key)
        self.assertEqual(w.file_list.count(), full)

    def test_selected_row_colouring_survives_lazy_build(self):
        w = self.win
        w._refresh_file_list()
        sb = w.file_list.verticalScrollBar()
        sb.setValue(sb.maximum()); pump(40)
        item = w.file_list.item(w.file_list.count() - 1)
        item.setSelected(True)
        pump(20)
        selected = [(i, lbl) for i, lbl, _r in w._path_row_labels if i is item]
        self.assertEqual(len(selected), 1)
        self.assertEqual([e.path for e in w._selected_entries()], [item.data(Qt.UserRole)])
        sb.setValue(0)

    def test_other_views_unchanged(self):
        w = self.win
        for mode in ("context", "grid", "path", "context"):
            w.view_mode = mode
            w._refresh_file_list()
        self.assertEqual(w.file_list.count(), self.N_DIRS * self.PER_DIR)


if __name__ == "__main__":
    unittest.main()
