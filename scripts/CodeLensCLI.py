#!/usr/bin/env python3
"""
CodeLensCLI.py - CodeLens CLI Edition (UNIVERSAL)
A self-contained terminal-GUI tool for precise, line-accurate text search
inside any file (code, config, text) - built to avoid the search
limitations, spacing bugs, missing line numbers, and stale file caching
of Visual Studio's Ctrl+F. Every search auto-reloads the file from disk
first, so edits saved in your editor are always reflected immediately.

Author: Robin Gupta
Run:    python CodeLensCLI.py
"""

import os
import re
import sys
import bisect
import codecs
import shutil
import string
import datetime

try:
    import msvcrt
    _HAVE_MSVCRT = True
except ImportError:
    _HAVE_MSVCRT = False

try:
    import readline
    _HAVE_READLINE = True
except ImportError:
    _HAVE_READLINE = False

# --------------------------------------------------------------------------
# COLOR / ANSI SETUP
# --------------------------------------------------------------------------
try:
    # sys.stdout/sys.stderr are None when this module is imported from a
    # no-console/windowed build (e.g. CodeLens.py frozen with PyInstaller's
    # --windowed flag) - colorama.init() assumes a real stream and crashes
    # trying to wrap None, which would otherwise take the whole GUI down
    # on startup even though the GUI never prints colored terminal text.
    if sys.stdout is None or sys.stderr is None:
        raise RuntimeError("no console stdout/stderr to wrap")
    from colorama import Fore, Back, Style, init as _colorama_init
    _colorama_init(autoreset=True)
    _HAVE_COLORAMA = True
except Exception:
    _HAVE_COLORAMA = False

    if os.name == "nt" and sys.stdout is not None and sys.stderr is not None:
        # Enable ANSI/VT100 escape processing on modern Windows terminals.
        # Skipped when there is no real console (e.g. this module imported
        # by CodeLens.py's --windowed GUI build): os.system("") spawns
        # cmd.exe to run the (empty) command, and with no console attached
        # to inherit, Windows briefly flashes a brand-new console window
        # into existence just to host it - the exact "console appears and
        # disappears" symptom reported against the GUI app.
        try:
            os.system("")
        except Exception:
            pass

    class _Fore:
        BLACK = "\033[30m"
        RED = "\033[31m"
        GREEN = "\033[32m"
        YELLOW = "\033[33m"
        BLUE = "\033[34m"
        MAGENTA = "\033[35m"
        CYAN = "\033[36m"
        WHITE = "\033[37m"
        LIGHTBLACK_EX = "\033[90m"
        LIGHTRED_EX = "\033[91m"
        LIGHTGREEN_EX = "\033[92m"
        LIGHTYELLOW_EX = "\033[93m"
        LIGHTBLUE_EX = "\033[94m"
        LIGHTMAGENTA_EX = "\033[95m"
        LIGHTCYAN_EX = "\033[96m"
        LIGHTWHITE_EX = "\033[97m"
        RESET = "\033[39m"

    class _Back:
        BLACK = "\033[40m"
        RED = "\033[41m"
        GREEN = "\033[42m"
        YELLOW = "\033[43m"
        BLUE = "\033[44m"
        MAGENTA = "\033[45m"
        CYAN = "\033[46m"
        WHITE = "\033[47m"
        RESET = "\033[49m"

    class _Style:
        BRIGHT = "\033[1m"
        DIM = "\033[2m"
        NORMAL = "\033[22m"
        RESET_ALL = "\033[0m"

    Fore, Back, Style = _Fore(), _Back(), _Style()

# Semantic color palette (dark / cyan / neon)
C_TITLE = Fore.CYAN + Style.BRIGHT
C_BORDER = Fore.CYAN
C_ACCENT = Fore.MAGENTA + Style.BRIGHT
C_OK = Fore.GREEN + Style.BRIGHT
C_WARN = Fore.YELLOW + Style.BRIGHT
C_ERR = Fore.RED + Style.BRIGHT
C_INFO = Fore.LIGHTBLUE_EX if hasattr(Fore, "LIGHTBLUE_EX") else Fore.BLUE
C_DIM = Style.DIM
C_LINE_NUM = Fore.CYAN + Style.BRIGHT
C_HIGHLIGHT = Back.YELLOW if hasattr(Back, "YELLOW") else ""
C_HIGHLIGHT_TXT = Fore.BLACK if hasattr(Fore, "BLACK") else ""
C_PROMPT = Fore.MAGENTA + Style.BRIGHT
R = Style.RESET_ALL

MAX_RANGE_CHUNK = 500
ENCODING_ATTEMPTS = ["utf-8-sig", "utf-8", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "latin-1"]


def _supports_unicode_box_drawing():
    """
    Classic cmd.exe often runs on a legacy codepage (CP437/850) that
    has no mapping for Unicode box-drawing characters, which would
    raise UnicodeEncodeError on print(); PowerShell/Windows Terminal
    default to UTF-8 (CP65001) and handle them fine. Probe once at
    startup and fall back to plain ASCII borders when unsupported.
    """
    try:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        "┌─┐│└┘".encode(encoding)
        return True
    except Exception:
        return False


if _supports_unicode_box_drawing():
    BOX_TL, BOX_TR, BOX_BL, BOX_BR, BOX_H, BOX_V = "┌", "┐", "└", "┘", "─", "│"
else:
    BOX_TL, BOX_TR, BOX_BL, BOX_BR, BOX_H, BOX_V = "+", "+", "+", "+", "-", "|"


# --------------------------------------------------------------------------
# GENERAL UTILITIES
# --------------------------------------------------------------------------
def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def get_term_width():
    """
    Current terminal column count, clamped to a comfortable range so
    borders/boxes stay readable whether the console is a tiny popup
    window or a maximized wide terminal - too narrow and text wraps
    mid-word, too wide and a single border line looks lost. Queried
    fresh on every call (not cached) so a mid-session resize is picked
    up immediately on the next screen redraw.
    """
    cols = shutil.get_terminal_size(fallback=(63, 24)).columns
    return max(40, min(cols - 1, 100))


def hr(char="=", width=None, color=C_BORDER):
    if width is None:
        width = get_term_width()
    print(color + (char * width) + R)


def show_banner():
    clear_screen()
    width = get_term_width()
    hr("=", width)
    print(C_TITLE + "CODELENS CLI v1.0 (UNIVERSAL)".center(width) + R)
    print(C_ACCENT + "Precision Line & Symbol Search for Developers".center(width) + R)
    hr("=", width)
    print(C_DIM + "Ctrl+V paste | Ctrl+Shift+C copy | Ctrl+Z undo | Ctrl+Y redo".center(width) + R)
    print()


def press_enter(msg="Press Enter to continue..."):
    try:
        input(C_DIM + msg + R)
    except (EOFError, KeyboardInterrupt):
        pass


def format_size(num_bytes):
    if num_bytes < 1024:
        return f"{num_bytes} B"
    kb = num_bytes / 1024
    if kb < 1024:
        return f"{kb:.2f} KB"
    mb = kb / 1024
    return f"{mb:.2f} MB"


def strip_quotes(text):
    text = text.strip()

    # PowerShell prefixes drag-and-dropped/pasted paths with a call
    # operator, e.g. `& 'C:\some path\file.cpp'` or `& "C:\file.cpp"`.
    # Strip that operator before touching quotes.
    if text.startswith("&"):
        text = text[1:].strip()

    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1]

    return text.strip()


# --------------------------------------------------------------------------
# TAB-COMPLETION / SMART INPUT
#
# Plain input() has no autocomplete on Windows. This gives every prompt
# shell-style Tab completion: press Tab to complete a partial command
# ('p' + Tab -> 'pick'), or to list options when the prefix is ambiguous.
# --------------------------------------------------------------------------
FILE_SELECT_COMMANDS = ["pick", "info", "exit"]
MAIN_MENU_COMMANDS = ["one", "multi", "lines", "info", "change", "add", "remove", "clear", "reset", "reload", "exit"]


def _common_prefix(strings):
    if not strings:
        return ""
    return os.path.commonprefix(strings)


