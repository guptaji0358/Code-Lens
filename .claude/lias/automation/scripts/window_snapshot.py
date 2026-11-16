"""Project-local write (risk 1): snapshot a window to PNG and extract pixel facts. JSON stdin -> JSON stdout.

Python does the mechanical part (capture + measuring); judging what the pixels *mean* is left to the agent.
Windows only, stdlib only (ctypes PrintWindow, zlib PNG). Writes PNGs under <project>/build/snapshots/.

Input (all optional):
  title_contains  window-title substring to find (default "CodeLens")
  title_exclude   list of substrings to skip (default: editors/terminals/Claude)
  launch          true -> start `python scripts/CodeLens.py` if no window is found, wait up to wait_s (default 15)
  name            output file stem (default "snapshot")
  baseline        PNG path (relative to build/snapshots) to compare against
  save_baseline   true -> also store this capture as the baseline
  regions         [{"name","x","y","w","h"}] -> mean luminance, dominant colour, diff vs baseline per region
  pixels          [[x, y], ...] -> exact RGB at those points
Output: path, size, blank flag, top colours, mean luminance, baseline diff, region/pixel facts.
"""
import ctypes
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from ctypes import wintypes

ROOT = os.environ.get("LIAS_PROJECT_ROOT", os.getcwd())
OUT = os.path.join(ROOT, "build", "snapshots")
DEFAULT_EXCLUDE = ["Claude", "PowerShell", "Command Prompt", "Terminal", "Visual Studio Code", "Explorer"]
TOL = 8
MAX_SAMPLES = 250000

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


def find_window(contains, exclude):
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                t = buf.value
                if contains.lower() in t.lower() and not any(x.lower() in t.lower() for x in exclude):
                    r = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(r))
                    if r.right - r.left >= 400 and r.bottom - r.top >= 300 and not user32.IsIconic(hwnd):
                        found.append(((r.right - r.left) * (r.bottom - r.top), hwnd, t))  # skips splash/tiny
        return True

    user32.EnumWindows(proc(cb), 0)
    found.sort(reverse=True)
    return found[0] if found else None


