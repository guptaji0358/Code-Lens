# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CodeLens is a precision line/symbol search tool for text files, distributed as a Windows desktop
app + installer. There is no build system, package manifest, or test suite — it's plain Python run
directly or packaged into a frozen exe by a separate (external) PyInstaller step.

## Commands

```bash
# Run the terminal edition (stdlib only, colorama optional)
python scripts/CodeLensCLI.py

# Run the desktop edition (needs PySide6)
pip install PySide6
python scripts/CodeLens.py

# Run the installer/uninstaller UI (also PySide6)
python scripts/installer_app.py            # install wizard
python scripts/installer_app.py --uninstall
```

There is no lint config, test suite, or requirements.txt in this repo. Syntax-check a file after
editing with `python -m py_compile scripts/CodeLens.py`.

## Architecture

**One search engine, two front-ends.** `scripts/CodeLensCLI.py` holds all shared logic — file
loading/encoding detection (`read_file_smart`, `load_single_file`), the `FileState`/`LoadedFile`
model, folder expansion (`collect_text_files`, `add_paths_to_state`), auto-reload-from-disk before
every search (`auto_reload_check`, `rescan_sources`), the actual search/pattern matching, and
installed-editor detection. `scripts/CodeLens.py` (the PySide6 GUI) does `import CodeLensCLI as
core` and calls into it rather than reimplementing anything — there is exactly one implementation
of file loading and search behavior. When changing loading/search/reload semantics, edit
`CodeLensCLI.py`; both front-ends pick it up automatically.

**State model:** `core.FileState` holds `files` (loaded `LoadedFile` entries: path, lines,
encoding, mtime) and `sources` (every raw file/folder path ever explicitly added — used to
re-derive the expected file set for live folder rescans, since folders are expanded into flat file
lists rather than kept as a tree). `state.files` is mutated directly from background worker
threads in several places (existing pattern) rather than only from the UI thread.

**GUI async pattern (`CodeLens.py`):** all slow work (disk I/O, folder scans) runs via
`FinderWindow._run_busy(work_fn, on_success, quiet=False)`, which moves an `_AsyncWorker` to a
`QThread`. `work_fn`'s return value is delivered back on the UI thread through
`worker.finished`/`worker.failed` — these **must** stay connected to real bound methods of a
`QObject` (`_on_busy_finished`/`_on_busy_failed`), never lambdas/closures, or Qt invokes them
directly on the worker thread instead of queuing to the UI thread (this caused a real
unclickable-window bug). `quiet=True` skips the wait-cursor/disable/status-line side effects, for
background syncs (the filesystem watcher) that shouldn't interrupt the user.

**Live filesystem sync:** `FinderWindow._fs_watcher` (`QFileSystemWatcher`) watches every directory
under each added folder plus every loaded file, debounced through `_fs_debounce` into
`_do_live_rescan`, which calls `core.rescan_sources(state)` to diff the on-disk file set against
`state.files` (adds new files, drops deleted ones, reloads changed ones) and re-syncs the watch
list via `_sync_fs_watcher()`. Any code path that changes `state.files` or `state.sources` (add,
remove, new set, reopen recent folder) must call `_sync_fs_watcher()` afterwards.

**Custom UI chrome:** dialogs, checkboxes, and the splash screen are all hand-built
(`AnimatedDialog`, `GlassCheckbox`, `GlassSwitch`, `SplashScreen`) rather than native Qt widgets, to
keep a consistent glassmorphism look. `AnimatedDialog.show(...)` is the one entry point used for
every info/warning/error/confirm popup (`FinderWindow._dialog/_info/_confirm/_error`) — never use
`QMessageBox` directly. Chaining two modal dialogs back-to-back (e.g. a confirm followed by another
dialog) must go through `QTimer.singleShot(0, ...)` between them, or the second dialog ends up
modal but invisible (see the comment on `on_change`).

**File list rendering:** `FinderWindow._refresh_file_list` renders `state.files` according to
`view_mode` ("context" detail list, "grid" icon tiles, or "path" grouped-by-folder tree) — it
fully rebuilds `self.file_list` each call and must restore the prior selection by path (`Qt.UserRole`),
since row index isn't stable across view modes.

**Windows integration (`scripts/installer_app.py`):** registers "Open with CodeLens" /
"Open folder with CodeLens" Explorer context-menu entries under `HKCU\Software\Classes\...`, routed
through a generated VBScript (`OpenWithCodeLens.vbs`) launched via `wscript.exe` rather than a
direct `ShellExecute`/`WScript.Shell.Run` of the exe — both of those were tried and hit real
failures (an unsigned-exe ShellExecute block, and an OLE/drag-and-drop threading crash), documented
in `_write_launcher_vbs`'s docstring. `CodeLens.py` receives the clicked file/folder path as
`sys.argv[1:]` and expands folders via `core.add_paths_to_state`.

## Persisted files

Written next to the running script/exe (`BASE_DIR`, resolved via `_resource_base_dir()` for both
source and frozen-exe layouts): `shortcuts.json` (keybindings) and `recent_folders.json` (MRU of
opened folder paths, devenv-style — folder paths only, not files). Crash tracebacks and Windows
fatal-exception dumps go to `%LOCALAPPDATA%\CodeLens_crash.log` via `_install_global_error_handling`.
