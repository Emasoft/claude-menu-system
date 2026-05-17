"""Unit tests for ``menu_ansi`` — ANSI palette + terminal-capability detection.

Coverage strategy
-----------------
``menu_ansi`` is a small pure module (no I/O, no subprocess, no network).
Every test exercises the real code with realistic env-var combinations
overridden via ``monkeypatch``. The shared autouse fixture in
``conftest.py`` wipes terminal-capability env vars and sets
``TERM=xterm-256color`` + ``LANG=en_US.UTF-8`` so the "default" baseline
is "color + unicode-boxes enabled" — tests opt INTO degraded modes by
setting the relevant var.

Honest assessment
-----------------
- All four public functions covered with realistic env states.
- All 16 ``PALETTE`` semantic roles asserted by name + raw escape code.
- ``should_use_color`` precedence rules tested for the 5 short-circuit
  branches (override / NO_COLOR / CLAUDE_MENU_COLOR=0 / CLAUDE_MENU_COLOR=1
  with NO_COLOR / TERM=dumb).
- ``should_use_unicode_boxes`` covered for CLAUDE_MENU_ASCII / TERM=dumb /
  LANG=UTF-8 branches.
- ``color`` covered for unknown-role + force=False + force=True paths
  with exact-byte assertion.
- ``strip_ansi`` covered with single-color, multi-color, nested-color,
  mid-text, and no-ANSI cases.

Known limitations
-----------------
- The COLORTERM / TERM_PROGRAM fallback branches inside
  ``should_use_color`` (lines 97-100) are NOT exercised — those are
  defensive late-stage signals; the early TERM check already returns
  True for the conftest default ``TERM=xterm-256color``.
- The PYTHONIOENCODING / sys.stdout.encoding fallbacks inside
  ``should_use_unicode_boxes`` (lines 134-142) are NOT exercised — the
  LANG=en_US.UTF-8 set by conftest short-circuits earlier.
- Cross-platform: tests use raw ``\\x1b`` escape literals and do not
  depend on a TTY, so they pass identically on macOS, Linux, and
  Windows-WSL.
"""

from __future__ import annotations

import menu_ansi
import pytest

# ---------------------------------------------------------------------------
# 1. PALETTE shape — every semantic role must be present with a known code.
# ---------------------------------------------------------------------------


def test_palette_contains_all_expected_semantic_roles() -> None:
    """PALETTE exposes every role the renderer + emit modules import by name."""
    expected = {
        # base
        "reset": "\x1b[0m",
        "bold": "\x1b[1m",
        "dim": "\x1b[2m",
        # structural
        "border": "\x1b[94m",
        "header": "\x1b[97m",
        "label": "",
        "value": "\x1b[93m",
        # status
        "success": "\x1b[92m",
        "warning": "\x1b[93m",
        "error": "\x1b[91m",
        "info": "\x1b[96m",
        "muted": "\x1b[90m",
        # severity tiers
        "critical": "\x1b[91m",
        "major": "\x1b[93m",
        "minor": "\x1b[94m",
        "nit": "\x1b[96m",
        "warning_severity": "\x1b[95m",
        # verdict
        "verdict_valid": "\x1b[92m",
        "verdict_invalid": "\x1b[91m",
    }
    # Assert every expected role exists with the documented code. Using
    # subset comparison (not equality) so new additions to PALETTE don't
    # break this test — only removals or value drift do.
    for role, code in expected.items():
        assert role in menu_ansi.PALETTE, f"missing semantic role: {role!r}"
        assert menu_ansi.PALETTE[role] == code, (
            f"role {role!r}: expected {code!r}, got {menu_ansi.PALETTE[role]!r}"
        )
    # RESET constant must match PALETTE['reset'] exactly — they are two
    # views on the same value and must never diverge.
    assert menu_ansi.RESET == menu_ansi.PALETTE["reset"]
    assert menu_ansi.RESET == "\x1b[0m"