def _path_candidates(buffer, limit=30):
    """
    Returns filesystem completions for a partially-typed path, e.g.
    'D:\\Apps\\SVG_Stu' -> ['D:\\Apps\\SVG_Studio.cpp', 'D:\\Apps\\SVG_Studio\\'].

    A bare drive spec like 'D:' or 'D:\\' is special-cased to list that
    drive's root - os.path.split() alone would treat 'D:' as a relative
    name with no separator and wrongly search the current directory.
    """
    if not buffer:
        return []
    expanded = os.path.expanduser(os.path.expandvars(buffer))
    drive, rest = os.path.splitdrive(expanded)
    typed_drive, _ = os.path.splitdrive(buffer)

    if not rest or rest in (os.sep, "/"):
        lookup_dir = (drive or ".") + os.sep
        typed_dir = (typed_drive + os.sep) if typed_drive else ""
        partial = ""
    else:
        typed_dir, partial = os.path.split(buffer)
        lookup_dir = os.path.split(expanded)[0] or "."

    try:
        entries = os.listdir(lookup_dir)
    except OSError:
        return []

    lower_partial = partial.lower()
    matches = []
    for entry in entries:
        if not entry.lower().startswith(lower_partial):
            continue
        full = os.path.join(typed_dir, entry) if typed_dir else entry
        if os.path.isdir(os.path.join(lookup_dir, entry)):
            full += os.sep
        matches.append(full)

    matches.sort()
    return matches[:limit]


def _file_select_candidates(buffer):
    if not buffer:
        return list(FILE_SELECT_COMMANDS)
    keyword_matches = [k for k in FILE_SELECT_COMMANDS if k.startswith(buffer.lower())]
    return keyword_matches + _path_candidates(buffer)


def _main_menu_candidates(buffer):
    if not buffer:
        return list(MAIN_MENU_COMMANDS)
    return [k for k in MAIN_MENU_COMMANDS if k.startswith(buffer.lower())]


def _erase_typed(count):
    if count:
        sys.stdout.write("\b" * count + " " * count + "\b" * count)
        sys.stdout.flush()


def _get_clipboard_text():
    """
    Reads text off the Windows clipboard via raw ctypes calls into
    user32/kernel32 - no pywin32 or other extra dependency needed, so
    CodeLensCLI.py stays a single self-contained file. Tries CF_UNICODETEXT
    first, then falls back to CF_TEXT (ANSI) - some sources only put
    ANSI text on the clipboard, and GetClipboardData(CF_UNICODETEXT)
    returns nothing in that case even though there IS text available.
    Returns None on any failure rather than raising, since a failed
    paste should just be a no-op.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        CF_UNICODETEXT = 13
        CF_TEXT = 1
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if not user32.OpenClipboard(0):
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            is_unicode = True
            if not handle:
                handle = user32.GetClipboardData(CF_TEXT)
                is_unicode = False
            if not handle:
                return None
            locked = kernel32.GlobalLock(handle)
            if not locked:
                return None
            try:
                if is_unicode:
                    text = ctypes.wstring_at(locked)
                else:
                    text = ctypes.string_at(locked).decode("mbcs", errors="replace")
            finally:
                kernel32.GlobalUnlock(handle)
            return text
        finally:
            user32.CloseClipboard()
    except Exception:
        return None


def _set_clipboard_text(text):
    """
    Writes `text` to the Windows clipboard as CF_UNICODETEXT, via raw
    ctypes calls (same self-contained approach as _get_clipboard_text).
    Returns True on success, False on any failure.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        data = text + "\0"
        size = len(data) * ctypes.sizeof(ctypes.c_wchar)

        if not user32.OpenClipboard(0):
            return False
        try:
            user32.EmptyClipboard()
            h_mem = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not h_mem:
                return False
            locked = kernel32.GlobalLock(h_mem)
            if not locked:
                return False
            ctypes.memmove(locked, data, size)
            kernel32.GlobalUnlock(h_mem)
            if not user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                kernel32.GlobalFree(h_mem)
                return False
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        return False


def _set_console_icon():
    """
    Swaps the running console window's title-bar/taskbar icon to
    Assets/app_icon.ico via raw ctypes calls - same self-contained,
    no-extra-dependency approach as the clipboard helpers above. Purely
    cosmetic, so any failure (non-Windows, headless console, missing
    icon file) is silently ignored rather than interrupting startup.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        # A plain os.path.dirname(__file__) breaks once this script is
        # frozen into an .exe: __file__ then points inside the
        # bootloader's temp extraction folder, not next to the real
        # .exe. sys.frozen / sys._MEIPASS are what PyInstaller sets for
        # a frozen app to find its own bundled data.
        if getattr(sys, "frozen", False):
            base_dir = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        else:
            base_dir = os.path.dirname(os.path.abspath(__file__))
        ico_path = os.path.join(base_dir, "Assets", "app_icon.ico")
        if not os.path.isfile(ico_path):
            return

        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        LR_DEFAULTSIZE = 0x0040

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        hwnd = kernel32.GetConsoleWindow()
        if not hwnd:
            return

        for flag, size in ((ICON_SMALL, 16), (ICON_BIG, 32)):
            hicon = user32.LoadImageW(
                None, ico_path, IMAGE_ICON, size, size, LR_LOADFROMFILE | LR_DEFAULTSIZE
            )
            if hicon:
                user32.SendMessageW(hwnd, WM_SETICON, flag, hicon)
    except Exception:
        pass


def _shift_is_down():
    """True if the Shift key is currently held, via GetAsyncKeyState.
    Used to tell Ctrl+Shift+C (copy) apart from plain Ctrl+C (cancel) -
    Windows consoles send the same 0x03 control code for both, since
    Shift doesn't change the ASCII value produced by a Ctrl+letter
    combination, so the only way to tell them apart is to check the
    live key state at the moment the 0x03 arrives."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        VK_SHIFT = 0x10
        return bool(ctypes.windll.user32.GetAsyncKeyState(VK_SHIFT) & 0x8000)
    except Exception:
        return False


