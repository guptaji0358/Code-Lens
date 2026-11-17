# Architecture

Verified against `scripts/` on 2026-11-17. Source is authoritative.

## One engine, two front-ends

- `scripts/CodeLensCLI.py` (~2800 lines) holds all shared logic: file loading and encoding
  detection (`read_file_smart`, `load_single_file`), the `LoadedFile` / `FileState` model,
  folder expansion (`collect_text_files`, `add_paths_to_state`), auto-reload before search
  (`auto_reload_check`, `rescan_sources`), search matching, and installed-editor detection.
- The terminal edition runs directly from it (`python scripts/CodeLensCLI.py`).
- The PySide6 GUI imports it as `core` and calls into it. There is one implementation of
  load/search/reload behaviour: change `CodeLensCLI.py`, not the GUI.
- The agent interface reuses it unmodified through `scripts/codelens_agent/engine.py`
  (see [agent-interface.md](agent-interface.md)).

## GUI file responsibilities (`scripts/`)

| File | Role |
|------|------|
| `CodeLens.py` | Thin entry point only. Runs DPI awareness before any QApplication, then `app.main()`. |
| `app.py` | Crash/hang watchdog installers (crash log in `%LOCALAPPDATA%\CodeLens_crash.log`), `SplashScreen`, `main()`. |
| `main_window.py` | `FinderWindow` (one large class holding shared `self.*` state) and `_AsyncWorker`. |
| `theme.py` | `THEMES` colours, `build_stylesheet`, icon helpers, and `BASE_DIR` via `_resource_base_dir()`. |
| `widgets.py` | Small reusable widgets with no app logic (`GlassSwitch`, `GlassCheckbox`, `ElidedLabel`, hover-icon buttons, ...). |
| `dialogs.py` | Popups: `AnimatedDialog` (QMessageBox replacement), `ShortcutsDialog`, `PreviewDialog`, `FileDetailsDialog`, `SettingsPanel`, `CommandReferenceDialog`, ... |
| `persistence.py` | `shortcuts.json` and `recent_folders.json` load/save, next to the exe or script. |
| `results_render.py` | Pure functions building results-pane HTML. No widgets, safe off the UI thread. |
| `installer_app.py` | Install/uninstall wizard (PySide6) and Explorer context-menu registration. |
| `native_context_menu.py` | Native context-menu support module (not yet mapped in detail). |

Placement rule (from `CLAUDE.md`): new small widgets/dialogs/helpers go in the matching file above;
new `FinderWindow` methods stay in `main_window.py` next to the state they touch.

## State model

- `core.FileState.files`: loaded `LoadedFile` entries (path, lines, encoding, mtime).
- `core.FileState.sources`: every raw file/folder path explicitly added. Used to re-derive the
  expected file set for live rescans, because folders are flattened into file lists, not kept as a tree.
- Background threads mutate `state.files` directly in several places. This is an existing pattern.

## Async GUI pattern (`main_window.py`)

- Slow work (disk I/O, folder scans) runs through `FinderWindow._run_busy(work_fn, on_success,
  busy_text, quiet, cancellable)` (~line 1501). It moves an `_AsyncWorker` onto a `QThread`.
- Results return on the UI thread via `worker.finished` / `worker.failed`, connected to
  `_on_busy_finished` (~1626) and `_on_busy_failed` (~1642). These **must** be bound methods of a
  QObject, never lambdas or closures. Otherwise Qt runs them on the worker thread. That caused a
  real unclickable-window bug.
- `quiet=True` skips wait-cursor, disable, and status-line side effects. Used for background
  watcher syncs.

## Live filesystem sync

- `FinderWindow._fs_watcher` (`QFileSystemWatcher`, ~line 140) watches each added folder's
  directories and each loaded file. Events are debounced via `_fs_debounce` into `_do_live_rescan`
  (~1747). That calls `core.rescan_sources(state)` to add, drop, or reload files, then
  `_sync_fs_watcher()` (~1709).
- Any code path that changes `state.files` or `state.sources` (add, remove, new set, reopen recent
  folder) must call `_sync_fs_watcher()` afterwards.

## UI rules

- Every info/warning/error/confirm popup goes through `AnimatedDialog.show(...)`, reached via
  `FinderWindow._dialog/_info/_confirm/_error`. Do not use `QMessageBox`.
- Two modal dialogs in a row must be separated by `QTimer.singleShot(0, ...)`. Otherwise the second
  is modal but invisible.
- The file list (`_refresh_file_list`, ~1222) renders in three modes: `context` (detail list),
  `grid` (icon tiles), and `path` (grouped tree). It fully rebuilds on each call and restores
  selection by path (`Qt.UserRole`), because row indices differ between modes.
- The custom chrome (`GlassCheckbox`, `GlassSwitch`, `SplashScreen`, dialogs) is hand-built on
  purpose, for a consistent glassmorphism look.

## Windows integration (`scripts/installer_app.py`)

- Writes "Open with CodeLens" and "Open folder with CodeLens" under `HKCU\Software\Classes\...`.
  Each entry launches through a generated `OpenWithCodeLens.vbs` run by `wscript.exe`.
  Direct `ShellExecute` and `WScript.Shell.Run` were tried and failed. The rationale is in the
  `_write_launcher_vbs` docstring.
- The GUI receives the clicked path as `sys.argv[1:]` (read in `FinderWindow.__init__`). Folders
  expand through `core.add_paths_to_state`.

## Known gaps

- `scripts/native_context_menu.py` and `scripts/results_render.py` were not read in full during
  initialization. Their descriptions above come from file names and the `CLAUDE.md` summary only.
- `main_window.py` is large (~2360 lines). Splitting it further was deliberately left alone.
