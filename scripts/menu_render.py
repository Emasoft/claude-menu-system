#!/usr/bin/env python3
"""Core renderer — turns a validated spec into a Unicode-bordered string.

Ports the four modes that originated in CPV's ``format_menu.py``
(``menu``, ``summary``, ``breakdown``, ``status_table``) and adds four
new modes for the universal menu system (``panel``, ``multi_box``,
``progress``, ``confirm``).

Box styles — ``"style"`` field on the spec (or default by mode):
  - ``heavy``   `┏━┓ ┃ ┗━┛`  (default for menu / summary / breakdown / status_table)
  - ``rounded`` `╭─╮ │ ╰─╯`  (default for panel / multi_box / progress / confirm)
  - ``light``   `┌─┐ │ └─┘`
  - ``double``  `╔═╗ ║ ╚═╝`
  - ``ascii``   `+-+ | +-+`   (fallback for non-box-drawing terminals)

Color: every renderer takes ``use_color: bool`` and threads the choice
into ``menu_ansi.color``. The renderer always *generates* output with
ANSI codes when ``use_color=True``; ``menu_emit`` may strip them later
if the environment changed.

Pure module — no IO, no env-var reads, no system calls. The caller
(``menu_write.py``) decides ``use_color`` and threads it in.
"""

from __future__ import annotations

import os
import sys
import warnings
from typing import Any

# Ensure sibling modules resolve when this script runs from any cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from menu_ansi import RESET, color, should_use_unicode_boxes
from menu_widthwrap import display_width, pad, wrap_ansi

# ---------------------------------------------------------------------------
# Box styles
# ---------------------------------------------------------------------------


class BoxStyle:
    """Glyph set for a single box style."""

    def __init__(
        self,
        *,
        tl: str,
        tr: str,
        bl: str,
        br: str,
        h_top: str,
        h_bot: str,
        v: str,
        # T-joints — top edge / middle edge / bottom edge
        t_top: str,
        t_mid: str,
        t_bot: str,
        # Header separator (between heavy top + light data rows)
        sep_left: str,
        sep_right: str,
        sep_cross: str,
        h_sep: str,
    ) -> None:
        self.tl, self.tr, self.bl, self.br = tl, tr, bl, br
        self.h_top, self.h_bot, self.v = h_top, h_bot, v
        self.t_top, self.t_mid, self.t_bot = t_top, t_mid, t_bot
        self.sep_left, self.sep_right, self.sep_cross, self.h_sep = (
            sep_left,
            sep_right,
            sep_cross,
            h_sep,
        )


STYLES: dict[str, BoxStyle] = {
    "heavy": BoxStyle(
        tl="┏",
        tr="┓",
        bl="└",
        br="┘",
        h_top="━",
        h_bot="─",
        v="│",
        t_top="┳",
        t_mid="┼",
        t_bot="┴",
        sep_left="┡",
        sep_right="┩",
        sep_cross="╇",
        h_sep="━",
    ),
    "rounded": BoxStyle(
        tl="╭",
        tr="╮",
        bl="╰",
        br="╯",
        h_top="─",
        h_bot="─",
        v="│",
        t_top="┬",
        t_mid="┼",
        t_bot="┴",
        sep_left="├",
        sep_right="┤",
        sep_cross="┼",
        h_sep="─",
    ),
    "light": BoxStyle(
        tl="┌",
        tr="┐",
        bl="└",
        br="┘",
        h_top="─",
        h_bot="─",
        v="│",
        t_top="┬",
        t_mid="┼",
        t_bot="┴",
        sep_left="├",
        sep_right="┤",
        sep_cross="┼",
        h_sep="─",
    ),
    "double": BoxStyle(
        tl="╔",
        tr="╗",
        bl="╚",
        br="╝",
        h_top="═",
        h_bot="═",
        v="║",
        t_top="╦",
        t_mid="╬",
        t_bot="╩",
        sep_left="╠",
        sep_right="╣",
        sep_cross="╬",
        h_sep="═",
    ),
    "ascii": BoxStyle(
        tl="+",
        tr="+",
        bl="+",
        br="+",
        h_top="-",
        h_bot="-",
        v="|",
        t_top="+",
        t_mid="+",
        t_bot="+",
        sep_left="+",
        sep_right="+",
        sep_cross="+",
        h_sep="-",
    ),
}