def _smart_input_windows(prompt, candidates_fn):
    """
    Raw single-line editor with Tab-completion plus the editing keys
    people expect from any normal text field:
      - Ctrl+Z  undo              - Ctrl+Y  redo
      - Ctrl+V (or Ctrl+Shift+V)  paste from the Windows clipboard
      - Ctrl+Shift+C  copy the current line to the Windows clipboard
      - Backspace, Esc (clear line), Enter, Ctrl+C (abort)
    Undo/redo is a simple snapshot stack, taken before every mutation
    (typed char, backspace, paste, tab-completion, clear).
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    buffer = ""
    undo_stack = []
    redo_stack = []

    def snapshot():
        undo_stack.append(buffer)
        redo_stack.clear()

    while True:
        ch = msvcrt.getwch()

        if ch in ("\r", "\n"):
            # A pasted block (or some terminals' own Enter key) delivers
            # "\r\n" as two separate characters. If we only consume the
            # "\r" here, the paired "\n" is left sitting at the front of
            # the input queue, where the NEXT read would immediately see
            # it and return an empty line - silently truncating whatever
            # was being typed/pasted right after. Swallow that paired
            # "\n" now (push back anything else with ungetwch, since it
            # wasn't part of this line ending).
            if ch == "\r" and msvcrt.kbhit():
                nxt = msvcrt.getwch()
                if nxt != "\n":
                    msvcrt.ungetwch(nxt)
            sys.stdout.write("\n")
            sys.stdout.flush()
            return buffer

        if ch == "\x03":
            # Windows sends the same 0x03 code for Ctrl+C and
            # Ctrl+Shift+C - check whether Shift is actually held to
            # tell "copy the line" apart from "cancel".
            if _shift_is_down():
                copied = bool(buffer) and _set_clipboard_text(buffer)
                sys.stdout.write("\n")
                if copied:
                    sys.stdout.write(f"[Copied {len(buffer)} chars to clipboard]\n")
                elif not buffer:
                    sys.stdout.write("[Nothing to copy]\n")
                else:
                    sys.stdout.write("[Copy failed]\n")
                sys.stdout.write(prompt + buffer)
                sys.stdout.flush()
                continue
            raise KeyboardInterrupt

        if ch == "\x04":  # Ctrl+D - EOF
            raise EOFError

        if ch == "\x1a":  # Ctrl+Z - undo
            if undo_stack:
                redo_stack.append(buffer)
                _erase_typed(len(buffer))
                buffer = undo_stack.pop()
                sys.stdout.write(buffer)
                sys.stdout.flush()
            continue

        if ch == "\x19":  # Ctrl+Y - redo
            if redo_stack:
                undo_stack.append(buffer)
                _erase_typed(len(buffer))
                buffer = redo_stack.pop()
                sys.stdout.write(buffer)
                sys.stdout.flush()
            continue

        if ch == "\x16":  # Ctrl+V (and Ctrl+Shift+V - same code either way,
                          # Shift doesn't change what a Ctrl+letter sends)
                          # - paste from clipboard
            clip = _get_clipboard_text()
            if clip:
                # These prompts are single-line, so collapse any
                # newlines in the pasted text instead of letting them
                # act like an Enter keypress mid-paste.
                clip = clip.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            if clip:
                snapshot()
                buffer += clip
                sys.stdout.write(clip)
                sys.stdout.flush()
            continue

        if ch == "\x08":  # Backspace
            if buffer:
                snapshot()
                buffer = buffer[:-1]
                _erase_typed(1)
            continue

        if ch == "\x1b":  # Esc clears the line
            if buffer:
                snapshot()
            _erase_typed(len(buffer))
            buffer = ""
            continue

        if ch in ("\x00", "\xe0"):  # Extended key prefix (arrows, F-keys) - discard
            msvcrt.getwch()
            continue

        if ch == "\t":
            matches = candidates_fn(buffer)
            if not matches:
                # Nothing to complete - insert a literal tab instead of
                # silently discarding the keypress, so tab-indented code
                # typed or pasted into a prompt with no completions
                # (e.g. a search snippet) isn't corrupted.
                snapshot()
                buffer += "\t"
                sys.stdout.write("\t")
                sys.stdout.flush()
                continue
            if len(matches) == 1:
                completion = matches[0]
            else:
                completion = _common_prefix(matches)

            if completion and completion != buffer:
                snapshot()
                _erase_typed(len(buffer))
                buffer = completion
                sys.stdout.write(buffer)
                sys.stdout.flush()
            elif len(matches) > 1:
                sys.stdout.write("\n" + "   ".join(matches) + "\n")
                sys.stdout.write(prompt + buffer)
                sys.stdout.flush()
            continue

        if ch.isprintable():
            snapshot()
            buffer += ch
            sys.stdout.write(ch)
            sys.stdout.flush()
        # Other control characters are ignored.


def _smart_input_readline(prompt, candidates_fn):
    def completer(text, state):
        matches = candidates_fn(readline.get_line_buffer())
        try:
            return matches[state]
        except IndexError:
            return None

    old_delims = readline.get_completer_delims()
    old_completer = readline.get_completer()
    readline.set_completer_delims("")
    readline.set_completer(completer)
    readline.parse_and_bind("tab: complete")
    try:
        return input(prompt)
    finally:
        readline.set_completer(old_completer)
        readline.set_completer_delims(old_delims)


def smart_input(prompt, candidates_fn):
    """
    Reads a line of input with Tab-completion when running in a real
    interactive console. Falls back to plain input() when stdin/stdout
    are redirected (pipes, test harnesses) or no completion backend is
    available, so behavior stays correct in every environment.
    """
    interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if interactive and os.name == "nt" and _HAVE_MSVCRT:
        try:
            return _smart_input_windows(prompt, candidates_fn)
        except OSError:
            pass  # Fall through to plain input() below.

    if interactive and _HAVE_READLINE:
        try:
            return _smart_input_readline(prompt, candidates_fn)
        except Exception:
            pass

    return input(prompt)


def _more_input_already_buffered():
    """
    Non-blocking check for whether more input is already sitting in the
    console's input queue (i.e. we're still mid-paste) versus the user
    having genuinely stopped typing and just pressed Enter. Used to tell
    a blank line that's part of a pasted multi-line snippet apart from a
    blank line that means "I'm done" - without this, either every mid-
    snippet blank line prematurely ends the paste (dropping the rest of
    it) or every genuine "I'm done" blank line is ignored (making the
    prompt look like it hung).
    """
    try:
        if os.name == "nt" and _HAVE_MSVCRT:
            return msvcrt.kbhit()
        if sys.stdin.isatty():
            import select
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            return bool(ready)
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------
# FILE STATE
# --------------------------------------------------------------------------
class LoadedFile:
    """One file's worth of loaded content."""

    def __init__(self, path, lines, encoding, mtime):
        self.path = path
        self.lines = lines       # list[str], 1-indexed via lines[i-1]
        self.encoding = encoding
        self.mtime = mtime       # last known on-disk modification time


class FileState:
    """Holds every currently-loaded file. Searches run across all of
    them at once; earlier versions of this tool only ever held one."""

    def __init__(self):
        self.files = []          # list[LoadedFile]
        self.last_search = None

    def is_loaded(self):
        return bool(self.files)

    def reset_search(self):
        self.last_search = None

    def clear_files(self):
        self.files = []
        self.last_search = None

    def paths(self):
        return [f.path for f in self.files]


# --------------------------------------------------------------------------
# ENCODING-AWARE FILE LOADING
# --------------------------------------------------------------------------
def read_file_smart(path):
    """
    Reads a file's raw bytes and decodes it using a BOM check first,
    then falls back through a list of common encodings, finally
    guaranteeing success with latin-1 (which can decode any byte value).
    Returns (text, encoding_used).
    """
    with open(path, "rb") as fh:
        raw = fh.read()

    bom_map = [
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ]
    for bom, enc in bom_map:
        if raw.startswith(bom):
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                pass

    for enc in ENCODING_ATTEMPTS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue

    # Guaranteed fallback - latin-1 can decode any byte sequence.
    return raw.decode("latin-1", errors="replace"), "latin-1 (best-effort)"


def load_single_file(path):
    """
    Reads `path` off disk. Returns (LoadedFile or None, message).
    Pure function - does not touch a FileState, so it doubles as both
    the initial loader and the reloader (auto-reload, manual reload).
    """
    try:
        if not os.path.isfile(path):
            return None, f"Path does not exist or is not a file: {path}"

        text, encoding = read_file_smart(path)
        lines = text.splitlines()
        abspath = os.path.abspath(path)
        try:
            mtime = os.path.getmtime(abspath)
        except OSError:
            mtime = None

        entry = LoadedFile(abspath, lines, encoding, mtime)
        return entry, f"Loaded '{os.path.basename(path)}' ({len(lines)} lines, encoding: {encoding})"

    except FileNotFoundError:
        return None, f"File not found: {path}"
    except PermissionError:
        return None, f"Permission denied while reading: {path}"
    except UnicodeDecodeError as e:
        return None, f"Unable to decode file (unsupported encoding): {e}"
    except IsADirectoryError:
        return None, f"Path is a directory, not a file: {path}"
    except OSError as e:
        return None, f"OS error while reading file: {e}"
    except Exception as e:
        return None, f"Unexpected error loading file: {e}"


def add_file_to_state(path, state):
    """
    Loads `path` and adds it to `state.files` - or, if that exact path
    is already loaded, replaces it in place (so re-selecting the same
    file refreshes it rather than duplicating it). Returns (success, message).
    """
    entry, msg = load_single_file(path)
    if not entry:
        return False, msg

    for i, existing in enumerate(state.files):
        if existing.path == entry.path:
            state.files[i] = entry
            state.reset_search()
            return True, f"Duplicate found - replaced existing entry: {msg}"

    state.files.append(entry)
    state.reset_search()
    return True, msg


# --------------------------------------------------------------------------
# HOT-RELOAD: NEVER SEARCH STALE IN-MEMORY CONTENT
# --------------------------------------------------------------------------
def auto_reload_check(state):
    """
    For every loaded file, compares its on-disk modification time
    against the last-known mtime. Any file that has changed (e.g.
    edited and saved in Visual Studio) is silently re-read from disk
    so every search always runs against fresh content. Returns True if
    at least one file was reloaded.
    """
    if not state.is_loaded():
        return False

    any_reloaded = False
    for i, entry in enumerate(state.files):
        try:
            current_mtime = os.path.getmtime(entry.path)
        except OSError:
            print(C_WARN + f"[auto-reload] '{os.path.basename(entry.path)}' is "
                            f"temporarily inaccessible - using last cached copy." + R)
            continue

        if entry.mtime is not None and current_mtime == entry.mtime:
            continue

        new_entry, msg = load_single_file(entry.path)
        if new_entry:
            state.files[i] = new_entry
            any_reloaded = True
            print(C_DIM + f"[auto-reload] '{os.path.basename(entry.path)}' changed on "
                           f"disk - refreshed ({len(new_entry.lines)} lines)." + R)
        else:
            print(C_ERR + f"[auto-reload] '{os.path.basename(entry.path)}' changed but "
                           f"could not be re-read: {msg}" + R)
    return any_reloaded


def force_reload_files(state):
    """
    Unconditionally re-reads every loaded file from disk, regardless of
    whether its mtime has changed. Used by the manual 'reload' command
    so the user can refresh on demand (e.g. after saving in an editor
    that doesn't update mtime the way Windows expects, or just to be
    sure they're looking at current content).
    """
    if not state.is_loaded():
        print(C_WARN + "No files loaded." + R)
        return

    for i, entry in enumerate(state.files):
        new_entry, msg = load_single_file(entry.path)
        if new_entry:
            state.files[i] = new_entry
        print((C_OK if new_entry else C_ERR) + msg + R)
    state.reset_search()


