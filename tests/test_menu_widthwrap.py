"""Unit tests for ``scripts/menu_widthwrap.py``.

Coverage targets every public symbol plus the private
``_char_display_width`` oracle that the rest of the module depends on:

- ``_char_display_width(ch)`` — 5 char-class tests (control / CJK /
  emoji / BMP dingbats / combining marks).
- ``display_width(text)`` — ANSI stripping + mixed CJK/ASCII totals.
- ``pad(text, width, align)`` — left / right / center alignment plus
  the "no truncation when already wider" invariant.
- ``terminal_width(*, fallback)`` — ``COLUMNS`` honored, capped by
  ``CLAUDE_MENU_MAX_WIDTH``, ``shutil`` fallback path on ``OSError``.
- ``wrap_ansi(text, budget, cont_indent)`` — fits-in-budget short
  path, ANSI colour preservation across wraps, unbreakable-token
  hard wrap.
- ``line_safe_truncate(text, max_chars, indicator)`` — cuts at ``\n``
  boundaries only, appends the indicator on its own line.

All tests are deterministic: terminal-size detection is mocked, no
test reads the real terminal, no test depends on the developer's
shell environment (env vars are wiped by the autouse fixture in
``conftest.py``).
"""

from __future__ import annotations

import shutil
import unicodedata

import pytest
from menu_widthwrap import (
    RESET,
    _char_display_width,
    display_width,
    line_safe_truncate,
    pad,
    terminal_width,
    wrap_ansi,
)

# ---------------------------------------------------------------------------
# _char_display_width — 5 char-class tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ch", ["\x00", "\x1f", "\x7f"])
def test_char_display_width_control_chars_are_zero(ch: str) -> None:
    """Control chars below 0x20 and DEL (0x7F) occupy 0 display columns."""
    assert _char_display_width(ch) == 0


@pytest.mark.parametrize("ch", ["中", "ｗ", "あ"])
def test_char_display_width_cjk_fullwidth_is_two(ch: str) -> None:
    """East-Asian Wide and Full-width chars occupy 2 columns (matches unicodedata)."""
    assert unicodedata.east_asian_width(ch) in ("W", "F")
    assert _char_display_width(ch) == 2


@pytest.mark.parametrize(
    "ch",
    [
        "😀",  # U+1F600 — Emoticons range
        "🎉",  # U+1F389 — Misc Symbols & Pictographs range
        "🀀",  # U+1F000 — Mahjong tiles range
        "🌀",  # U+1F300 — start of Misc Symbols & Pictographs
    ],
)
def test_char_display_width_emoji_u1f600_range_is_two(ch: str) -> None:
    """Emoji in U+1F000 / U+1F300 / U+1F600 ranges occupy 2 columns."""
    assert _char_display_width(ch) == 2


@pytest.mark.parametrize("ch", ["✓", "✗", "⚠", "○", "◐", "⊝", "•"])
def test_char_display_width_bmp_dingbats_are_one(ch: str) -> None:
    """BMP Misc-Symbols and Dingbats render as 1 column in monospace fonts."""
    assert _char_display_width(ch) == 1


def test_char_display_width_combining_mark_is_zero() -> None:
    """Combining acute (U+0301) is a Mn category mark and occupies 0 columns."""
    combining_acute = "́"
    assert unicodedata.category(combining_acute) == "Mn"
    assert _char_display_width(combining_acute) == 0


# ---------------------------------------------------------------------------
# display_width — ANSI strip + mixed CJK/ASCII
# ---------------------------------------------------------------------------


def test_display_width_strips_ansi_before_counting() -> None:
    """ANSI SGR escapes are not counted toward the visible width."""
    assert display_width("\x1b[91mhi\x1b[0m") == 2


def test_display_width_mixed_cjk_and_ascii_sums_correctly() -> None:
    """Mixed-script string ``a中b`` totals 1+2+1 = 4 columns."""
    assert display_width("a中b") == 4


# ---------------------------------------------------------------------------
# pad — alignment + no-truncation invariant
# ---------------------------------------------------------------------------


def test_pad_left_align_default_pads_on_right() -> None:
    """Default alignment is left — spaces are appended after the text."""
    assert pad("hi", 6) == "hi    "


def test_pad_right_align_prepends_spaces() -> None:
    """Right alignment pushes content to the right edge of the cell."""
    assert pad("hi", 6, align="right") == "    hi"


def test_pad_center_align_splits_evenly_with_right_bias_on_odd() -> None:
    """Center alignment puts ``pad // 2`` spaces left, the remainder right."""
    # width=7, text width=2 → pad=5 → 2 left, 3 right
    assert pad("hi", 7, align="center") == "  hi   "


def test_pad_returns_text_unchanged_when_already_wide_enough() -> None:
    """``pad`` never truncates — wider-than-target text is returned as-is."""
    text = "this string is wide"
    assert pad(text, 5) == text


# ---------------------------------------------------------------------------
# terminal_width — COLUMNS, cap, fallback
# ---------------------------------------------------------------------------


def test_terminal_width_honors_columns_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """``COLUMNS`` env var wins over ``shutil.get_terminal_size`` detection."""
    monkeypatch.setenv("COLUMNS", "73")
    assert terminal_width() == 73


