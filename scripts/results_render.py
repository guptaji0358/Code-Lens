"""
results_render.py - pure functions that build the results pane's HTML.

No `self`, no widgets - just local lists/strings and a read-only theme
dict - so FinderWindow's one-line/multi-line/range search paths can each
build their whole results buffer on a background QThread and only touch
the QTextEdit once, in the main-thread "done" callback.
"""

import os

from theme import FONT_MONO


def _html_write(buffer, text, t=None, color=None, bold=False, mono=True, bg=None):
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
    buffer.append(f"<span{style_attr}>{text}</span>")


def _html_write_match_line(buffer, t, line_no, line_text, spans, width, indent):
    num_str = str(line_no).rjust(width)
    _html_write(buffer, f"{indent}{num_str}: ", color=t["accent"])
    if not spans:
        _html_write(buffer, f"{line_text}\n")
        return
    cursor = 0
    for start, end in spans:
        _html_write(buffer, line_text[cursor:start])
        _html_write(buffer, line_text[start:end], color=t["hl_fg"], bg=t["hl_bg"])
        cursor = end
    _html_write(buffer, line_text[cursor:] + "\n")


def _html_render_file_card(buffer, t, index, total, entry, results, note=None):
    name = os.path.basename(entry.path)
    if not results:
        _html_write(buffer, f"  [{index}/{total}] {name} - no matches\n", color=t["fg_dim"])
        return

    count = len(results)
    label = f"{count} match" if count == 1 else f"{count} matches"
    _html_write(buffer, f"\n[{index}/{total}] ", color=t["fg_dim"])
    _html_write(buffer, f"{name}", color=t["accent"], bold=True)
    _html_write(buffer, "  -  ")
    _html_write(buffer, f"{label}\n", color=t["ok"])
    _html_write(buffer, f"  {entry.path}\n", color=t["fg_dim"])
    if note:
        _html_write(buffer, f"  ({note})\n", color=t["warn"])

    width = len(str(max(len(entry.lines), 1)))
    multiple = len(results) > 1
    for n, item in enumerate(results, start=1):
        if multiple:
            _html_write(buffer, f"  -- match {n}/{len(results)} --\n", color=t["fg_dim"])
        if item[0] == "single":
            _, line_no, line_text, spans = item
            _html_write_match_line(buffer, t, line_no, line_text, spans, width, indent="  ")
        else:
            _, start_no, end_no, per_line = item
            _html_write(buffer, f"  Lines {start_no}-{end_no}:\n", color=t["accent"], bold=True)
            for li_no, line_text, spans in per_line:
                _html_write_match_line(buffer, t, li_no, line_text, spans, width, indent="    ")
