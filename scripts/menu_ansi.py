"""Semantic ANSI color palette + terminal-capability detection.

Why a separate module: every other module (renderer, emit, write) needs
to know the SAME answer to "what does this terminal support?". Centralising
the policy in one helper avoids drift between modules and lets the test
suite override it deterministically.

Two policies live here:

1. ``should_use_color()`` — whether to embed ANSI color codes
2. ``should_use_unicode_boxes()`` — whether the terminal can render
   Unicode box-drawing characters (heavy/rounded/light/double styles).
   Auto-downgrade to ASCII style is handled in the renderer when this
   returns False.

Both functions are conservative — they err on the side of NOT using a
feature if any signal suggests the terminal can't render it. The cost
of a false negative is "menu looks plainer than it could"; the cost of
a false positive is "menu looks like a wall of garbled escape codes",
which is much worse.
"""

from __future__ import annotations

import os
import re
import sys

# Semantic palette — caller specifies a role, not a raw code.
PALETTE: dict[str, str] = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "border": "\x1b[94m",  # bright blue
    "header": "\x1b[97m",  # bright white
    "label": "",
    "value": "\x1b[93m",  # bright yellow
    "success": "\x1b[92m",  # bright green
    "warning": "\x1b[93m",  # bright yellow
    "error": "\x1b[91m",  # bright red
    "info": "\x1b[96m",  # bright cyan
    "muted": "\x1b[90m",  # bright black / grey
    "critical": "\x1b[91m",
    "major": "\x1b[93m",
    "minor": "\x1b[94m",
    "nit": "\x1b[96m",
    "warning_severity": "\x1b[95m",
    "verdict_valid": "\x1b[92m",
    "verdict_invalid": "\x1b[91m",
}

RESET = PALETTE["reset"]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# TERM values that mean "no terminal escape support at all". Anything
# else is assumed to support at least 8-color ANSI — that's been the
# baseline since the early 1980s.
_DUMB_TERMS: frozenset[str] = frozenset({"dumb", "unknown", ""})

# Known IDE-embedded terminals. Most of these DO support ANSI colors
# (VSCode integrated terminal, JetBrains console, etc.) but some don't
# advertise it well via isatty. We trust their TERM_PROGRAM / TERM
# values rather than isatty here.
_KNOWN_COLOR_IDE_TERMS: frozenset[str] = frozenset(
    {"vscode", "JetBrains-JediTerm", "tmux", "screen"}
)


def should_use_color(*, override: bool | None = None) -> bool:
    """Return True iff the current environment wants color codes emitted.

    Decision order (any signal in the list short-circuits):

    1. ``override`` argument — explicit test/caller override wins.
    2. ``NO_COLOR`` env var set → no color (per https://no-color.org).
    3. ``CLAUDE_MENU_COLOR=0`` → no color (project-specific override).
    4. ``CLAUDE_MENU_COLOR=1`` → force color even when other signals say no.
    5. ``TERM=dumb`` / ``TERM`` unset → no color.
    6. ``COLORTERM`` set OR ``TERM_PROGRAM`` in known-color-IDE list → color.
    7. Default: color enabled. (We do NOT check isatty here — hook
       scripts emit to a pipe that goes to the user's real terminal,
       so isatty(stdout) is False but the destination supports color.)
    """
    if override is not None:
        return override
    if "NO_COLOR" in os.environ:
        return False
    cmc = os.environ.get("CLAUDE_MENU_COLOR")
    if cmc == "0":
        return False
    if cmc == "1":
        return True
    term = os.environ.get("TERM", "").strip().lower()
    if term in _DUMB_TERMS:
        return False
    if os.environ.get("COLORTERM"):
        return True
    if os.environ.get("TERM_PROGRAM") in _KNOWN_COLOR_IDE_TERMS:
        return True
    # Conservative default: if TERM was set to something non-dumb,
    # assume the terminal supports at least 8-color ANSI.
    return True


def should_use_unicode_boxes(*, override: bool | None = None) -> bool:
    """Return True iff the terminal can render Unicode box-drawing chars.

    Decision order:

    1. ``override`` argument.
    2. ``CLAUDE_MENU_ASCII=1`` → force ASCII boxes.
    3. ``TERM=dumb`` / unset → ASCII (no escape support implies primitive).
    4. ``LANG`` / ``LC_ALL`` / ``LC_CTYPE`` contains "UTF-8" (case-insensitive) → unicode.
    5. ``PYTHONIOENCODING`` starts with "utf" → unicode.
    6. ``sys.stdout.encoding`` starts with "utf" → unicode.
    7. Default: unicode. (Modern terminals are overwhelmingly UTF-8;
       falling back to ASCII by default would punish 99%+ of users.)
    """
    if override is not None:
        return override
    if os.environ.get("CLAUDE_MENU_ASCII") == "1":
        return False
    term = os.environ.get("TERM", "").strip().lower()
    if term in _DUMB_TERMS:
        return False
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = os.environ.get(var, "").lower()
        if "utf" in val:
            return True
        if val and val not in ("c", "posix"):
            # Some locale set, just not UTF — be optimistic.
            return True
    pio = os.environ.get("PYTHONIOENCODING", "").lower()
    if pio.startswith("utf"):
        return True
    try:
        enc = (getattr(sys.stdout, "encoding", "") or "").lower()
        if enc.startswith("utf"):
            return True
    except (AttributeError, ValueError):
        pass
    # Fall through to optimistic default — modern terminals are UTF-8.
    return True


def color(text: str, role: str, *, force: bool | None = None) -> str:
    """Wrap ``text`` in the ANSI code for ``role`` if color is enabled.

    ``force=True`` always applies; ``force=False`` always skips;
    ``force=None`` (default) consults ``should_use_color()``.
    """
    if force is None:
        force = should_use_color()
    if not force:
        return text
    code = PALETTE.get(role, "")
    if not code:
        return text
    return f"{code}{text}{RESET}"


def strip_ansi(text: str) -> str:
    """Remove every ANSI CSI escape from ``text``."""
    return _ANSI_RE.sub("", text)