def test_terminal_width_capped_at_claude_menu_max_width(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CLAUDE_MENU_MAX_WIDTH`` caps the result; malformed env vars fall back gracefully."""
    monkeypatch.setenv("CLAUDE_MENU_MAX_WIDTH", "120")
    monkeypatch.setenv("COLUMNS", "200")
    assert terminal_width() == 120
    # Malformed CLAUDE_MENU_MAX_WIDTH → default cap of 120 (exercises ValueError branch).
    monkeypatch.setenv("CLAUDE_MENU_MAX_WIDTH", "not-a-number")
    monkeypatch.setenv("COLUMNS", "200")
    assert terminal_width() == 120
    # Malformed COLUMNS → silently ignored, falls through to shutil/fallback path.
    monkeypatch.setenv("CLAUDE_MENU_MAX_WIDTH", "120")
    monkeypatch.setenv("COLUMNS", "not-a-number")
    monkeypatch.setattr(
        shutil, "get_terminal_size", lambda _default: type("S", (), {"columns": 95})()
    )
    assert terminal_width() == 95


def test_terminal_width_falls_back_when_shutil_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``OSError`` from ``shutil.get_terminal_size`` falls back to the ``fallback`` param."""

    # Autouse fixture already deleted COLUMNS. Force shutil to raise OSError.
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise OSError("no tty")

    monkeypatch.setattr(shutil, "get_terminal_size", _raise)
    # fallback=80 < default cap of 120 → returns 80
    assert terminal_width(fallback=80) == 80


# ---------------------------------------------------------------------------
# wrap_ansi — fits-in-budget, colour preservation, hard wrap
# ---------------------------------------------------------------------------


def test_wrap_ansi_returns_single_line_when_text_fits() -> None:
    """Text whose display width is within budget is returned as a single-line list."""
    result = wrap_ansi("hello", 80)
    assert result == ["hello"]


def test_wrap_ansi_preserves_ansi_color_across_wraps() -> None:
    """Wrapped coloured text re-emits the active SGR code on each continuation line."""
    red = "\x1b[91m"
    text = f"{red}this is a long colored sentence{RESET}"
    lines = wrap_ansi(text, 10)
    # Multi-line output — the input is 31 visible cols, budget is 10.
    assert len(lines) > 1, f"expected multiple lines, got {lines!r}"
    # First line carries the opening SGR; continuation lines re-emit it.
    assert lines[0].startswith(red), f"first line lost colour: {lines[0]!r}"
    for cont in lines[1:]:
        assert red in cont, f"continuation line lost colour: {cont!r}"
    # Every coloured line ends with a RESET so wrap boundaries don't bleed
    # the colour into surrounding indent.
    for line in lines:
        assert line.endswith(RESET), f"line missing RESET suffix: {line!r}"


def test_wrap_ansi_hard_wraps_unbreakable_token_longer_than_budget() -> None:
    """A single token wider than ``budget`` is hard-wrapped; degenerate inputs short-circuit."""
    url = "https://example.com/very/long/path/that/cannot/break"
    budget = 10
    lines = wrap_ansi(url, budget)
    # Hard wrap → at least two lines, and the input had no whitespace
    # so every line is a slice of the original token.
    assert len(lines) > 1, f"expected hard wrap, got {lines!r}"
    # No line should exceed the budget in display width.
    for line in lines:
        assert display_width(line) <= budget, (
            f"line exceeds budget {budget}: {line!r} width={display_width(line)}"
        )
    # Reassembling the lines reproduces the original token (no whitespace lost).
    assert "".join(lines) == url
    # Degenerate budgets short-circuit to a single-line passthrough.
    assert wrap_ansi(url, 0) == [url]
    assert wrap_ansi(url, -5) == [url]
    # Negative cont_indent is clamped to 0 (smoke-tests the guard).
    sentence = "alpha beta gamma delta epsilon zeta"
    multi = wrap_ansi(sentence, budget=12, cont_indent=-3)
    assert len(multi) > 1
    # No continuation line should start with negative-indent garbage.
    for cont in multi[1:]:
        assert not cont.startswith(" " * 99)


# ---------------------------------------------------------------------------
# line_safe_truncate — cuts only at \n boundaries
# ---------------------------------------------------------------------------


def test_line_safe_truncate_cuts_at_newline_boundaries_only() -> None:
    """Truncation drops whole lines, appends the indicator on its own final line."""
    text = "a\nb\nc\nd\ne"  # len=9
    out = line_safe_truncate(text, max_chars=8, indicator="…")
    # Indicator must be present as the final line.
    assert out.endswith("…"), f"missing indicator: {out!r}"
    # Output is a join of complete original lines + '\n…'. No partial line.
    lines = out.split("\n")
    assert lines[-1] == "…", f"indicator not on its own line: {lines!r}"
    # Every kept line (everything before the indicator) must be an original line.
    original_lines = set(text.split("\n"))
    for kept in lines[:-1]:
        assert kept in original_lines, f"partial line emitted: {kept!r}"
    # Short text within budget is returned unchanged (early-return branch).
    assert line_safe_truncate("ok", max_chars=10) == "ok"
    # Indicator longer than max_chars → fallback char-truncate (budget <= 0 branch).
    assert line_safe_truncate("abcdef", max_chars=2, indicator="…[truncated]") == "ab"
    # Single line longer than budget, no \n to cut on → char-truncate path.
    single = "x" * 100
    out_single = line_safe_truncate(single, max_chars=20, indicator="…")
    assert out_single.endswith("\n…")
    # The char-truncate keeps `budget` chars before the indicator newline.
    assert out_single.startswith("x")
    assert out_single.count("x") <= 20  # within max_chars bound