# --------------------------------------------------------------------------
# FILE SEARCH-BY-NAME (recursive)
# --------------------------------------------------------------------------
def list_available_drives():
    """Returns every mounted drive root on Windows (e.g. ['C:\\\\', 'D:\\\\']),
    or ['/'] on POSIX systems."""
    if os.name == "nt":
        return [f"{letter}:\\" for letter in string.ascii_uppercase
                if os.path.exists(f"{letter}:\\")]
    return ["/"]


def _walk_search(filename, roots, max_results=200, on_root_start=None):
    """
    Shared recursive scan used by both the cwd-scoped and system-wide
    searches. Tries exact (case-insensitive) filename match first across
    all roots; only falls back to substring matches if there were no
    exact hits anywhere.
    """
    target = filename.strip().lower()
    exact_matches = []
    partial_matches = []

    for root in roots:
        if on_root_start:
            on_root_start(root)
        for dirpath, dirnames, filenames in os.walk(root):
            # Skip common noise / inaccessible junk quietly.
            dirnames[:] = [d for d in dirnames if d not in (
                "$RECYCLE.BIN", "System Volume Information", "node_modules", ".git"
            )]
            try:
                for fname in filenames:
                    lower = fname.lower()
                    full = os.path.join(dirpath, fname)
                    if lower == target:
                        exact_matches.append(full)
                    elif target in lower:
                        partial_matches.append(full)
            except PermissionError:
                continue
            if len(exact_matches) + len(partial_matches) >= max_results:
                break
        if len(exact_matches) + len(partial_matches) >= max_results:
            break

    results = exact_matches if exact_matches else partial_matches
    return results[:max_results]


def search_file_by_name(filename, root=None, max_results=200):
    """
    Recursively searches from `root` (default: current working directory)
    for files matching `filename`. Tries exact (case-insensitive) filename
    match first; if none found, falls back to substring match.
    """
    root = root or os.getcwd()
    return _walk_search(filename, [root], max_results=max_results)


def search_file_system_wide(filename, max_results=200, on_root_start=None):
    """
    Recursively searches every mounted drive (Windows) or the filesystem
    root (POSIX) for files matching `filename`. Used as a fallback when a
    plain filename search under the current directory finds nothing.
    """
    return _walk_search(filename, list_available_drives(), max_results=max_results,
                         on_root_start=on_root_start)


# --------------------------------------------------------------------------
# FILE INFO / METADATA
# --------------------------------------------------------------------------
def display_info(state):
    auto_reload_check(state)
    print()
    hr("-", get_term_width(), C_BORDER)
    if not state.is_loaded():
        print(C_WARN + "No files are currently loaded." + R)
        hr("-", get_term_width(), C_BORDER)
        return

    print_loaded_files_matrix(state.files, f"FILE INFORMATION ({len(state.files)} file(s) loaded)")
    hr("-", get_term_width(), C_BORDER)


# --------------------------------------------------------------------------
# SEARCH HELPERS
# --------------------------------------------------------------------------
def highlight_line(line, spans):
    """
    Given a line and a list of (start, end) match spans, wraps each
    match in highlight color codes for terminal display.
    """
    if not spans:
        return line
    out = []
    cursor = 0
    for start, end in spans:
        out.append(line[cursor:start])
        out.append(f"{C_HIGHLIGHT}{C_HIGHLIGHT_TXT}{line[start:end]}{R}")
        cursor = end
    out.append(line[cursor:])
    return "".join(out)


def print_match(line_no, line_text, spans, width, prefix=""):
    num_str = str(line_no).rjust(width)
    print(f"{prefix}{C_LINE_NUM}{num_str}:{R} {highlight_line(line_text, spans)}")


def _find_matches(lines, pattern, multiline=False, search_text=None):
    """
    Pure match computation, no printing - returns a list of match
    descriptors, either ('single', line_no, line_text, spans) or
    ('range', start_line_no, end_line_no, [(line_no, line_text, spans), ...]).
    Kept separate from printing so a search can be tried silently
    (e.g. the comment-aware fallback chain, or one pass per file in a
    multi-file search) without spamming intermediate "no match" output.

    `search_text`, when given, is matched against instead of joining
    `lines` fresh (used to search a comment-stripped copy while still
    reporting positions against the real, unstripped `lines`).
    """
    results = []

    if not multiline:
        for idx, line in enumerate(lines, start=1):
            found = list(pattern.finditer(line))
            if found:
                results.append(("single", idx, line, [m.span() for m in found]))
        return results

    full_text = search_text if search_text is not None else "\n".join(lines)
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    for m in pattern.finditer(full_text):
        if m.start() == m.end():
            continue  # ignore degenerate empty matches
        start_line = bisect.bisect_right(offsets, m.start()) - 1
        end_line = bisect.bisect_right(offsets, m.end() - 1) - 1
        start_line = max(start_line, 0)
        end_line = max(end_line, start_line)

        if start_line == end_line:
            li = start_line
            local_start = m.start() - offsets[li]
            local_end = m.end() - offsets[li]
            results.append(("single", li + 1, lines[li], [(local_start, local_end)]))
        else:
            per_line = []
            for li in range(start_line, end_line + 1):
                line_text = lines[li]
                line_start_off = offsets[li]
                line_end_off = offsets[li] + len(line_text)
                span_start = max(m.start(), line_start_off) - line_start_off
                span_end = min(m.end(), line_end_off) - line_start_off
                spans = [(span_start, span_end)] if span_start < span_end else []
                per_line.append((li + 1, line_text, spans))
            results.append(("range", start_line + 1, end_line + 1, per_line))

    return results


def _print_matches(results, width, prefix=""):
    multiple = len(results) > 1
    for n, item in enumerate(results, start=1):
        if multiple:
            print(f"{prefix}{C_DIM}-- match {n}/{len(results)} --{R}")
        if item[0] == "single":
            _, line_no, line_text, spans = item
            print_match(line_no, line_text, spans, width, prefix=prefix)
        else:
            _, start_no, end_no, per_line = item
            print(f"{prefix}{C_LINE_NUM}Lines {start_no}-{end_no}:{R}")
            for li_no, line_text, spans in per_line:
                num_str = str(li_no).rjust(width)
                print(f"{prefix}  {C_LINE_NUM}{num_str}:{R} {highlight_line(line_text, spans)}")
        if multiple and n != len(results):
            print(prefix.rstrip() if prefix.strip() else "")


def _print_file_result_card(index, total, entry, results, width, note=None):
    """
    Renders one file's results as a bordered card - a title bar with
    the file's position/name/match count baked into the top border,
    the full path and any fallback note underneath, then the matches
    themselves indented inside the box. Files with zero matches get a
    single dim summary line instead of an empty card, so a search
    across many files doesn't bury the files that actually matched
    under a wall of empty boxes. The box width is re-measured against
    the terminal every call, so a resized/popup console never leaves
    stale, mismatched border lines - narrow paths get truncated in the
    middle instead so they don't wrap and break the box shape.
    """
    box_width = get_term_width()
    name = os.path.basename(entry.path)

    if not results:
        print(f"  {C_DIM}[{index}/{total}] {name} - no matches{R}")
        return

    count = len(results)
    label = f"{count} match" if count == 1 else f"{count} matches"
    plain_title = f" [{index}/{total}] {name} - {label} "
    dash_count = max(box_width - len(plain_title) - 1, 3)

    # Layered coloring instead of one flat bright color: a muted border
    # frames the card, the position index is dim (secondary info), the
    # filename is bold (what you're scanning for), and the match count
    # pops in green (the number that actually matters).
    colored_title = (f" {C_DIM}[{index}/{total}]{R} {C_TITLE}{name}{R} {C_DIM}-{R} "
                      f"{C_OK}{label}{R} ")

    print()
    print(f"{C_BORDER}{BOX_TL}{R}{colored_title}{C_BORDER}{BOX_H * dash_count}{BOX_TR}{R}")
    print(f"{C_BORDER}{BOX_V}{R} {C_DIM}{truncate_middle(entry.path, box_width - 2)}{R}")
    if note:
        print(f"{C_BORDER}{BOX_V}{R} {C_DIM}({note}){R}")
    print(f"{C_BORDER}{BOX_V}{R}")
    _print_matches(results, width, prefix=f"{C_BORDER}{BOX_V}{R}  ")
    print(f"{C_BORDER}{BOX_BL}{BOX_H * box_width}{BOX_BR}{R}")