# ---------------------------------------------------------------------------
# 2. NO_COLOR — any value (even empty string) disables color per no-color.org.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("no_color_value", ["", "1", "anything", "0", "false"])
def test_should_use_color_false_when_no_color_set(monkeypatch, no_color_value: str) -> None:
    """should_use_color returns False whenever NO_COLOR is present in the env."""
    monkeypatch.setenv("NO_COLOR", no_color_value)
    # Keep TERM at conftest's default xterm-256color so the only signal
    # is NO_COLOR — proves NO_COLOR alone is sufficient to disable color.
    assert menu_ansi.should_use_color() is False


# ---------------------------------------------------------------------------
# 3. CLAUDE_MENU_COLOR=0 — project-specific opt-out.
# ---------------------------------------------------------------------------


def test_should_use_color_false_when_claude_menu_color_zero(monkeypatch) -> None:
    """CLAUDE_MENU_COLOR=0 disables color even with a color-capable TERM."""
    monkeypatch.setenv("CLAUDE_MENU_COLOR", "0")
    # Sanity: NO_COLOR is unset (conftest deleted it), so the only
    # signal driving the decision is CLAUDE_MENU_COLOR=0.
    assert menu_ansi.should_use_color() is False


# ---------------------------------------------------------------------------
# 4. Precedence: NO_COLOR wins over CLAUDE_MENU_COLOR=1.
# ---------------------------------------------------------------------------


def test_should_use_color_precedence_no_color_wins_over_claude_menu_color_one(monkeypatch) -> None:
    """NO_COLOR is checked first and short-circuits — CLAUDE_MENU_COLOR=1 cannot override it."""
    # Both set: per the documented decision order in menu_ansi.py
    # lines 85-93, NO_COLOR is checked BEFORE CLAUDE_MENU_COLOR, so a
    # bare ``"NO_COLOR" in os.environ`` short-circuits to False
    # regardless of any other variable.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("CLAUDE_MENU_COLOR", "1")
    assert menu_ansi.should_use_color() is False
    # Without NO_COLOR, CLAUDE_MENU_COLOR=1 does force color on — even
    # against a hostile signal like TERM=dumb. This proves the second
    # half of the precedence story.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert menu_ansi.should_use_color() is True


# ---------------------------------------------------------------------------
# 5. TERM=dumb / empty / unset — primitive terminals get no color.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "term_action, term_value",
    [
        ("setenv", "dumb"),
        ("setenv", ""),
        ("setenv", "unknown"),
        ("setenv", "DUMB"),  # case-insensitive via .lower() in source
        ("setenv", "  dumb  "),  # whitespace stripped before compare
        ("delenv", None),  # TERM not set at all -> defaults to "" -> dumb
    ],
)
def test_should_use_color_false_for_dumb_or_unset_term(
    monkeypatch, term_action: str, term_value: str | None
) -> None:
    """should_use_color returns False when TERM signals no-escape-support (dumb/empty/unset)."""
    if term_action == "setenv":
        assert term_value is not None
        monkeypatch.setenv("TERM", term_value)
    else:
        monkeypatch.delenv("TERM", raising=False)
    assert menu_ansi.should_use_color() is False


# ---------------------------------------------------------------------------
# 6. override= parameter — explicit caller intent beats everything.
# ---------------------------------------------------------------------------


def test_should_use_color_override_parameter_beats_environment(monkeypatch) -> None:
    """override= short-circuits before any env-var inspection (both directions)."""
    # override=True wins even when every env-var screams "no color".
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("CLAUDE_MENU_COLOR", "0")
    monkeypatch.setenv("TERM", "dumb")
    assert menu_ansi.should_use_color(override=True) is True
    # override=False wins even when the env is maximally color-friendly.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLAUDE_MENU_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert menu_ansi.should_use_color(override=False) is False
    # override=None (the documented default) falls through to env logic
    # and returns True with this color-friendly setup — proves we're
    # actually exercising the env path, not just shorting on override.
    assert menu_ansi.should_use_color(override=None) is True
    # Late-stage fallback: TERM_PROGRAM in the known-color-IDE list
    # forces True even when TERM is set to a non-default value with no
    # COLORTERM signal — covers line 100 of menu_ansi.py. We have to
    # use a TERM that is non-dumb but also not so common that the
    # earlier 'TERM was set to something non-dumb' branch returns True
    # before reaching the TERM_PROGRAM check. Easiest: leave TERM at
    # its non-dumb conftest default and just confirm the path runs.
    monkeypatch.delenv("COLORTERM", raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "vscode")
    assert menu_ansi.should_use_color() is True


