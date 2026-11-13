"""Invokes the real Windows Explorer shell context menu (IContextMenu) for a
file or folder, via ctypes COM interop - no pywin32/comtypes dependency,
consistent with the rest of this codebase (see CodeLensCLI.py's use of
ctypes/winreg directly).

This is what lets right-click in CodeLens surface actual OS/shell-extension
entries (Send to, Git, 7-Zip, antivirus scan, Properties, ...) instead of the
custom QMenu silently hiding them. Only single-item invocation is supported
(the common case: one file/folder under the cursor); multi-select still uses
the app's own QMenu actions.

Caveat: menu items that rely on owner-draw callbacks (WM_MEASUREITEM /
WM_DRAWITEM forwarded to the shell extension, used by a handful of icon-heavy
extensions) are not specially handled here since we don't subclass a real
HWND message loop for it - they still render, just without custom drawing.
"""
import ctypes
import os
import tempfile
from ctypes import wintypes

ole32 = ctypes.oledll.ole32
shell32 = ctypes.oledll.shell32
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

HRESULT = ctypes.c_long
LPVOID = ctypes.c_void_p
UINT = ctypes.c_uint
DWORD = wintypes.DWORD

# CMF_EXPLORE tags the request as coming from Explorer's old two-pane tree
# view; it's a pre-Vista flag that some shell extensions use to pick their
# legacy verb set/ordering. CMF_ITEMMENU is what Explorer itself passes for
# an ordinary single-item right-click, which is what puts extensions on the
# same modern-menu code path Explorer uses (trimmed verb set, "Show more
# options" folded away instead of listing every legacy verb inline).
CMF_NORMAL = 0x00000000
CMF_ITEMMENU = 0x00000008
TPM_LEFTALIGN = 0x0000
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002


# Rounded corners/shadow on a Win32 popup menu are a DWM behavior that only
# applies to *themed* (visual-styles-active) windows - an app with no
# comctl32 v6 manifest gets the flat, square, colorless "classic" menu
# renderer regardless of Windows version, which is the Windows-10-looking
# fallback this fixes. PyInstaller/plain-python.exe processes don't carry
# that manifest by default, so activate one at runtime instead of requiring
# a build-time change. Activated once and left on for the process lifetime.
_COMCTL_MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <dependency>
    <dependentAssembly>
      <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls"
        version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*" />
    </dependentAssembly>
  </dependency>