def _print_file_search_header(term_label, file_count):
    print()
    hr("-", get_term_width(), C_BORDER)
    label_preview = term_label if len(term_label) <= 60 else term_label[:57] + "..."
    print(C_TITLE + f"Search results for: {label_preview!r} "
                     f"(across {file_count} file(s))" + R)
    hr("-", get_term_width(), C_BORDER)


def _print_file_search_footer(total_matches, files_with_matches, file_count):
    print()
    hr("-", get_term_width(), C_BORDER)
    if total_matches:
        print(C_OK + f"Found {total_matches} matching occurrence(s) in "
                      f"{files_with_matches}/{file_count} file(s)." + R)
    else:
        print(C_WARN + "No matches found in any loaded file." + R)
    hr("-", get_term_width(), C_BORDER)


def yes_no(prompt, default=False):
    suffix = "Y/n" if default else "y/N"
    try:
        ans = input(f"{C_PROMPT}{prompt} ({suffix}): {R}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


# --------------------------------------------------------------------------
# 1) FIND ONE LINE (exact literal match)
# --------------------------------------------------------------------------
def find_one_line(state):
    if not state.is_loaded():
        print(C_WARN + "No files loaded." + R)
        return
    auto_reload_check(state)

    print(C_DIM + "Tip: this is a literal match - include any comment "
                  "(e.g. // TODO or /* note */) in what you type if you "
                  "want lines to match only with that exact comment attached." + R)
    try:
        term = input(f"{C_PROMPT}Enter exact text to search for: {R}")
    except (EOFError, KeyboardInterrupt):
        return
    if not term:
        print(C_WARN + "Search term cannot be empty." + R)
        return

    # A one-line search should only ever have one line typed/pasted in.
    # If more is already queued right behind it, the user pasted a
    # multi-line block here by mistake - reject it and drain the extra
    # lines so they don't leak into the next prompt (e.g. silently
    # answering "Case sensitive?" or auto-triggering a Find Multiple
    # Lines search once back at the main menu).
    if _more_input_already_buffered():
        extra_lines = 0
        while _more_input_already_buffered():
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                break
            extra_lines += 1
        print(C_ERR + f"That's {1 + extra_lines} lines of code - Find One Line only "
                       f"accepts a single line. Use 2/multi (Find Multiple Lines) instead." + R)
        return

    case_sensitive = yes_no("Case sensitive search?", default=False)
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(term), flags)

    state.last_search = f"one: '{term}' (case_sensitive={case_sensitive})"

    _print_file_search_header(term, len(state.files))
    total_matches = 0
    files_with_matches = 0
    for i, entry in enumerate(state.files, start=1):
        width = len(str(max(len(entry.lines), 1)))
        results = _find_matches(entry.lines, pattern, multiline=False)
        if results:
            files_with_matches += 1
            total_matches += len(results)
        _print_file_result_card(i, len(state.files), entry, results, width)
    _print_file_search_footer(total_matches, files_with_matches, len(state.files))


# --------------------------------------------------------------------------
# 2) FIND MULTIPLE LINES (whitespace/blank-line-insensitive regex)
# --------------------------------------------------------------------------
COMMENT_MARKER_RE = re.compile(
    r"//[^\n]*|/\*.*?\*/|#[^\n]*|--[^\n]*|<!--.*?-->",
    flags=re.DOTALL,
)


def has_comments(text):
    """True if `text` contains a recognizable comment marker (//, /* */,
    #, --, or <!-- -->). Used to decide whether a similarity search should
    require the user's own comment text verbatim, or instead retry against
    a comment-stripped copy of the file if the plain search finds nothing."""
    return bool(COMMENT_MARKER_RE.search(text))


def strip_comments_preserve_layout(text):
    """
    Blanks out comment text - replacing every character of a comment
    except newlines with a space - while leaving everything else and all
    line breaks untouched. This keeps the stripped text exactly the same
    length and line layout as the original, so match offsets computed
    against it still map back to the correct line numbers in the real
    (unstripped) file content.
    """
    def _blank(m):
        return "".join(ch if ch == "\n" else " " for ch in m.group(0))

    return COMMENT_MARKER_RE.sub(_blank, text)


def build_flexible_pattern(user_text):
    """
    Tokenizes the user's input into words (\\w+) and individual
    non-space characters, escapes each token, and joins them with \\s*
    so that whitespace differences (extra spaces, tabs, or line breaks,
    or none at all) around punctuation like ( ) , ; are ignored during
    matching. Since \\s matches newlines by default, the same pattern
    transparently supports snippets that span multiple lines.
    """
    tokens = re.findall(r"\w+|\S", user_text, flags=re.MULTILINE)
    escaped = [re.escape(t) for t in tokens]
    return r"\s*".join(escaped)


def read_multiline_input(seed_line=None):
    """
    Collects one or more lines of input from the user.

    - A single line followed by one blank line finishes immediately (so a
      plain one-line search only needs Enter pressed twice).
    - Once a SECOND line has been entered, a SINGLE blank line is kept as
      part of the snippet (many real code blocks have a blank line in
      the middle) - but TWO blank lines in a row finish it, same "press
      Enter when you're done" intuition as the single-line case, just
      needing one extra Enter since the first blank might be content.
      This is purely content-based (counts consecutive blank lines you
      actually typed/pasted) rather than guessing from how fast more
      input arrives, so it can't race against paste timing.
    - Typing 'END' on its own line always finishes immediately too, for
      anyone who prefers being explicit.

    `seed_line`, if given, is treated as the first already-typed/pasted
    line (used when a paste at the main menu is auto-recovered into a
    search instead of being rejected as an unknown command) - the rest
    of the paste is still sitting in the input buffer and will be read
    normally by the loop below.
    """
    print(C_INFO + "Enter text/code to match (whitespace & line-break insensitive)." + R)
    print(C_DIM + "  - One line: type it, then press Enter on a blank line to search." + R)
    print(C_DIM + "  - Multiple lines: paste/type them, then press Enter twice "
                  "(or type END) to search." + R)
    collected = []
    last_was_blank = False
    if seed_line is not None and seed_line != "" and seed_line.strip().upper() != "END":
        collected.append(seed_line)
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            return None
        if line.strip().upper() == "END":
            break
        if line == "":
            if not collected:
                continue
            if len(collected) == 1:
                break
            if last_was_blank:
                break
            collected.append(line)
            last_was_blank = True
            continue
        collected.append(line)
        last_was_blank = False
    return "\n".join(collected)


def _search_file_with_comment_fallback(entry, pattern_as_typed, pattern_stripped_snippet):
    """
    Runs the comment-aware fallback chain against ONE loaded file,
    silently (no printing) so a multi-file search doesn't spam
    intermediate "no match" noise per file per attempt:
      1. As typed (comments and all, if any).
      2. If the snippet had comment(s), the snippet with its own
         comment(s) stripped - the user's comment text may just not
         match the file's wording.
      3. The FILE's comments stripped (matching only - the original,
         unstripped lines are still what gets reported/displayed).
    Returns (results, note) - `note` is None for a plain step-1 match,
    or a short string describing which fallback step actually matched.
    """
    full_text = "\n".join(entry.lines)

    results = _find_matches(entry.lines, pattern_as_typed, multiline=True, search_text=full_text)
    if results:
        return results, None

    if pattern_stripped_snippet is not None:
        results = _find_matches(entry.lines, pattern_stripped_snippet, multiline=True,
                                 search_text=full_text)
        if results:
            return results, "matched after removing comment(s) from your search text"

    stripped_file_text = strip_comments_preserve_layout(full_text)
    results = _find_matches(entry.lines, pattern_as_typed, multiline=True,
                             search_text=stripped_file_text)
    if results:
        return results, "matched after removing comments from the file"

    return [], None