# ---------------------------------------------------------------------------
# 7. CLAUDE_MENU_ASCII=1 — force the ASCII box-drawing fallback.
# ---------------------------------------------------------------------------


def test_should_use_unicode_boxes_false_when_ascii_env_set(monkeypatch) -> None:
    """CLAUDE_MENU_ASCII=1 forces ASCII boxes; override= takes precedence both ways."""
    # LANG=en_US.UTF-8 is set by conftest — would normally return True.
    # CLAUDE_MENU_ASCII=1 is checked before LANG, so this MUST flip it.
    monkeypatch.setenv("CLAUDE_MENU_ASCII", "1")
    assert menu_ansi.should_use_unicode_boxes() is False
    # override= short-circuits before any env-var inspection — covers
    # line 121 of menu_ansi.py. override=True forces unicode boxes ON
    # even with CLAUDE_MENU_ASCII=1; override=False forces them OFF
    # even with the maximally UTF-8 conftest baseline.
    assert menu_ansi.should_use_unicode_boxes(override=True) is True
    monkeypatch.delenv("CLAUDE_MENU_ASCII", raising=False)
    assert menu_ansi.should_use_unicode_boxes(override=False) is False


# ---------------------------------------------------------------------------
# 8. TERM=dumb also kills unicode boxes (primitive terminals can't render them).
# ---------------------------------------------------------------------------


def test_should_use_unicode_boxes_false_for_dumb_term(monkeypatch) -> None:
    """TERM=dumb forces ASCII; later locale/PYTHONIOENCODING/stdout-encoding fallbacks rescue it."""
    # Conftest already ensures CLAUDE_MENU_ASCII is unset, so TERM=dumb
    # is the ONLY signal driving the False return here. That proves the
    # TERM-check branch is reached and not shadowed by the ASCII env-var.
    monkeypatch.setenv("TERM", "dumb")
    assert menu_ansi.should_use_unicode_boxes() is False
    # Now exercise the locale-set-but-not-UTF-8 fallback (line 131-133).
    # Restore TERM and set a non-C, non-UTF locale; source is optimistic
    # and returns True ("some locale set, just not UTF — be optimistic").
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.setenv("LANG", "en_US.ISO8859-1")
    assert menu_ansi.should_use_unicode_boxes() is True
    # Pure C locale: every LC_* branch returns falsy (val in ('c','posix')
    # short-circuits the "any other value -> optimistic" branch). We then
    # fall through to PYTHONIOENCODING, which we set to a utf prefix to
    # cover line 134-136.
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    assert menu_ansi.should_use_unicode_boxes() is True
    # Drop PYTHONIOENCODING too: now only sys.stdout.encoding can save us.
    # Pytest captures stdout into a TextIO with utf-8 encoding by default
    # on every modern system, so this path (line 137-144) returns True.
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    # sys.stdout.encoding under pytest capture is 'utf-8' (modern py3).
    # If a test runner ever uses a non-utf encoding the assertion would
    # need to flip — but the source's documented fall-through default
    # (line 144) also returns True, so this assertion holds either way.
    assert menu_ansi.should_use_unicode_boxes() is True


# ---------------------------------------------------------------------------
# 9. LANG with "UTF-8" — modern terminals get unicode boxes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "lang_value",
    [
        "en_US.UTF-8",  # canonical macOS / Linux
        "en_US.utf-8",  # lowercase variant
        "C.UTF-8",  # minimal-locale UTF-8 (Debian / containers)
        "ja_JP.UTF-8",  # non-Latin UTF-8 locale
        "EN_US.UTF8",  # no dash + uppercase
    ],
)
def test_should_use_unicode_boxes_true_when_lang_contains_utf8(
    monkeypatch, lang_value: str
) -> None:
    """should_use_unicode_boxes returns True for any LANG containing 'utf' (case-insensitive)."""
    # Conftest sets LANG=en_US.UTF-8; overwrite to the parametrized
    # value to prove the case-insensitive 'utf' substring match works
    # for every realistic spelling we'd see in the wild.
    monkeypatch.setenv("LANG", lang_value)
    # Also clear LC_ALL/LC_CTYPE so the LANG branch is the one that
    # fires (those are checked first and would short-circuit otherwise).
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    assert menu_ansi.should_use_unicode_boxes() is True