def _style(spec: dict[str, Any], default: str) -> BoxStyle:
    """Resolve box style with graceful ASCII downgrade.

    Lookup order:
    1. ``spec["style"]`` if set and valid.
    2. ``default`` for the mode.

    Then: if the resolved style needs Unicode box-drawing chars AND the
    terminal can't render them (per ``should_use_unicode_boxes()``), we
    downgrade to ``ascii``. Explicit ``"ascii"`` always works regardless
    of terminal. This means a spec author can request rounded/heavy
    without worrying about breaking dumb terminals, IDE consoles, or
    LANG=C environments.
    """
    name = spec.get("style", default)
    if name not in STYLES:
        raise ValueError(f"unknown box style {name!r}; allowed: {sorted(STYLES)}")
    # Auto-downgrade to ASCII if terminal can't render unicode boxes.
    # Honor an explicit ``"force_unicode": true`` escape hatch for tests.
    if name != "ascii" and not spec.get("force_unicode", False):
        if not should_use_unicode_boxes():
            return STYLES["ascii"]
    return STYLES[name]


# ---------------------------------------------------------------------------
# Menu mode
# ---------------------------------------------------------------------------

# Static keys bypass renumbering even when `renumber:true`.
#   "0" — historical cancel / back convention.
#   "A" — historical "All / ALL findings" shortcut.
#   "M" / "B" / "X" — CPV's reserved navigation letters: Main menu / Back / eXit.
# Reserving M/B/X here makes CPV's fixed-key contract correct-by-default
# so callers don't have to remember `renumber:false` just to keep nav
# letters stable. Single-source-of-truth for both renderer and validator
# (menu_spec._validate_menu treats this same set as the "single-or-static"
# allowlist for key validation).
_STATIC_KEYS = frozenset({"0", "A", "M", "B", "X"})


