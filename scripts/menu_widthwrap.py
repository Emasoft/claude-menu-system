#!/usr/bin/env python3
"""Display-width measurement, ANSI-safe wrapping, terminal-width detection.

Three responsibilities, kept in one module because they all depend on
the same monospace-width oracle:

1. ``display_width(text)`` — Unicode-aware column count after stripping
   ANSI codes. Ported from CPV's ``format_menu.py`` (unicodedata-based,
   no ``wcwidth`` dependency).
2. ``wrap_ansi(text, budget, cont_indent=0)`` — wrap to ``budget``
   display columns without splitting an ANSI escape mid-sequence. Color
   state is tracked and re-applied on continuation lines so wrapped
   coloured text doesn't lose its color.
3. ``terminal_width()`` — best-effort terminal column count. Order:
   ``COLUMNS`` env var → ``shutil.get_terminal_size`` → 80 default,
   capped at ``CLAUDE_MENU_MAX_WIDTH`` (default 120) so ultra-wide
   terminals don't produce stretched boxes.

Also provides ``line_safe_truncate(text, max_chars)`` for the 10K cap
in ``menu_emit.py`` — cuts only at newline boundaries, never mid-ANSI.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata

# Ensure sibling modules resolve when this script runs from any cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from menu_ansi import strip_ansi

# Match a single ANSI CSI escape. Used by the wrapper to walk past
# escapes character-by-character without consuming the visible char
# they precede.
_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def _char_display_width(ch: str) -> int:
    """Return the display column count for ``ch`` in a monospace terminal.

    Ported verbatim from CPV's ``format_menu.py._char_display_width``.
    The heuristic is empirical — what actually renders in iTerm2 /
    Terminal.app / GNOME Terminal / Windows Terminal:

    - **0 cols**: control chars (< 0x20, 0x7F), combining marks
      (Mn/Mc/Me), format chars (Cf).
    - **2 cols**: East-Asian Wide / Full-width; post-BMP emoji
      (U+1F000+ ranges).
    - **1 col**: everything else — including BMP Misc Symbols /
      Dingbats (✓ ✗ ⚠ ○ ◐ ⊝ •) which DO render as 1 column in
      standard monospace fonts.
    """
    if ord(ch) < 0x20 or ord(ch) == 0x7F:
        return 0
    if unicodedata.category(ch) in ("Mn", "Mc", "Me", "Cf"):
        return 0
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    code = ord(ch)
    if (
        0x1F300 <= code <= 0x1FAFF  # Misc Symbols & Pictographs, Symbols & Pict Ext-A
        or 0x1F000 <= code <= 0x1F2FF  # Mahjong, Domino, Playing Cards
        or 0x1F600 <= code <= 0x1F64F  # Emoticons
    ):
        return 2
    return 1


def display_width(text: str) -> int:
    """Total display columns occupied by ``text`` after stripping ANSI codes."""
    return sum(_char_display_width(ch) for ch in strip_ansi(text))


def pad(text: str, width: int, align: str = "left") -> str:
    """Pad ``text`` to ``width`` display columns with spaces.

    If ``text`` is already wider than ``width``, return unchanged
    rather than truncating — truncating menu cells silently hides
    user-facing information.
    """
    pad_amount = width - display_width(text)
    if pad_amount <= 0:
        return text
    if align == "right":
        return " " * pad_amount + text
    if align == "center":
        left = pad_amount // 2
        right = pad_amount - left
        return " " * left + text + " " * right
    return text + " " * pad_amount


def terminal_width(*, fallback: int = 80) -> int:
    """Detect terminal column count, honoring CLAUDE_MENU_MAX_WIDTH cap.

    Lookup order:
    1. ``COLUMNS`` env var (POSIX-standard, honored by ``stty``).
    2. ``shutil.get_terminal_size((fallback, 24)).columns``.
    3. ``fallback`` (default 80).

    Capped at ``CLAUDE_MENU_MAX_WIDTH`` (env var, default 120) so
    ultra-wide terminals don't produce stretched boxes that are hard
    to scan.
    """
    raw_max = os.environ.get("CLAUDE_MENU_MAX_WIDTH", "120")
    try:
        max_width = int(raw_max)
    except ValueError:
        max_width = 120
    cols_env = os.environ.get("COLUMNS")
    if cols_env:
        try:
            return min(int(cols_env), max_width)
        except ValueError:
            pass
    try:
        return min(shutil.get_terminal_size((fallback, 24)).columns, max_width)
    except (OSError, ValueError):
        return min(fallback, max_width)


def wrap_ansi(text: str, budget: int, cont_indent: int = 0) -> list[str]:
    """Wrap ``text`` to ``budget`` display columns, ANSI-safe.

    - Never splits an ANSI escape mid-sequence (escapes are treated as
      0-column atoms).
    - Tracks the currently-active SGR state and re-emits it at the
      start of each continuation line so wrapped coloured text keeps
      its color.
    - Word-boundary preferred. If a single token is wider than
      ``budget`` (e.g. an unbroken URL), it is hard-wrapped at the
      column limit.
    - ``cont_indent`` spaces are prepended to every continuation line
      so wrapped text visually aligns inside its cell.

    Returns a list of lines (no trailing newlines).
    """
    if budget <= 0:
        return [text]
    if cont_indent < 0:
        cont_indent = 0

    # Pre-split on hard newlines so explicit \n in the input is honored.
    raw_lines = text.split("\n")
    out: list[str] = []
    for raw_line in raw_lines:
        out.extend(_wrap_single_line(raw_line, budget, cont_indent))
    return out


def _wrap_single_line(text: str, budget: int, cont_indent: int) -> list[str]:
    """Wrap a single logical line (no \\n in input)."""
    if display_width(text) <= budget:
        return [text]

    # Split into atoms: each atom is either an ANSI escape or a single
    # visible character. Atoms carry their display width with them.
    atoms: list[tuple[str, int, bool]] = []  # (text, display_width, is_ansi)
    i = 0
    while i < len(text):
        m = _ANSI_SGR_RE.match(text, i)
        if m:
            atoms.append((m.group(0), 0, True))
            i = m.end()
        else:
            ch = text[i]
            atoms.append((ch, _char_display_width(ch), False))
            i += 1

    # Walk atoms, accumulating into the current line until adding the
    # next atom would exceed budget. At each break point, remember the
    # most-recently-seen whitespace position so we can wrap on a word
    # boundary if possible.
    indent = " " * cont_indent
    cont_budget = max(1, budget - cont_indent)
    lines: list[str] = []
    cur_atoms: list[tuple[str, int, bool]] = []
    cur_width = 0
    last_space_idx: int | None = None  # index into cur_atoms of last whitespace
    active_sgr: list[str] = []  # stack of active SGR escapes
    is_first_line = True

    def _flush(break_at: int | None) -> None:
        """Emit a line containing cur_atoms[:break_at] (or all)."""
        nonlocal cur_atoms, cur_width, last_space_idx, is_first_line
        if break_at is None:
            kept = cur_atoms[:]
            remainder: list[tuple[str, int, bool]] = []
        else:
            kept = cur_atoms[:break_at]
            # Skip the breaking whitespace itself when starting next line.
            remainder = cur_atoms[break_at + 1 :]
        # Build the line. Re-emit active SGR codes at start of continuation lines.
        prefix = "" if is_first_line else indent + "".join(active_sgr)
        body = "".join(a for a, _, _ in kept)
        # Track SGR state changes inside the kept atoms.
        for atom, _w, is_ansi in kept:
            if is_ansi:
                if atom == "\x1b[0m":
                    active_sgr.clear()
                else:
                    active_sgr.append(atom)
        # If the line was actively colored, append a reset so the wrap
        # boundary doesn't bleed the color into the indent of the next line.
        suffix = RESET if active_sgr else ""
        lines.append(prefix + body + suffix)
        is_first_line = False
        # Reset accumulator with remainder.
        cur_atoms = remainder
        cur_width = sum(w for _, w, _ in cur_atoms)
        last_space_idx = None
        for idx, (atom, _w, is_ansi) in enumerate(cur_atoms):
            if not is_ansi and atom.isspace():
                last_space_idx = idx

    line_budget = budget if is_first_line else cont_budget
    for atom, width, is_ansi in atoms:
        if not is_ansi and atom.isspace():
            last_space_idx = len(cur_atoms)
        if cur_width + width > line_budget and cur_atoms:
            if last_space_idx is not None:
                _flush(last_space_idx)
            else:
                _flush(None)  # hard wrap — no whitespace to break on
            line_budget = cont_budget
        cur_atoms.append((atom, width, is_ansi))
        cur_width += width
    if cur_atoms:
        _flush(None)
    return lines if lines else [text]


# Re-export RESET so callers don't need to import menu_ansi just for it.
RESET = "\x1b[0m"


def line_safe_truncate(text: str, max_chars: int, *, indicator: str = "…[truncated]") -> str:
    """Truncate ``text`` to at most ``max_chars`` chars, cutting only at \\n.

    Used by the 10K hook-output cap in ``menu_emit.py``. Never cuts
    mid-ANSI-escape: we walk lines from the start, accumulating until
    the next line would push past the budget. The ``indicator`` is
    appended on its own line iff we actually dropped content.
    """
    if len(text) <= max_chars:
        return text
    budget = max_chars - len(indicator) - 1  # -1 for the joining \n
    if budget <= 0:
        return text[:max_chars]
    lines = text.split("\n")
    out: list[str] = []
    used = 0
    for line in lines:
        next_used = used + len(line) + (1 if out else 0)
        if next_used > budget:
            break
        out.append(line)
        used = next_used
    if not out:
        # Single line longer than the budget — char-truncate.
        return text[:budget] + "\n" + indicator
    return "\n".join(out) + "\n" + indicator