def find_multiple_lines(state, seed_line=None):
    if not state.is_loaded():
        print(C_WARN + "No files loaded." + R)
        return
    auto_reload_check(state)

    snippet = read_multiline_input(seed_line=seed_line)
    if snippet is None or not snippet.strip():
        print(C_WARN + "Search snippet cannot be empty." + R)
        return

    case_sensitive = yes_no("Case sensitive search?", default=False)
    flags = 0 if case_sensitive else re.IGNORECASE

    try:
        pattern_as_typed = re.compile(build_flexible_pattern(snippet), flags)
    except re.error as e:
        print(C_ERR + f"Failed to build search pattern: {e}" + R)
        return

    # Comment-aware fallback (see _search_file_with_comment_fallback):
    # only build the "strip snippet comments" pattern once, up front,
    # if the snippet actually has comment(s) to strip.
    pattern_stripped_snippet = None
    if has_comments(snippet):
        stripped_snippet = COMMENT_MARKER_RE.sub("", snippet)
        if stripped_snippet.strip():
            try:
                pattern_stripped_snippet = re.compile(build_flexible_pattern(stripped_snippet), flags)
            except re.error:
                pattern_stripped_snippet = None

    state.last_search = f"multi: {snippet!r} (case_sensitive={case_sensitive})"

    _print_file_search_header(snippet, len(state.files))
    total_matches = 0
    files_with_matches = 0
    for i, entry in enumerate(state.files, start=1):
        width = len(str(max(len(entry.lines), 1)))
        results, note = _search_file_with_comment_fallback(
            entry, pattern_as_typed, pattern_stripped_snippet)
        if results:
            files_with_matches += 1
            total_matches += len(results)
        _print_file_result_card(i, len(state.files), entry, results, width, note=note)
    _print_file_search_footer(total_matches, files_with_matches, len(state.files))


# --------------------------------------------------------------------------
# 3) FIND LINE RANGE
# --------------------------------------------------------------------------
def parse_range(text, total_lines):
    text = text.strip().replace(" ", "")
    m = re.match(r"^(\d+)-(\d+)$", text)
    if not m:
        # Allow a single line number too.
        if text.isdigit():
            n = int(text)
            return n, n
        return None
    start, end = int(m.group(1)), int(m.group(2))
    if start > end:
        start, end = end, start
    start = max(start, 1)
    end = min(end, total_lines)
    return start, end


def select_loaded_file(state, verb="view"):
    """
    Returns the LoadedFile to operate on - auto-picks it if only one
    file is loaded, otherwise asks which of the loaded files to use
    (line ranges are inherently file-specific, unlike the text
    searches which naturally run across every loaded file at once).
    Returns None if the user cancels.
    """
    if len(state.files) == 1:
        return state.files[0]

    print()
    hr("-", get_term_width(), C_BORDER)
    print(C_TITLE + f"Which loaded file do you want to {verb}?" + R)
    for i, entry in enumerate(state.files, start=1):
        print(f"  {C_ACCENT}[{i}]{R} {entry.path}  {C_DIM}({len(entry.lines)} lines){R}")
    hr("-", get_term_width(), C_BORDER)

    while True:
        try:
            choice = input(f"{C_PROMPT}Enter number (or 'b' to cancel): {R}").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if choice.lower() in ("b", "back", "cancel"):
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(state.files):
            return state.files[int(choice) - 1]
        print(C_ERR + "Invalid selection. Try again." + R)