def render_menu(spec: dict[str, Any], use_color: bool = False) -> tuple[str, dict[str, str]]:
    """Return (table_string, key_to_action_id_map).

    The map lets the orchestrator route the user's reply back to the
    intended action_id even after disabled rows were dropped and the
    remaining rows renumbered. Ported from CPV ``format_menu.py``.
    """
    header_label = spec["header"]
    rows_in = spec["rows"]
    footer = spec.get("footer", "Type a number to choose:")
    renumber = spec.get("renumber", True)
    style = _style(spec, default="heavy")

    rows_live = [r for r in rows_in if not r.get("disabled", False)]
    action_map: dict[str, str] = {}
    rendered_rows: list[tuple[str, str]] = []
    next_num = 1
    for r in rows_live:
        action_id = r.get("action_id", r["key"])
        if r["key"] in _STATIC_KEYS or not renumber:
            rendered_key = r["key"]
        else:
            rendered_key = str(next_num)
            next_num += 1
            # M2: renumber silently rewrites author-chosen numeric keys to
            # positional values. That is the documented behavior of
            # ``renumber:true``, but an author who wrote literal 1/3/2 keys
            # would not expect them re-rendered as 1/2/3 with no signal —
            # warn so the clobber is visible.
            if r["key"] != rendered_key:
                warnings.warn(
                    f"renumber=True rewrote row key {r['key']!r} -> {rendered_key!r}; "
                    "numeric keys are positional placeholders under renumber "
                    f"(only the reserved static keys {sorted(_STATIC_KEYS)} are "
                    "preserved). Set renumber=False to keep literal keys.",
                    stacklevel=2,
                )
        # M1 (defense in depth): a duplicate rendered key would overwrite an
        # earlier row's action route in ``action_map``, silently making the
        # first row's action_id unreachable. Fail fast instead. ``_validate_menu``
        # already rejects duplicate authored keys; this guards the render path
        # for callers who bypass validation (e.g. renumber=False with dup keys).
        if rendered_key in action_map:
            raise ValueError(
                f"duplicate menu key {rendered_key!r}: row action "
                f"{action_id!r} would overwrite {action_map[rendered_key]!r}"
            )
        action_map[rendered_key] = action_id
        rendered_rows.append((rendered_key, r["label"]))

    # M4: a menu with no live rows (rows:[] or every row disabled) would
    # render a body-less box plus a "Type a number to choose:" footer,
    # prompting the user to pick from zero options with nothing to route to.
    # Reject it — menu_write.py:73 catches ValueError -> exit 3.
    if not rendered_rows:
        raise ValueError("menu has no selectable rows (all rows disabled or rows empty)")

    key_header = "#"
    width_key = (
        max(display_width(key_header), *(display_width(k) for k, _ in rendered_rows))
        if rendered_rows
        else display_width(key_header)
    )
    width_label = (
        max(display_width(header_label), *(display_width(lbl) for _, lbl in rendered_rows))
        if rendered_rows
        else display_width(header_label)
    )

    pad_l, pad_r = 1, 1
    bar_key_top = style.h_top * (width_key + pad_l + pad_r)
    bar_label_top = style.h_top * (width_label + pad_l + pad_r)
    bar_key_bot = style.h_bot * (width_key + pad_l + pad_r)
    bar_label_bot = style.h_bot * (width_label + pad_l + pad_r)
    bar_key_sep = style.h_sep * (width_key + pad_l + pad_r)
    bar_label_sep = style.h_sep * (width_label + pad_l + pad_r)

    border_color = "border"
    header_color = "header"

    def col(text: str, role: str) -> str:
        return color(text, role, force=use_color)

    lines = [
        col(f"{style.tl}{bar_key_top}{style.t_top}{bar_label_top}{style.tr}", border_color),
        col(style.v, border_color)
        + f"{' ' * pad_l}{col(pad(key_header, width_key, 'right'), header_color)}{' ' * pad_r}"
        + col(style.v, border_color)
        + f"{' ' * pad_l}{col(pad(header_label, width_label), header_color)}{' ' * pad_r}"
        + col(style.v, border_color),
        col(
            f"{style.sep_left}{bar_key_sep}{style.sep_cross}{bar_label_sep}{style.sep_right}",
            border_color,
        ),
    ]
    for k, label in rendered_rows:
        lines.append(
            col(style.v, border_color)
            + f"{' ' * pad_l}{pad(k, width_key, 'right')}{' ' * pad_r}"
            + col(style.v, border_color)
            + f"{' ' * pad_l}{pad(label, width_label)}{' ' * pad_r}"
            + col(style.v, border_color)
        )
    lines.append(
        col(f"{style.bl}{bar_key_bot}{style.t_bot}{bar_label_bot}{style.br}", border_color)
    )
    if footer:
        lines.append(footer)
    return "\n".join(lines), action_map


# ---------------------------------------------------------------------------
# Summary mode
# ---------------------------------------------------------------------------

_SEVERITY_ORDER = ("CRITICAL", "MAJOR", "MINOR", "NIT", "WARNING")
_SEVERITY_ROLE = {
    "CRITICAL": "critical",
    "MAJOR": "major",
    "MINOR": "minor",
    "NIT": "nit",
    "WARNING": "warning_severity",
}


