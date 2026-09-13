"""
persistence.py - small JSON files CodeLens persists next to the
running script/exe: keybindings and the recent-folders MRU list.
"""

import json
import os

from theme import BASE_DIR

# --------------------------------------------------------------------------
# KEYBOARD SHORTCUTS (editable in Settings, persisted to shortcuts.json)
# --------------------------------------------------------------------------
SHORTCUT_ACTIONS = [
    ("search", "Run Search", "Ctrl+Return"),
    ("add", "Add File(s)", "Alt+A"),
    ("reload", "Reload Files", "Ctrl+R"),
    ("reset", "Reset", "Ctrl+Shift+R"),
    ("clear_results", "Clear Results", "Shift+C"),
    ("case_sensitive", "Toggle Case Sensitive", "Alt+S"),
]
SHORTCUTS_FILE = os.path.join(BASE_DIR, "shortcuts.json")


def load_shortcuts():
    mapping = {key: default for key, _label, default in SHORTCUT_ACTIONS}
    if os.path.isfile(SHORTCUTS_FILE):
        try:
            with open(SHORTCUTS_FILE, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            mapping.update({k: v for k, v in saved.items() if k in mapping and v})
        except (OSError, json.JSONDecodeError):
            pass
    return mapping


def save_shortcuts(mapping):
    try:
        with open(SHORTCUTS_FILE, "w", encoding="utf-8") as fh:
            json.dump(mapping, fh, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------
# RECENT FOLDERS (devenv-style MRU: folder paths only, not every file)
# --------------------------------------------------------------------------
RECENT_FOLDERS_FILE = os.path.join(BASE_DIR, "recent_folders.json")
MAX_RECENT_FOLDERS = 10


def load_recent_folders():
    if os.path.isfile(RECENT_FOLDERS_FILE):
        try:
            with open(RECENT_FOLDERS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                return [p for p in data if isinstance(p, str)][:MAX_RECENT_FOLDERS]
        except (OSError, json.JSONDecodeError):
            pass
    return []


def save_recent_folders(folders):
    try:
        with open(RECENT_FOLDERS_FILE, "w", encoding="utf-8") as fh:
            json.dump(folders[:MAX_RECENT_FOLDERS], fh, indent=2)
    except OSError:
        pass


def remember_recent_folder(folder):
    """Pushes `folder` to the front of the persisted MRU list, like
    Visual Studio's recent-folder list under File > Open > Folder -
    just the folder path itself, not every file inside it."""
    folders = load_recent_folders()
    folder = os.path.abspath(folder)
    folders = [f for f in folders if os.path.normcase(f) != os.path.normcase(folder)]
    folders.insert(0, folder)
    save_recent_folders(folders)
    return folders[:MAX_RECENT_FOLDERS]


# --------------------------------------------------------------------------
# VIEW SETTINGS (misc persisted toggles - solution explorer display, etc.)
# --------------------------------------------------------------------------
VIEW_SETTINGS_FILE = os.path.join(BASE_DIR, "view_settings.json")
VIEW_SETTINGS_DEFAULTS = {
    "hide_unsupported": False,
}


def load_view_settings():
    settings = dict(VIEW_SETTINGS_DEFAULTS)
    if os.path.isfile(VIEW_SETTINGS_FILE):
        try:
            with open(VIEW_SETTINGS_FILE, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            settings.update({k: v for k, v in saved.items() if k in settings})
        except (OSError, json.JSONDecodeError):
            pass
    return settings


def save_view_setting(key, value):
    settings = load_view_settings()
    settings[key] = value
    try:
        with open(VIEW_SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2)
    except OSError:
        pass