def remove_file(state):
    """
    Removes one or more currently loaded files from `state` without
    touching anything on disk - just drops them from the in-memory
    working set. Accepts a single number, comma-separated numbers
    ('1,3'), 'all' to clear everything, or 'b' to cancel.
    """
    if not state.is_loaded():
        print(C_WARN + "No files loaded." + R)
        return

    print()
    hr("-", get_term_width(), C_BORDER)
    print(C_TITLE + "REMOVE FILE(S) FROM CURRENT SET" + R)
    print_loaded_files_matrix(state.files, f"Currently loaded ({len(state.files)})",
                               include_size=False, include_modified=False)
    hr("-", get_term_width(), C_BORDER)

    try:
        choice = input(f"{C_PROMPT}Enter number(s) to remove, 'all', "
                        f"or 'b' to cancel: {R}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return

    if not choice or choice.lower() in ("b", "back", "cancel"):
        print(C_WARN + "Cancelled - nothing removed." + R)
        return

    if choice.lower() == "all":
        if yes_no(f"Remove ALL {len(state.files)} loaded file(s)?", default=False):
            state.clear_files()
            print(C_OK + "Removed all loaded files." + R)
        else:
            print(C_WARN + "Cancelled - nothing removed." + R)
        return

    parts = [p.strip() for p in choice.split(",") if p.strip()]
    bad = [p for p in parts if not (p.isdigit() and 1 <= int(p) <= len(state.files))]
    if not parts or bad:
        print(C_ERR + "Invalid selection - nothing removed." + R)
        return

    indices = sorted({int(p) - 1 for p in parts}, reverse=True)
    removed_names = []
    for idx in indices:
        removed_names.append(os.path.basename(state.files[idx].path))
        del state.files[idx]

    state.reset_search()
    print(C_OK + f"Removed {len(removed_names)} file(s): {', '.join(removed_names)}" + R)
    if not state.files:
        print(C_WARN + "No files remain loaded - use 'add' or 'change' to load new ones." + R)


def reset_state_flow(state):
    """
    Handles the 'reset' menu command. Always warns before doing
    anything, then lets the user choose how far the reset goes:
      - just clear the remembered last search (state stays as-is)
      - clear the remembered search AND unload every currently
        loaded file, relaunching straight back into file selection
    Returns "change" if the caller should return to file selection
    (all files were cleared), or None if nothing needs to change.
    """
    print()
    hr("-", get_term_width(), C_BORDER)
    print(C_WARN + "WARNING: Reset will clear the current search state." + R)
    print(C_DIM + "This cannot be undone - your last search term/snippet will "
                  "be forgotten." + R)
    hr("-", get_term_width(), C_BORDER)

    if not yes_no("Continue with reset?", default=False):
        print(C_WARN + "Reset cancelled." + R)
        return None

    if not state.is_loaded():
        state.reset_search()
        print(C_OK + "Search state reset." + R)
        return None

    full = yes_no(
        f"Also remove ALL {len(state.files)} loaded file(s) and relaunch "
        f"file selection? (No = just reset search state)",
        default=False,
    )

    if full:
        state.clear_files()
        print(C_OK + "All loaded files removed - relaunching file selection." + R)
        return "change"

    state.reset_search()
    print(C_OK + "Search state reset." + R)
    return None


def show_line_range(state):
    if not state.is_loaded():
        print(C_WARN + "No files loaded." + R)
        return
    auto_reload_check(state)

    entry = select_loaded_file(state, verb="show a line range from")
    if entry is None:
        return
    lines = entry.lines
    total_lines = len(lines)

    try:
        raw = input(f"{C_PROMPT}Enter line range (e.g. 1500-3000), "
                    f"'{os.path.basename(entry.path)}' has {total_lines} lines: {R}")
    except (EOFError, KeyboardInterrupt):
        return

    parsed = parse_range(raw, total_lines)
    if not parsed:
        print(C_ERR + "Invalid range format. Use e.g. '1500-3000'." + R)
        return

    start, end = parsed
    if start > total_lines or end < 1:
        print(C_ERR + f"Range out of bounds. File only has {total_lines} lines." + R)
        return

    width = len(str(total_lines))
    line_span = end - start + 1

    print()
    hr("-", get_term_width(), C_BORDER)
    print(C_TITLE + f"{os.path.basename(entry.path)}: lines {start}-{end} ({line_span} lines)" + R)
    hr("-", get_term_width(), C_BORDER)

    pos = start
    while pos <= end:
        chunk_end = min(pos + MAX_RANGE_CHUNK - 1, end)
        for i in range(pos, chunk_end + 1):
            num_str = str(i).rjust(width)
            print(f"{C_LINE_NUM}{num_str}:{R} {lines[i - 1]}")
        pos = chunk_end + 1
        if pos <= end:
            print()
            print(C_WARN + f"-- {chunk_end - start + 1}/{line_span} lines shown. "
                            f"{end - chunk_end} remaining. --" + R)
            cont = yes_no("Show next chunk?", default=True)
            if not cont:
                break

    hr("-", get_term_width(), C_BORDER)


# --------------------------------------------------------------------------
# GUI FILE PICKER
# --------------------------------------------------------------------------
def gui_file_picker():
    """
    Opens the native OS file dialog with multi-select enabled (hold
    Ctrl or Shift to pick several at once) - returns a list of chosen
    paths, empty if the user cancelled or no file was picked.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        print(C_ERR + "tkinter is not available in this Python installation." + R)
        return []

    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        paths = filedialog.askopenfilenames(title="Select file(s) to search "
                                                    "(Ctrl/Shift-click for multiple)")
        root.destroy()
        return list(paths)
    except Exception as e:
        print(C_ERR + f"GUI file picker failed: {e}" + R)
        return []


# --------------------------------------------------------------------------
# FILE SELECTION MODULE
# --------------------------------------------------------------------------
def truncate_middle(text, max_len):
    """Shortens `text` to `max_len` by cutting out its middle, keeping the
    start and end intact (e.g. 'D:\\Apps...\\backup\\SVG_Studio.cpp') so
    near-duplicate paths stay distinguishable even when truncated."""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    keep = max_len - 3
    head = keep // 2
    tail = keep - head
    return text[:head] + "..." + text[-tail:]


def print_loaded_files_matrix(entries, title, include_size=True, include_modified=True):
    """
    Renders a list of LoadedFile entries as a clean, column-aligned
    table instead of a stacked bullet list - stays readable once
    there are 5+ files loaded, unlike '* name / path' repeated per
    file. `include_size`/`include_modified` trim the table down for
    contexts where those columns aren't worth the width (e.g. the
    compact header shown on every screen redraw).
    """
    print(C_TITLE + title + R)

    rows = []
    for entry in entries:
        size = "N/A"
        modified = "N/A"
        if include_size or include_modified:
            try:
                st = os.stat(entry.path)
                size = format_size(st.st_size)
                modified = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                pass
        rows.append({
            "name": os.path.basename(entry.path),
            "size": size,
            "lines": f"{len(entry.lines):,}",
            "encoding": entry.encoding or "?",
            "modified": modified,
            "path": entry.path,
        })

    num_w = len(f"[{len(rows)}]")
    name_w = min(max((len(r["name"]) for r in rows), default=4), 28)
    lines_w = max(max((len(r["lines"]) for r in rows), default=0), len("Lines"))
    enc_w = min(max((len(r["encoding"]) for r in rows), default=8), 14)
    size_w = max(max((len(r["size"]) for r in rows), default=0), len("Size"))
    mod_w = max(max((len(r["modified"]) for r in rows), default=0), len("Modified"))

    fixed_w = num_w + 1 + name_w + 1 + lines_w + 1 + enc_w + 1
    if include_size:
        fixed_w += size_w + 1
    if include_modified:
        fixed_w += mod_w + 1

    term_width = shutil.get_terminal_size(fallback=(100, 24)).columns
    path_w = max(term_width - fixed_w - 1, 20)

    header_bits = [f"{'#'.ljust(num_w)}", f"{'Name'.ljust(name_w)}"]
    if include_size:
        header_bits.append(f"{'Size'.rjust(size_w)}")
    header_bits.append(f"{'Lines'.rjust(lines_w)}")
    header_bits.append(f"{'Encoding'.ljust(enc_w)}")
    if include_modified:
        header_bits.append(f"{'Modified'.ljust(mod_w)}")
    header_bits.append("Path")
    header = " ".join(header_bits)

    print(C_ACCENT + header + R)
    print(C_DIM + ("-" * min(term_width, len(header) + path_w)) + R)

    for i, r in enumerate(rows, start=1):
        bits = [f"{C_ACCENT}{f'[{i}]'.ljust(num_w)}{R}",
                truncate_middle(r["name"], name_w).ljust(name_w)]
        if include_size:
            bits.append(r["size"].rjust(size_w))
        bits.append(r["lines"].rjust(lines_w))
        bits.append(truncate_middle(r["encoding"], enc_w).ljust(enc_w))
        if include_modified:
            bits.append(r["modified"].ljust(mod_w))
        bits.append(f"{C_DIM}{truncate_middle(r['path'], path_w)}{R}")
        print(" ".join(bits))


def get_quick_file_stats(path):
    """
    Cheap, best-effort metadata for the file-picker matrix: size, an
    approximate line count (raw newline count - no encoding detection,
    so this stays fast even for huge files), and last-modified time.
    Any field that can't be read falls back to 'N/A' rather than raising.
    """
    stats = {"size": "N/A", "lines": "N/A", "modified": "N/A"}
    try:
        st = os.stat(path)
        stats["size"] = format_size(st.st_size)
        stats["modified"] = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
    except OSError:
        pass

    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        line_count = raw.count(b"\n")
        if raw and not raw.endswith(b"\n"):
            line_count += 1
        stats["lines"] = f"{line_count:,}"
    except OSError:
        pass

    return stats


def prompt_numbered_selection(matches):
    """
    Displays matched files as a size/lines/path matrix. Accepts a single
    number, several comma-separated numbers (e.g. '1,3,5') to load
    multiple files at once, or 'b' to cancel. Returns a list of chosen
    paths (empty if cancelled).
    """
    print()
    hr("-", get_term_width(), C_BORDER)
    print(C_TITLE + f"Found {len(matches)} matching file(s) - pick one or more:" + R)
    hr("-", get_term_width(), C_BORDER)

    rows = []
    for path in matches:
        stats = get_quick_file_stats(path)
        rows.append((os.path.basename(path), stats["size"], stats["lines"], stats["modified"], path))

    num_w = len(f"[{len(rows)}]")
    name_w = min(max((len(r[0]) for r in rows), default=4), 28)
    size_w = max(max((len(r[1]) for r in rows), default=0), len("Size"))
    lines_w = max(max((len(r[2]) for r in rows), default=0), len("Lines"))
    mod_w = max(max((len(r[3]) for r in rows), default=0), len("Modified"))

    term_width = shutil.get_terminal_size(fallback=(100, 24)).columns
    fixed_w = num_w + 1 + name_w + 1 + size_w + 1 + lines_w + 1 + mod_w + 1
    path_w = max(term_width - fixed_w - 1, 20)

    header = (f"{'#'.ljust(num_w)} {'Name'.ljust(name_w)} {'Size'.rjust(size_w)} "
              f"{'Lines'.rjust(lines_w)} {'Modified'.ljust(mod_w)} Location")
    print(C_ACCENT + header + R)
    print(C_DIM + ("-" * min(term_width, len(header) + path_w)) + R)

    for i, (name, size, lines, modified, path) in enumerate(rows, start=1):
        disp_name = truncate_middle(name, name_w)
        disp_path = truncate_middle(path, path_w)
        num_str = f"[{i}]".ljust(num_w)
        print(f"{C_ACCENT}{num_str}{R} {disp_name.ljust(name_w)} {size.rjust(size_w)} "
              f"{lines.rjust(lines_w)} {modified.ljust(mod_w)} {C_DIM}{disp_path}{R}")

    hr("-", get_term_width(), C_BORDER)

    while True:
        try:
            choice = input(f"{C_PROMPT}Enter number(s), comma-separated for "
                           f"multiple (or 'b' to go back): {R}").strip()
        except (EOFError, KeyboardInterrupt):
            return []
        if choice.lower() in ("b", "back", "cancel"):
            return []

        parts = [p.strip() for p in choice.split(",") if p.strip()]
        if not parts:
            print(C_ERR + "Invalid selection. Try again." + R)
            continue

        bad = [p for p in parts if not (p.isdigit() and 1 <= int(p) <= len(matches))]
        if bad:
            print(C_ERR + f"Invalid selection(s): {', '.join(bad)}. Try again." + R)
            continue

        seen = set()
        chosen = []
        for p in parts:
            path = matches[int(p) - 1]
            if path not in seen:
                seen.add(path)
                chosen.append(path)
        return chosen


def _load_many(paths, state):
    """Adds each path in `paths` to `state`, printing a result line per
    file. Returns True if at least one loaded successfully."""
    any_ok = False
    for path in paths:
        ok, msg = add_file_to_state(path, state)
        print((C_OK if ok else C_ERR) + msg + R)
        any_ok = any_ok or ok
    return any_ok


def _prompt_add_another(state):
    """After successfully loading file(s), asks whether to keep adding
    more. Returns True to stay in the selection loop, False to proceed
    to the main menu."""
    press_enter()
    return yes_no(f"Add another file? ({len(state.files)} loaded so far)", default=False)


def file_selection_loop(state, standalone=True):
    """
    Interactive loop to select one or more files - multiple files can
    be added via ';'-separated paths, comma-separated numbers from a
    name search's match list, or by looping back and adding more one
    at a time. Returns True once ready to move on to the main menu.

    `standalone=True` (default) is the initial pre-menu file picker -
    'exit'/Ctrl+C there quits the whole program. `standalone=False` is
    used for the '6/add' mid-session flow to add more files onto an
    already-loaded set - 'exit'/Ctrl+C there just cancels back to the
    main menu instead, leaving the existing files untouched.
    """
    while True:
        print()
        hr("-", get_term_width(), C_BORDER)
        print(C_TITLE + ("FILE SELECTION" if standalone else "ADD MORE FILES") + R)
        if state.files:
            print(C_OK + f"  {len(state.files)} file(s) loaded so far." + R)
        print(f"  {C_ACCENT}Paste/drag file path(s){R} directly - separate multiple with ';'")
        print(f"  {C_ACCENT}pick{R}    - open GUI file picker")
        print(f"  {C_ACCENT}<name>{R}  - search current directory for a filename")
        print(f"  {C_ACCENT}info{R}    - show info of currently loaded file(s)")
        exit_hint = "quit the program" if standalone else "cancel, return to main menu"
        print(f"  {C_ACCENT}exit{R}    - {exit_hint}")
        hr("-", get_term_width(), C_BORDER)

        try:
            raw = smart_input(f"{C_PROMPT}> {R}", _file_select_candidates)
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        cmd = strip_quotes(raw)
        if not cmd:
            continue

        lowered = cmd.lower()

        if lowered in ("exit", "quit"):
            return False

        if lowered == "info":
            display_info(state)
            press_enter()
            continue

        if lowered in ("pick", "browse", "gui"):
            paths = gui_file_picker()
            if not paths:
                print(C_WARN + "No file selected." + R)
                continue
            if _load_many(paths, state) and not _prompt_add_another(state):
                return True
            continue

        # Multiple paths at once, separated by ';'.
        if ";" in cmd:
            parts = [strip_quotes(p) for p in cmd.split(";")]
            parts = [p for p in parts if p]
            resolved = []
            for p in parts:
                candidate = os.path.expanduser(os.path.expandvars(p))
                if os.path.isfile(candidate):
                    resolved.append(candidate)
                else:
                    print(C_ERR + f"Not found, skipped: {p}" + R)
            if resolved and _load_many(resolved, state) and not _prompt_add_another(state):
                return True
            continue

        # Direct single path attempt (absolute or relative).
        candidate = os.path.expanduser(os.path.expandvars(cmd))
        if os.path.isfile(candidate):
            if _load_many([candidate], state) and not _prompt_add_another(state):
                return True
            continue

        if os.path.isdir(candidate):
            print(C_ERR + "That is a directory, not a file." + R)
            continue

        # Fall back to recursive filename search, scoped to the cwd first.
        print(C_INFO + f"'{cmd}' not found directly - searching "
                        f"subdirectories of {os.getcwd()} ..." + R)
        matches = search_file_by_name(cmd)

        if not matches:
            print(C_WARN + "No matches under the current directory." + R)
            expand = yes_no(
                "Search your entire system (all drives) instead? This can take a while",
                default=False,
            )

            if not expand:
                print(C_WARN + "Please try again (or type 'exit')." + R)
                continue

            def _announce(root):
                print(C_DIM + f"  Scanning {root} ..." + R)

            print(C_INFO + f"Searching the whole system for '{cmd}' ..." + R)
            matches = search_file_system_wide(cmd, on_root_start=_announce)

            if not matches:
                print(C_WARN + "No matching files found anywhere on this system. "
                                "Please try again (or type 'exit')." + R)
                continue

        if len(matches) == 1:
            chosen = matches
            print(C_OK + f"Auto-loading only match: {matches[0]}" + R)
        else:
            chosen = prompt_numbered_selection(matches)
            if not chosen:
                continue

        if _load_many(chosen, state) and not _prompt_add_another(state):
            return True


# --------------------------------------------------------------------------
# MAIN COMMAND MENU
# --------------------------------------------------------------------------
def print_file_header(state):
    display_cap = 8
    shown = state.files[:display_cap]
    print_loaded_files_matrix(shown, f"ACTIVE FILES ({len(state.files)})",
                               include_size=False, include_modified=False)
    remaining = len(state.files) - display_cap
    if remaining > 0:
        print(C_DIM + f"  ... and {remaining} more (see 4/info for the full list)" + R)


def print_main_menu():
    hr("-", get_term_width(), C_BORDER)
    print(C_TITLE + "MAIN MENU" + R)
    print(f"  {C_ACCENT}1{R}/{C_ACCENT}one{R}      Find One Line (exact literal match)")
    print(f"  {C_ACCENT}2{R}/{C_ACCENT}multi{R}    Find Multiple Lines (whitespace/blank-line insensitive)")
    print(f"  {C_ACCENT}3{R}/{C_ACCENT}lines{R}    Find Line Range")
    print(f"  {C_ACCENT}4{R}/{C_ACCENT}info{R}     File Info")
    print(f"  {C_ACCENT}5{R}/{C_ACCENT}change{R}   Clear all & pick new file(s)")
    print(f"  {C_ACCENT}6{R}/{C_ACCENT}add{R}      Add more files (keeps current set)")
    print(f"  {C_ACCENT}7{R}/{C_ACCENT}remove{R}   Remove loaded file(s) from the current set")
    print(f"  {C_ACCENT}8{R}/{C_ACCENT}clear{R}    Clear Screen")
    print(f"  {C_ACCENT}9{R}/{C_ACCENT}reset{R}    Reset search state (or clear everything / relaunch)")
    print(f"  {C_ACCENT}10{R}/{C_ACCENT}reload{R}  Reload all loaded files from disk now")
    print(f"  {C_ACCENT}11{R}/{C_ACCENT}exit{R}    Exit")
    hr("-", get_term_width(), C_BORDER)


def redraw_active_screen(state):
    show_banner()
    print_file_header(state)
    print()
    print_main_menu()


def command_loop(state):
    """
    Runs the main command interface for an already-loaded file.
    Returns 'change' to go back to file selection, or 'exit' to quit.
    """
    redraw_active_screen(state)

    while True:
        try:
            raw = smart_input(f"{C_PROMPT}> {R}", _main_menu_candidates).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return "exit"

        cmd = raw.lower()

        if cmd in ("1", "one", "exact"):
            find_one_line(state)
            press_enter()
            redraw_active_screen(state)

        elif cmd in ("2", "multi", "multiple", "similar"):
            find_multiple_lines(state)
            press_enter()
            redraw_active_screen(state)

        elif cmd in ("3", "lines"):
            show_line_range(state)
            press_enter()
            redraw_active_screen(state)

        elif cmd in ("4", "info"):
            display_info(state)
            press_enter()
            redraw_active_screen(state)

        elif cmd in ("5", "change"):
            state.clear_files()
            return "change"

        elif cmd in ("6", "add"):
            file_selection_loop(state, standalone=False)
            redraw_active_screen(state)

        elif cmd in ("7", "remove"):
            remove_file(state)
            press_enter()
            if not state.files:
                return "change"
            redraw_active_screen(state)

        elif cmd in ("8", "clear"):
            redraw_active_screen(state)

        elif cmd in ("9", "reset"):
            action = reset_state_flow(state)
            if action == "change":
                return "change"
            redraw_active_screen(state)

        elif cmd in ("10", "reload"):
            force_reload_files(state)
            press_enter()
            redraw_active_screen(state)

        elif cmd in ("11", "exit", "quit"):
            return "exit"

        elif not cmd:
            continue

        else:
            # Not a recognized menu command - most likely a code snippet
            # that was dragged/pasted straight in (possibly multi-line,
            # with the remaining lines already buffered on stdin) without
            # first selecting option 2. Recover gracefully instead of
            # rejecting every pasted line one by one.
            print(C_INFO + "Not a menu command - treating this as a "
                            "Find Multiple Lines search snippet." + R)
            find_multiple_lines(state, seed_line=raw)
            press_enter()
            redraw_active_screen(state)


# --------------------------------------------------------------------------
# ENTRY POINT
# --------------------------------------------------------------------------
def main():
    _set_console_icon()
    show_banner()
    state = FileState()

    try:
        while True:
            loaded = file_selection_loop(state)
            if not loaded:
                break

            result = command_loop(state)
            if result == "exit":
                break
            # result == "change" -> loop back to file_selection_loop
    except KeyboardInterrupt:
        pass
    except Exception as e:
        # Last-resort net for anything not already handled closer to
        # where it happened (a locked/network drive dropping mid-scan,
        # an out-of-memory on a huge file, etc.) - fail with a clear,
        # colored one-liner instead of dumping a raw Python traceback.
        print()
        print(C_ERR + f"Unexpected error: {type(e).__name__}: {e}" + R)
        print(C_WARN + "Exiting cleanly rather than crashing." + R)
    finally:
        print()
        w = get_term_width()
        hr("=", w, C_BORDER)
        print(C_OK + "Thanks for using CodeLens CLI - happy hunting!".center(w) + R)
        hr("=", w, C_BORDER)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Absolute last resort: something went wrong even outside the
        # main loop (e.g. during the very first banner/state setup).
        print(f"\nFatal error: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