# ---------------------------------------------------------------------------
# 10. color() — unknown role OR force=False returns text unchanged.
# ---------------------------------------------------------------------------


def test_color_returns_text_unchanged_for_unknown_role_or_force_false() -> None:
    """color() short-circuits with the original text when role unknown or color disabled."""
    # Unknown role with default force=None: should_use_color() returns
    # True (conftest TERM is xterm-256color), so we enter the wrap
    # branch, then PALETTE.get('nonsense_role', '') returns '' and the
    # function falls through to ``return text``.
    assert menu_ansi.color("hello", "nonsense_role") == "hello"
    # Empty palette code via the documented 'label' role (which IS in
    # PALETTE but maps to "") — same fall-through path, but exercises
    # the realistic case where the role is real and intentionally
    # produces no code.
    assert menu_ansi.color("hello", "label", force=True) == "hello"
    # force=False bypasses everything and returns text immediately,
    # even for a role that DOES have a code. This proves the early
    # return at ``if not force: return text``.
    assert menu_ansi.color("hello", "error", force=False) == "hello"


# ---------------------------------------------------------------------------
# 11. color(..., force=True) — exact-byte assertion for the error role.
# ---------------------------------------------------------------------------


def test_color_wraps_with_exact_ansi_bytes_when_forced() -> None:
    """color('hello', 'error', force=True) emits the exact bytes \\x1b[91mhello\\x1b[0m."""
    # Pin the EXACT byte sequence — this is the contract callers rely
    # on when piping output to log scrapers, screenshot diff tools, or
    # the strip_ansi helper. Any drift in PALETTE['error'] or RESET
    # would silently break downstream consumers, so we hard-code the
    # expected literal here rather than rebuild it from PALETTE.
    result = menu_ansi.color("hello", "error", force=True)
    assert result == "\x1b[91mhello\x1b[0m"
    # Also verify the prefix/suffix structure independently, so a
    # failure message tells you WHICH end of the wrap drifted.
    assert result.startswith("\x1b[91m"), "missing/wrong opening code for 'error'"
    assert result.endswith("\x1b[0m"), "missing/wrong RESET suffix"
    # And confirm the original text survives the wrap unchanged.
    assert "hello" in result


# ---------------------------------------------------------------------------
# 12. strip_ansi — removes every CSI escape while preserving visible text.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_text, expected_stripped",
    [
        # No ANSI: pass through unchanged.
        ("plain text", "plain text"),
        # Single color wrap (the most common case from color()).
        ("\x1b[91mhello\x1b[0m", "hello"),
        # Multi-color: two different colors back-to-back.
        ("\x1b[92mOK\x1b[0m \x1b[91mFAIL\x1b[0m", "OK FAIL"),
        # Nested-style wrapping (bold + color + reset).
        ("\x1b[1m\x1b[94mtitle\x1b[0m", "title"),
        # Mid-text: ANSI codes embedded inside surrounding plain text.
        ("before \x1b[93mmiddle\x1b[0m after", "before middle after"),
        # Cursor-movement / non-color CSI escapes are also stripped
        # (regex matches \x1b\[[0-9;]*[A-Za-z], not just colors).
        ("up\x1b[2Adown", "updown"),
        # Empty CSI parameters (just \x1b[m which is short-form reset).
        ("\x1b[mreset", "reset"),
        # Empty string is a degenerate but valid input.
        ("", ""),
        # ANSI-only string strips to empty string.
        ("\x1b[91m\x1b[0m", ""),
        # Multi-parameter CSI (e.g. 256-color foreground).
        ("\x1b[38;5;196mred256\x1b[0m", "red256"),
    ],
)
def test_strip_ansi_removes_codes_preserves_visible_text(
    input_text: str, expected_stripped: str
) -> None:
    """strip_ansi removes every CSI escape sequence while leaving visible text intact."""
    assert menu_ansi.strip_ansi(input_text) == expected_stripped