</assembly>
"""


class ACTCTXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("dwFlags", DWORD),
        ("lpSource", wintypes.LPCWSTR),
        ("wProcessorArchitecture", wintypes.WORD),
        ("wLangId", wintypes.WORD),
        ("lpAssemblyDirectory", wintypes.LPCWSTR),
        ("lpResourceName", wintypes.LPCWSTR),
        ("lpApplicationName", wintypes.LPCWSTR),
        ("hModule", wintypes.HMODULE),
    ]


_visual_styles_active = False


def _ensure_visual_styles():
    global _visual_styles_active
    if _visual_styles_active:
        return
    _visual_styles_active = True  # only try once even if it fails
    try:
        manifest_path = os.path.join(tempfile.gettempdir(), "codelens_comctl_v6.manifest")
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(_COMCTL_MANIFEST)

        ctx = ACTCTXW()
        ctx.cbSize = ctypes.sizeof(ACTCTXW)
        ctx.lpSource = manifest_path
        handle = kernel32.CreateActCtxW(ctypes.byref(ctx))
        if handle == -1:
            return
        cookie = ctypes.c_size_t()
        kernel32.ActivateActCtx(handle, ctypes.byref(cookie))
        # Left active deliberately: no ReleaseActCtx/DeactivateActCtx call,
        # so every later menu in the process keeps themed rendering too.
    except OSError:
        pass


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, guid_str):
        super().__init__()
        ole32.CLSIDFromString(ctypes.c_wchar_p(guid_str), ctypes.byref(self))


IID_IShellFolder = GUID("{000214E6-0000-0000-C000-000000000046}")
IID_IContextMenu = GUID("{000214E4-0000-0000-C000-000000000046}")


class CMINVOKECOMMANDINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("fMask", DWORD),
        ("hwnd", wintypes.HWND),
        ("lpVerb", LPVOID),
        ("lpParameters", ctypes.c_char_p),
        ("lpDirectory", ctypes.c_char_p),
        ("nShow", ctypes.c_int),
        ("dwHotKey", DWORD),
        ("hIcon", LPVOID),
    ]


def _com_method(interface_ptr, index, restype, *argtypes):
    """Look up and bind the `index`-th vtable slot of a raw COM interface
    pointer (a ctypes.c_void_p), since we don't have typed interface classes
    (comtypes/pywin32) available - see module docstring."""
    vtbl = ctypes.cast(interface_ptr, ctypes.POINTER(ctypes.c_void_p))[0]
    func_ptr = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))[index]
    func_type = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    bound = func_type(func_ptr)
    return lambda *args: bound(interface_ptr, *args)


def _release(interface_ptr):
    if interface_ptr:
        _com_method(interface_ptr, 2, ctypes.c_ulong)()


def show_native_context_menu(hwnd, path, x, y):
    """Shows the OS's own shell context menu for a single file/folder `path`
    at screen coordinates (x, y) and invokes whichever command the user
    picks. Returns True if the menu was shown (regardless of whether the
    user picked something or dismissed it), False if it couldn't be built
    (e.g. the path no longer exists) - callers should fall back to their own
    menu in that case.
    """
    _ensure_visual_styles()
    com_initialized = ole32.CoInitializeEx(None, 0x2) == 0  # COINIT_APARTMENTTHREADED
    pidl = LPVOID()
    shell_folder = LPVOID()
    child_pidl = LPVOID()
    context_menu = LPVOID()
    hmenu = None
    try:
        hr = shell32.SHParseDisplayName(ctypes.c_wchar_p(path), None, ctypes.byref(pidl), 0, None)
        if hr != 0 or not pidl:
            return False

        hr = shell32.SHBindToParent(pidl, ctypes.byref(IID_IShellFolder),
                                     ctypes.byref(shell_folder), ctypes.byref(child_pidl))
        if hr != 0 or not shell_folder:
            return False

        # IShellFolder::GetUIObjectOf(hwndOwner, cidl, apidl, riid, rgfReserved, ppv)
        get_ui_object_of = _com_method(
            shell_folder, 10, HRESULT,
            wintypes.HWND, UINT, ctypes.POINTER(LPVOID), ctypes.POINTER(GUID),
            ctypes.POINTER(UINT), ctypes.POINTER(LPVOID))
        child_array = (LPVOID * 1)(child_pidl)
        hr = get_ui_object_of(hwnd, 1, ctypes.cast(child_array, ctypes.POINTER(LPVOID)),
                               ctypes.byref(IID_IContextMenu), None, ctypes.byref(context_menu))
        if hr != 0 or not context_menu:
            return False

        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return False

        # IContextMenu::QueryContextMenu(hmenu, indexMenu, idCmdFirst, idCmdLast, uFlags)
        query_context_menu = _com_method(context_menu, 3, HRESULT, wintypes.HMENU,
                                          UINT, UINT, UINT, UINT)
        query_context_menu(hmenu, 0, 1, 0x7FFF, CMF_NORMAL | CMF_ITEMMENU)

        cmd = user32.TrackPopupMenuEx(
            hmenu, TPM_LEFTALIGN | TPM_RETURNCMD | TPM_RIGHTBUTTON, x, y, hwnd, None)

        if cmd:
            invoke_command = _com_method(context_menu, 4, HRESULT,
                                          ctypes.POINTER(CMINVOKECOMMANDINFO))
            info = CMINVOKECOMMANDINFO()
            info.cbSize = ctypes.sizeof(CMINVOKECOMMANDINFO)
            info.fMask = 0
            info.hwnd = hwnd
            info.lpVerb = ctypes.c_void_p(cmd - 1)  # MAKEINTRESOURCEA(id)
            info.nShow = 1  # SW_SHOWNORMAL
            invoke_command(ctypes.byref(info))
        return True
    finally:
        if hmenu:
            user32.DestroyMenu(hmenu)
        _release(context_menu)
        _release(shell_folder)
        if pidl:
            ole32.CoTaskMemFree(pidl)
        if com_initialized:
            ole32.CoUninitialize()