def render_summary(spec: dict[str, Any], use_color: bool = False) -> str:
    """Render a 2-column severity summary table + verdict + report path."""
    title = spec.get("title", "Findings summary")
    counts_in = spec["counts"]
    counts = {
        sev: counts_in.get(sev.lower(), counts_in.get(sev.upper(), 0)) for sev in _SEVERITY_ORDER
    }
    verdict = (spec.get("verdict") or "").upper()
    report_path = spec.get("report_path", "")
    style = _style(spec, default="heavy")

    col_sev = "Severity"
    col_count = "Count"
    width_sev = max(display_width(col_sev), *(display_width(s) for s in _SEVERITY_ORDER))
    width_count = max(
        display_width(col_count), *(display_width(str(counts[s])) for s in _SEVERITY_ORDER)
    )

    pad_l, pad_r = 1, 1
    bar_sev_top = style.h_top * (width_sev + pad_l + pad_r)
    bar_count_top = style.h_top * (width_count + pad_l + pad_r)
    bar_sev_bot = style.h_bot * (width_sev + pad_l + pad_r)
    bar_count_bot = style.h_bot * (width_count + pad_l + pad_r)
    bar_sev_sep = style.h_sep * (width_sev + pad_l + pad_r)
    bar_count_sep = style.h_sep * (width_count + pad_l + pad_r)

    def col(text: str, role: str) -> str:
        return color(text, role, force=use_color)

    lines = [
        title,
        col(f"{style.tl}{bar_sev_top}{style.t_top}{bar_count_top}{style.tr}", "border"),
        col(style.v, "border")
        + f"{' ' * pad_l}{col(pad(col_sev, width_sev), 'header')}{' ' * pad_r}"
        + col(style.v, "border")
        + f"{' ' * pad_l}{col(pad(col_count, width_count, 'right'), 'header')}{' ' * pad_r}"
        + col(style.v, "border"),
        col(
            f"{style.sep_left}{bar_sev_sep}{style.sep_cross}{bar_count_sep}{style.sep_right}",
            "border",
        ),
    ]
    for sev in _SEVERITY_ORDER:
        count = counts[sev]
        sev_cell = pad(sev, width_sev)
        count_cell = pad(str(count), width_count, "right")
        if use_color and count > 0:
            sev_cell = color(sev, _SEVERITY_ROLE[sev], force=True) + " " * (
                width_sev - display_width(sev)
            )
            count_cell = color(
                pad(str(count), width_count, "right"), _SEVERITY_ROLE[sev], force=True
            )
        lines.append(
            col(style.v, "border")
            + f"{' ' * pad_l}{sev_cell}{' ' * pad_r}"
            + col(style.v, "border")
            + f"{' ' * pad_l}{count_cell}{' ' * pad_r}"
            + col(style.v, "border")
        )
    lines.append(col(f"{style.bl}{bar_sev_bot}{style.t_bot}{bar_count_bot}{style.br}", "border"))
    if verdict:
        v_role = "verdict_valid" if verdict == "VALID" else "verdict_invalid"
        lines.append(f"Verdict: {color(verdict, v_role, force=use_color)}")
    if report_path:
        lines.append(f"Report: {report_path}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Breakdown mode
# ---------------------------------------------------------------------------


def render_breakdown(spec: dict[str, Any], use_color: bool = False) -> str:
    """Render a category × severity matrix with row totals + totals row."""
    title = spec.get("title", "Findings breakdown")
    row_header = spec.get("row_header", "Category")
    columns = spec.get("columns") or list(_SEVERITY_ORDER)
    rows = spec["rows"]
    include_total_col = spec.get("total_column", True)
    include_total_row = spec.get("totals_row", True)
    verdict = (spec.get("verdict") or "").upper()
    report_path = spec.get("report_path", "")
    style = _style(spec, default="heavy")

    columns_upper = [c.upper() for c in columns]
    total_col_label = "Total"

    matrix: list[tuple[str, list[int], int]] = []
    column_totals = [0] * len(columns)
    grand_total = 0
    for row in rows:
        normalized = {k.upper(): int(v) for k, v in row["counts"].items() if isinstance(v, int)}
        row_counts = [normalized.get(col, 0) for col in columns_upper]
        row_total = sum(row_counts)
        matrix.append((row["label"], row_counts, row_total))
        for i, c in enumerate(row_counts):
            column_totals[i] += c
        grand_total += row_total
    if include_total_row and matrix:
        matrix.append(("TOTAL", column_totals, grand_total))

    pad_l, pad_r = 1, 1
    width_label = (
        max(display_width(row_header), *(display_width(lbl) for lbl, _, _ in matrix))
        if matrix
        else display_width(row_header)
    )
    col_widths = []
    for i, c in enumerate(columns):
        col_max = (
            max(
                display_width(c),
                *(display_width(str(row_counts[i])) for _, row_counts, _ in matrix),
            )
            if matrix
            else display_width(c)
        )
        col_widths.append(col_max)
    width_total = (
        max(display_width(total_col_label), *(display_width(str(t)) for _, _, t in matrix))
        if (include_total_col and matrix)
        else 0
    )

    def cell_bar(w: int, ch: str) -> str:
        return ch * (w + pad_l + pad_r)

    def colb(text: str, role: str) -> str:
        return color(text, role, force=use_color)

    def row_line(
        label: str, cells: list[str], total_cell: str | None, *, header_row: bool = False
    ) -> str:
        v = colb(style.v, "border")
        label_role = "header" if header_row else "label"
        label_part = f"{v}{' ' * pad_l}{colb(pad(label, width_label), label_role)}{' ' * pad_r}"
        cell_parts = "".join(
            f"{v}{' ' * pad_l}{pad(cells[i], col_widths[i], 'right')}{' ' * pad_r}"
            for i in range(len(cells))
        )
        total_part = ""
        if include_total_col and total_cell is not None:
            total_part = f"{v}{' ' * pad_l}{pad(total_cell, width_total, 'right')}{' ' * pad_r}"
        return label_part + cell_parts + total_part + v

    top_segments = [cell_bar(width_label, style.h_top)] + [
        cell_bar(w, style.h_top) for w in col_widths
    ]
    sep_segments = [cell_bar(width_label, style.h_sep)] + [
        cell_bar(w, style.h_sep) for w in col_widths
    ]
    bot_segments = [cell_bar(width_label, style.h_bot)] + [
        cell_bar(w, style.h_bot) for w in col_widths
    ]
    if include_total_col:
        top_segments.append(cell_bar(width_total, style.h_top))
        sep_segments.append(cell_bar(width_total, style.h_sep))
        bot_segments.append(cell_bar(width_total, style.h_bot))

    top = colb(style.tl + style.t_top.join(top_segments) + style.tr, "border")
    sep = colb(style.sep_left + style.sep_cross.join(sep_segments) + style.sep_right, "border")
    bot = colb(style.bl + style.t_bot.join(bot_segments) + style.br, "border")
    mid = colb(
        "├" + style.t_mid.join(sep_segments).replace(style.sep_cross, style.t_mid) + "┤", "border"
    )

    header_cells = [str(c) for c in columns]
    header_total = total_col_label if include_total_col else None
    lines = [title, top, row_line(row_header, header_cells, header_total, header_row=True), sep]

    last_data_idx = len(matrix) - (1 if include_total_row and matrix else 0) - 1
    for idx, (label, row_counts, row_total) in enumerate(matrix):
        cells = [str(c) for c in row_counts]
        if use_color:
            for i, count in enumerate(row_counts):
                if count > 0 and columns_upper[i] in _SEVERITY_ROLE:
                    role = _SEVERITY_ROLE[columns_upper[i]]
                    cells[i] = color(pad(str(count), col_widths[i], "right"), role, force=True)
            if label == "TOTAL":
                label = f"\x1b[1m{label}{RESET}"
        total_cell = str(row_total) if include_total_col else None
        if include_total_row and idx == last_data_idx + 1 and matrix:
            lines.append(mid)
        lines.append(row_line(label, cells, total_cell))
    lines.append(bot)

    if verdict:
        v_role = "verdict_valid" if verdict == "VALID" else "verdict_invalid"
        lines.append(f"Verdict: {color(verdict, v_role, force=use_color)}")
    if report_path:
        lines.append(f"Report: {report_path}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Status table mode
# ---------------------------------------------------------------------------

_STATUS_GLYPH = {
    "ok": "✓",
    "implemented": "✓",
    "missing": "✗",
    "buggy": "⚠",
    "partial": "◐",
    "pending": "○",
    "skipped": "⊝",
    "info": "•",
}
_STATUS_ROLE = {
    "ok": "success",
    "implemented": "success",
    "missing": "error",
    "buggy": "warning",
    "partial": "warning",
    "pending": "info",
    "skipped": "muted",
    "info": "info",
}
_STATUS_LABEL = {
    "ok": "OK",
    "implemented": "OK",
    "missing": "MISSING",
    "buggy": "BUGGY",
    "partial": "PARTIAL",
    "pending": "PENDING",
    "skipped": "SKIPPED",
    "info": "INFO",
}


def render_status_table(spec: dict[str, Any], use_color: bool = False) -> str:
    """3-column status table: Component | Status | Notes."""
    title = spec.get("title", "Status")
    row_header = spec.get("row_header", "Component")
    rows = spec["rows"]
    include_summary_row = spec.get("summary_row", True)
    footer_line = spec.get("footer", "")
    style = _style(spec, default="heavy")

    status_col = "Status"
    notes_col = "Notes"

    normalized: list[tuple[str, str, str, str]] = []
    counts: dict[str, int] = {}
    for r in rows:
        skey = r["status"].lower()
        if skey not in _STATUS_GLYPH:
            raise ValueError(f"unknown status {r['status']!r}; allowed: {sorted(_STATUS_GLYPH)}")
        glyph = _STATUS_GLYPH[skey]
        status_text = f"{glyph} {_STATUS_LABEL[skey]}"
        notes = str(r.get("notes", ""))
        normalized.append((r["label"], skey, status_text, notes))
        counts[skey] = counts.get(skey, 0) + 1

    pad_l, pad_r = 1, 1
    width_label = (
        max(display_width(row_header), *(display_width(lbl) for lbl, _, _, _ in normalized))
        if normalized
        else display_width(row_header)
    )
    width_status = (
        max(display_width(status_col), *(display_width(s) for _, _, s, _ in normalized))
        if normalized
        else display_width(status_col)
    )
    width_notes = (
        max(display_width(notes_col), *(display_width(n) for _, _, _, n in normalized))
        if normalized
        else display_width(notes_col)
    )

    def bar(w: int, ch: str) -> str:
        return ch * (w + pad_l + pad_r)

    def colb(text: str, role: str) -> str:
        return color(text, role, force=use_color)

    top = colb(
        f"{style.tl}{bar(width_label, style.h_top)}{style.t_top}{bar(width_status, style.h_top)}"
        f"{style.t_top}{bar(width_notes, style.h_top)}{style.tr}",
        "border",
    )
    sep = colb(
        f"{style.sep_left}{bar(width_label, style.h_sep)}{style.sep_cross}{bar(width_status, style.h_sep)}"
        f"{style.sep_cross}{bar(width_notes, style.h_sep)}{style.sep_right}",
        "border",
    )
    bot = colb(
        f"{style.bl}{bar(width_label, style.h_bot)}{style.t_bot}{bar(width_status, style.h_bot)}"
        f"{style.t_bot}{bar(width_notes, style.h_bot)}{style.br}",
        "border",
    )

    v = colb(style.v, "border")
    lines = [
        title,
        top,
        f"{v}{' ' * pad_l}{colb(pad(row_header, width_label), 'header')}{' ' * pad_r}"
        f"{v}{' ' * pad_l}{colb(pad(status_col, width_status), 'header')}{' ' * pad_r}"
        f"{v}{' ' * pad_l}{colb(pad(notes_col, width_notes), 'header')}{' ' * pad_r}{v}",
        sep,
    ]
    for label, skey, status_text, notes in normalized:
        label_cell = pad(label, width_label)
        status_padded = pad(status_text, width_status)
        notes_cell = pad(notes, width_notes)
        if use_color:
            status_padded = color(status_padded, _STATUS_ROLE[skey], force=True)
        lines.append(
            f"{v}{' ' * pad_l}{label_cell}{' ' * pad_r}"
            f"{v}{' ' * pad_l}{status_padded}{' ' * pad_r}"
            f"{v}{' ' * pad_l}{notes_cell}{' ' * pad_r}{v}"
        )
    lines.append(bot)

    if include_summary_row and counts:
        order = ("ok", "implemented", "missing", "buggy", "partial", "pending", "skipped", "info")
        seen: set[str] = set()
        parts: list[str] = []
        for k in order:
            if k in counts and _STATUS_LABEL[k] not in seen:
                seen.add(_STATUS_LABEL[k])
                label_text = _STATUS_LABEL[k]
                if use_color:
                    label_text = color(label_text, _STATUS_ROLE[k], force=True)
                parts.append(f"{counts[k]} {label_text}")
        if parts:
            lines.append("Summary: " + ", ".join(parts))
    if footer_line:
        lines.append(footer_line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Panel mode — single-section box with title + body lines
# ---------------------------------------------------------------------------


def render_panel(spec: dict[str, Any], use_color: bool = False) -> str:
    """Render a single titled box. Body is a list of strings (already-formatted lines).

    The renderer auto-wraps each body line to fit the box width. If
    ``spec["width"]`` is given, that's the OUTER width; otherwise the
    box auto-fits the longest body line up to ``terminal_width()``.
    """
    from menu_widthwrap import terminal_width

    header = spec["header"]
    body = spec.get("body", [])
    footer = spec.get("footer", "")
    style = _style(spec, default="rounded")

    # Compute inner width.
    user_width = spec.get("width")
    pad_l, pad_r = 1, 1
    if user_width == "auto" or user_width is None:
        term_w = terminal_width()
        max_body = max((display_width(line) for line in body), default=0)
        # Need room for 2 borders + 2 padding.
        inner_w = max(display_width(header), max_body)
        inner_w = min(inner_w, term_w - 2 - pad_l - pad_r)
    else:
        inner_w = max(1, int(user_width) - 2 - pad_l - pad_r)

    # Wrap each body line to fit.
    wrapped_body: list[str] = []
    for line in body:
        wrapped_body.extend(wrap_ansi(line, inner_w))
    if not wrapped_body:
        wrapped_body = [""]

    def colb(text: str, role: str) -> str:
        return color(text, role, force=use_color)

    h_top_bar = style.h_top * (inner_w + pad_l + pad_r)
    h_bot_bar = style.h_bot * (inner_w + pad_l + pad_r)
    h_sep_bar = style.h_sep * (inner_w + pad_l + pad_r)
    v = colb(style.v, "border")

    lines = [
        colb(f"{style.tl}{h_top_bar}{style.tr}", "border"),
        f"{v}{' ' * pad_l}{colb(pad(header, inner_w), 'header')}{' ' * pad_r}{v}",
        colb(f"{style.sep_left}{h_sep_bar}{style.sep_right}", "border"),
    ]
    for line in wrapped_body:
        lines.append(f"{v}{' ' * pad_l}{pad(line, inner_w)}{' ' * pad_r}{v}")
    lines.append(colb(f"{style.bl}{h_bot_bar}{style.br}", "border"))
    if footer:
        lines.append(footer)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-box mode — stack of panels
# ---------------------------------------------------------------------------


def render_multi_box(spec: dict[str, Any], use_color: bool = False) -> str:
    """Stack of panels separated by a blank line. Each box renders via render_panel."""
    boxes = spec["boxes"]
    parent_style = spec.get("style", "rounded")
    out: list[str] = []
    for i, box in enumerate(boxes):
        # Inherit parent style unless box overrides.
        if "style" not in box:
            box = dict(box)
            box["style"] = parent_style
        out.append(render_panel(box, use_color=use_color))
        if i < len(boxes) - 1:
            out.append("")  # blank line between boxes
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Progress mode — title + bar + counter
# ---------------------------------------------------------------------------


def render_progress(spec: dict[str, Any], use_color: bool = False) -> str:
    """Render a progress bar with title + counter (current/total)."""
    header = spec["header"]
    current = int(spec["current"])
    total = int(spec["total"])
    bar_width = int(spec.get("bar_width", 40))
    style = _style(spec, default="rounded")

    fraction = min(1.0, current / total) if total > 0 else 0.0
    filled = round(fraction * bar_width)
    empty = bar_width - filled
    bar = ("█" * filled) + ("░" * empty)
    pct = round(fraction * 100)
    counter = f"{current}/{total}  ({pct}%)"

    def colb(text: str, role: str) -> str:
        return color(text, role, force=use_color)

    bar_colored = colb(bar, "success" if fraction >= 1.0 else "info")
    inner = f"{bar_colored}  {counter}"
    inner_w = max(display_width(inner), display_width(header))
    pad_l, pad_r = 1, 1
    h_top = style.h_top * (inner_w + pad_l + pad_r)
    h_bot = style.h_bot * (inner_w + pad_l + pad_r)
    h_sep = style.h_sep * (inner_w + pad_l + pad_r)
    v = colb(style.v, "border")
    return "\n".join(
        [
            colb(f"{style.tl}{h_top}{style.tr}", "border"),
            f"{v}{' ' * pad_l}{colb(pad(header, inner_w), 'header')}{' ' * pad_r}{v}",
            colb(f"{style.sep_left}{h_sep}{style.sep_right}", "border"),
            f"{v}{' ' * pad_l}{pad(inner, inner_w)}{' ' * pad_r}{v}",
            colb(f"{style.bl}{h_bot}{style.br}", "border"),
        ]
    )


# ---------------------------------------------------------------------------
# Confirm mode — Y/N table with 0 — Cancel
# ---------------------------------------------------------------------------


def render_confirm(spec: dict[str, Any], use_color: bool = False) -> tuple[str, dict[str, str]]:
    """Confirm prompt with Yes / No / Cancel. Returns (text, action_map)."""
    header = spec["header"]
    yes_label = spec.get("yes_label", "Yes")
    no_label = spec.get("no_label", "No")
    style = spec.get("style", "heavy")
    # Reuse render_menu for the bordered table — it already handles styles + map.
    menu_spec = {
        "header": header,
        "style": style,
        "footer": spec.get("footer", "Type a number to choose:"),
        "rows": [
            {"key": "1", "action_id": "yes", "label": yes_label},
            {"key": "2", "action_id": "no", "label": no_label},
            {"key": "0", "action_id": "cancel", "label": "Cancel"},
        ],
    }
    return render_menu(menu_spec, use_color=use_color)


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------


def render(spec: dict[str, Any], use_color: bool = False) -> tuple[str, dict[str, str] | None]:
    """Top-level renderer — dispatch on ``spec["mode"]``.

    Returns ``(rendered_text, action_map or None)``. The action_map is
    only non-None for modes that produce one (menu, confirm) — every
    other mode returns ``None`` for the second element.
    """
    mode = spec["mode"]
    if mode == "menu":
        text, action_map = render_menu(spec, use_color=use_color)
        return text, action_map
    if mode == "summary":
        return render_summary(spec, use_color=use_color), None
    if mode == "breakdown":
        return render_breakdown(spec, use_color=use_color), None
    if mode == "status_table":
        return render_status_table(spec, use_color=use_color), None
    if mode == "panel":
        return render_panel(spec, use_color=use_color), None
    if mode == "multi_box":
        return render_multi_box(spec, use_color=use_color), None
    if mode == "progress":
        return render_progress(spec, use_color=use_color), None
    if mode == "confirm":
        text, action_map = render_confirm(spec, use_color=use_color)
        return text, action_map
    raise ValueError(f"unknown render mode {mode!r}")
