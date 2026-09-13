#!/usr/bin/env python3
"""
CodeLens.py - CodeLens GUI Edition entry point.

A modern glass-styled Qt desktop front-end for the same search engine
as CodeLensCLI.py (exact one-line search, whitespace-insensitive
multi-line search, line ranges, encoding-aware auto-reloading file
loading). All parsing/search logic is imported from CodeLensCLI.py so
every front-end shares one engine - only presentation lives here.

This file is just the process entry point (DPI awareness must run
before anything creates a window) - the actual implementation is split
across:
  theme.py           - colors, stylesheet, icon helpers
  widgets.py         - small reusable custom widgets
  dialogs.py         - popup dialogs and the settings panel
  persistence.py     - shortcuts.json / recent_folders.json
  results_render.py  - results-pane HTML building
  main_window.py     - FinderWindow, the main application window
  app.py             - splash screen + main()

Icons: real vector files under Assets/DarkModeIcons/ and
Assets/LightModeIcons/ (same filenames in both, different palette).
Qt loads SVG natively via QIcon/QSvgRenderer - no rasterizer
dependency needed. Accent-role icons are re-tinted at runtime to match
whatever custom accent color the user picks in Settings.

Author: Robin Gupta
Run:    python CodeLens.py
"""

import ctypes
import sys

if sys.platform == "win32":
    # This process declares no DPI awareness anywhere (no manifest, no
    # SetProcessDpiAwareness call) - confirmed by grepping the whole file
    # and the PyInstaller spec. On a scaled display, Windows then falls
    # back to stretching this app's whole window as a bitmap for display
    # while still doing input hit-testing against its real, unscaled
    # coordinates - so a window can visually render in one place while
    # only being clickable at a different, scaled-mismatched position.
    # That reproduces exactly what was observed on a real scaled display:
    # a dialog that's visibly present and correctly positioned by every
    # internal (unscaled) coordinate check, keyboard-navigable, but whose
    # buttons don't respond to a real mouse click at the spot the user
    # actually sees them - indistinguishable from a total freeze. Must be
    # set before QApplication (or any window) is created; must degrade
    # silently on non-Windows/older Windows since ctypes.windll and
    # shcore/PROCESS_PER_MONITOR_DPI_AWARE_V2 aren't universally available.
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass

from app import main

if __name__ == "__main__":
    main()