def capture(hwnd):
    """Return (width, height, rgb bytes) via PrintWindow(PW_RENDERFULLCONTENT)."""
    if user32.IsIconic(hwnd):
        raise RuntimeError("window is minimized")
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        raise RuntimeError("window has empty size")
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    ok = user32.PrintWindow(hwnd, mdc, 2)
    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth, bi.biHeight, bi.biPlanes, bi.biBitCount = w, -h, 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    if not ok:
        raise RuntimeError("PrintWindow failed")
    bgra = buf.raw
    rgb = bytearray(w * h * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    return w, h, bytes(rgb)


def png_bytes(w, h, rgb):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def read_png(path):
    """Read a PNG written by this script (8-bit RGB, filter 0). Returns (w, h, rgb) or raises."""
    with open(path, "rb") as fh:
        data = fh.read()
    pos, idat, w, h = 8, b"", 0, 0
    while pos < len(data):
        n, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", body[:10])
            if depth != 8 or ctype != 2:
                raise RuntimeError("baseline must be an 8-bit RGB PNG written by this tool")
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * 3 + 1
    rows = []
    for y in range(h):
        if raw[y * stride] != 0:
            raise RuntimeError("baseline uses PNG filters; re-create it with save_baseline")
        rows.append(raw[y * stride + 1:(y + 1) * stride])
    return w, h, b"".join(rows)


def px(rgb, w, x, y):
    i = (y * w + x) * 3
    return rgb[i], rgb[i + 1], rgb[i + 2]


def analyse(w, h, rgb, base, x0=0, y0=0, rw=None, rh=None):
    rw = rw if rw is not None else w - x0
    rh = rh if rh is not None else h - y0
    x1, y1 = min(w, x0 + rw), min(h, y0 + rh)
    x0, y0 = max(0, x0), max(0, y0)
    area = max(1, (x1 - x0) * (y1 - y0))
    step = max(1, int((area / MAX_SAMPLES) ** 0.5))
    counts, lum, n, diff = {}, 0.0, 0, 0
    for y in range(y0, y1, step):
        for x in range(x0, x1, step):
            c = px(rgb, w, x, y)
            counts[c] = counts.get(c, 0) + 1
            lum += 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
            n += 1
            if base is not None:
                b = px(base, w, x, y)
                if abs(b[0] - c[0]) > TOL or abs(b[1] - c[1]) > TOL or abs(b[2] - c[2]) > TOL:
                    diff += 1
    n = max(1, n)
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    res = {"mean_luminance": round(lum / n, 1), "unique_colors_sampled": len(counts),
           "top_colors": [{"hex": "#%02x%02x%02x" % c, "fraction": round(k / n, 4)} for c, k in top],
           "blank": bool(top and top[0][1] / n > 0.995)}
    if base is not None:
        res["diff_fraction"] = round(diff / n, 4)
    return res


def main():
    req = json.loads(sys.stdin.read() or "{}")
    if os.name != "nt":
        print(json.dumps({"ok": False, "error": "Windows only"}))
        return
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except OSError:
        pass
    contains = req.get("title_contains", "CodeLens")
    exclude = req.get("title_exclude", DEFAULT_EXCLUDE)
    win = find_window(contains, exclude)
    launched = False
    if win is None and req.get("launch"):
        subprocess.Popen([sys.executable, os.path.join(ROOT, "scripts", "CodeLens.py")], cwd=ROOT,
                         creationflags=0x00000008 | 0x00000200)
        launched = True
        deadline = time.time() + float(req.get("wait_s", 15))
        while win is None and time.time() < deadline:
            time.sleep(0.5)
            win = find_window(contains, exclude)
        time.sleep(1.5)  # let first paint finish
    if win is None:
        print(json.dumps({"ok": False, "error": "no visible window matching %r" % contains, "launched": launched}))
        return
    deadline = time.time() + (float(req.get("wait_s", 15)) if launched else 0)
    while True:
        _, hwnd, title = win
        try:
            w, h, rgb = capture(hwnd)
            break
        except RuntimeError as e:
            if time.time() >= deadline:
                print(json.dumps({"ok": False, "error": str(e), "launched": launched}))
                return
            time.sleep(0.5)
            win = find_window(contains, exclude) or win
    os.makedirs(OUT, exist_ok=True)
    stem = "".join(c for c in req.get("name", "snapshot") if c.isalnum() or c in "-_") or "snapshot"
    path = os.path.join(OUT, stem + ".png")
    with open(path, "wb") as fh:
        fh.write(png_bytes(w, h, rgb))
    base, base_note = None, None
    if req.get("baseline"):
        bp = os.path.abspath(os.path.join(OUT, req["baseline"]))
        if not bp.startswith(OUT + os.sep) or not os.path.isfile(bp):
            base_note = "baseline not found inside build/snapshots"
        else:
            bw, bh, brgb = read_png(bp)
            if (bw, bh) != (w, h):
                base_note = "size mismatch: baseline %dx%d vs now %dx%d" % (bw, bh, w, h)
            else:
                base = brgb
    out = {"ok": True, "launched": launched, "window_title": title, "path": path, "width": w, "height": h}
    out.update(analyse(w, h, rgb, base))
    if base_note:
        out["baseline_note"] = base_note
    regions = []
    for r in req.get("regions") or []:
        a = analyse(w, h, rgb, base, int(r.get("x", 0)), int(r.get("y", 0)), int(r.get("w", w)), int(r.get("h", h)))
        a["name"] = r.get("name", "region")
        regions.append(a)
    if regions:
        out["regions"] = regions
    pts = []
    for p in req.get("pixels") or []:
        x, y = int(p[0]), int(p[1])
        if 0 <= x < w and 0 <= y < h:
            pts.append({"x": x, "y": y, "hex": "#%02x%02x%02x" % px(rgb, w, x, y)})
    if pts:
        out["pixels"] = pts
    if req.get("save_baseline"):
        bpath = os.path.join(OUT, stem + ".baseline.png")
        with open(bpath, "wb") as fh:
            fh.write(png_bytes(w, h, rgb))
        out["baseline_saved"] = bpath
    print(json.dumps(out))


main()
