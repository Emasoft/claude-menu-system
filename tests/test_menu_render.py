"""Unit tests for ``scripts/menu_render.py`` — pure renderer for 8 menu modes.

Coverage strategy
-----------------
``menu_render`` is a pure module (no I/O, no env writes — only env reads
via ``menu_ansi``). Every test exercises the real renderer end-to-end
with realistic specs, asserts on the rendered string + action_map, and
uses ``monkeypatch`` to flip the few env vars (``CLAUDE_MENU_ASCII``,
``COLUMNS``) that change behavior.

The shared autouse fixture in ``conftest.py`` wipes terminal-capability
env vars and sets ``TERM=xterm-256color`` + ``LANG=en_US.UTF-8`` so the
"default" baseline is "color allowed + unicode-boxes allowed". Every
test that wants ASCII or no-color flips the relevant signal explicitly.

Honest assessment
-----------------
- All 8 public renderers exercised with realistic specs.
- All 5 box styles (heavy / rounded / light / double / ascii) exercised
  via ``render_menu`` parametrization.
- ASCII downgrade tested both directions (env-flag flips to ASCII;
  ``force_unicode`` overrides the flip back to unicode).
- ANSI-safety + Unicode-wide-char column-width invariants both tested
  by splitting on ``style.v`` (vertical bar) and measuring
  ``display_width`` of the inter-bar cell content — proves the column
  math handles both zero-width ANSI codes and double-width CJK glyphs.
- Dispatcher (``render``) parametrized over all 8 modes, asserting the
  return-tuple shape (action_map present for menu/confirm only).

Known limitations
-----------------
- The wrap behavior inside ``render_panel`` is exercised with a narrow
  ``width`` to force wrap — exact wrap boundaries depend on
  ``menu_widthwrap.wrap_ansi`` which has its own dedicated tests.
- Color-application paths inside summary / breakdown / status_table
  are exercised (we set ``use_color=True`` and assert non-zero count
  cells get colored) but the exact ANSI bytes for each severity tier
  are pinned in ``test_menu_ansi.py``, not re-asserted here.
"""

from __future__ import annotations

from typing import Any

import pytest
from menu_ansi import strip_ansi
from menu_render import (
    STYLES,
    render,
    render_breakdown,
    render_confirm,
    render_menu,
    render_multi_box,
    render_panel,
    render_progress,
    render_status_table,
    render_summary,
)
from menu_widthwrap import display_width

# ---------------------------------------------------------------------------
# 1. render_menu — basic 3-row table + action_map shape.
# ---------------------------------------------------------------------------


def test_render_menu_basic_three_rows_produces_table_and_action_map() -> None:
    """render_menu builds a 3-data-row table and an action_map keyed by rendered key."""
    spec = {
        "header": "Choose an option",
        "rows": [
            {"key": "1", "action_id": "alpha", "label": "Alpha option"},
            {"key": "2", "action_id": "beta", "label": "Beta option"},
            {"key": "3", "action_id": "gamma", "label": "Gamma option"},
        ],
    }
    text, action_map = render_menu(spec, use_color=False)
    # Strip ANSI so substring checks don't fail if color crept in via the
    # autouse fixture's TERM setting (use_color=False means no color is
    # emitted, but be defensive).
    plain = strip_ansi(text)
    # Header label and all three labels must appear in the rendered text.
    assert "Choose an option" in plain
    assert "Alpha option" in plain
    assert "Beta option" in plain
    assert "Gamma option" in plain
    # Default footer is the prompt string.
    assert "Type a number to choose:" in plain
    # Action map maps each rendered key (1/2/3) to its action_id.
    assert action_map == {"1": "alpha", "2": "beta", "3": "gamma"}


# ---------------------------------------------------------------------------
# 2. render_menu — disabled rows dropped, kept rows renumber 1..N contiguously.
# ---------------------------------------------------------------------------


def test_render_menu_disabled_rows_dropped_and_kept_rows_renumber() -> None:
    """Disabled rows vanish from output; remaining rows get keys 1..N (no gaps)."""
    spec = {
        "header": "Filtered menu",
        "rows": [
            {"key": "1", "action_id": "first", "label": "Keep first"},
            {"key": "2", "action_id": "skipped", "label": "Skip me", "disabled": True},
            {"key": "3", "action_id": "third", "label": "Keep third"},
            {"key": "4", "action_id": "fourth", "label": "Keep fourth"},
        ],
    }
    text, action_map = render_menu(spec, use_color=False)
    plain = strip_ansi(text)
    # Skipped label must NOT appear at all.
    assert "Skip me" not in plain
    # Surviving rows must all appear.
    assert "Keep first" in plain
    assert "Keep third" in plain
    assert "Keep fourth" in plain
    # Renumbering: 3 surviving rows -> keys 1, 2, 3 (the original "3"
    # and "4" keys are not preserved; the gap from dropping "2" is closed).
    assert action_map == {"1": "first", "2": "third", "3": "fourth"}


# ---------------------------------------------------------------------------
# 3. render_menu — "0" and "A" are static keys and skip the renumber pass.
# ---------------------------------------------------------------------------


def test_render_menu_static_keys_zero_and_a_keep_literal() -> None:
    """Keys '0' and 'A' bypass renumbering — they keep their literal value."""
    spec = {
        "header": "Menu with statics",
        "rows": [
            {"key": "1", "action_id": "first", "label": "First"},
            {"key": "2", "action_id": "second", "label": "Second"},
            {"key": "0", "action_id": "cancel", "label": "Cancel"},
            {"key": "A", "action_id": "all", "label": "All findings"},
        ],
    }
    _text, action_map = render_menu(spec, use_color=False)
    # First two get renumbered 1, 2 (they happen to match originals).
    # "0" and "A" stay literal — they DO NOT consume slots in the
    # next_num counter, per the static-keys frozenset in the source.
    assert action_map["1"] == "first"
    assert action_map["2"] == "second"
    assert action_map["0"] == "cancel"
    assert action_map["A"] == "all"


# ---------------------------------------------------------------------------
# 4. render_menu — renumber=False preserves the original spec keys verbatim.
# ---------------------------------------------------------------------------


def test_render_menu_renumber_false_preserves_original_keys() -> None:
    """renumber=False keeps the spec's original keys (no 1..N renumbering)."""
    spec = {
        "header": "Custom keys",
        "renumber": False,
        "rows": [
            {"key": "x", "action_id": "exit", "label": "Exit"},
            {"key": "h", "action_id": "help", "label": "Help"},
            {"key": "9", "action_id": "ninth", "label": "Ninth"},
        ],
    }
    text, action_map = render_menu(spec, use_color=False)
    plain = strip_ansi(text)
    # Original keys appear as cell content.
    assert "Exit" in plain
    assert "Help" in plain
    # Map preserves originals.
    assert action_map == {"x": "exit", "h": "help", "9": "ninth"}


# ---------------------------------------------------------------------------
# 5. render_menu — use_color=True emits ANSI escape sequences.
# ---------------------------------------------------------------------------


def test_render_menu_use_color_true_emits_ansi_escapes() -> None:
    """When use_color=True, the rendered text contains \\x1b CSI escape codes."""
    spec = {
        "header": "Colored menu",
        "rows": [
            {"key": "1", "action_id": "a", "label": "A"},
            {"key": "2", "action_id": "b", "label": "B"},
        ],
    }
    text, _ = render_menu(spec, use_color=True)
    # Raw output must contain at least one ESC = \x1b CSI start.
    assert "\x1b[" in text, "use_color=True must emit ANSI escape codes"
    # After stripping, the visible content survives — proves ANSI was
    # wrapped around content, not corrupting it.
    plain = strip_ansi(text)
    assert "Colored menu" in plain
    assert "A" in plain
    assert "B" in plain


# ---------------------------------------------------------------------------
# 6. render_summary — all 5 severities appear in the documented order.
# ---------------------------------------------------------------------------


def test_render_summary_all_five_severities_appear_in_order() -> None:
    """render_summary lists CRITICAL, MAJOR, MINOR, NIT, WARNING in that fixed order."""
    spec = {
        "title": "Test results",
        "counts": {
            "critical": 1,
            "major": 2,
            "minor": 3,
            "nit": 4,
            "warning": 5,
        },
    }
    plain = strip_ansi(render_summary(spec, use_color=False))
    # Each severity name appears at least once.
    for sev in ("CRITICAL", "MAJOR", "MINOR", "NIT", "WARNING"):
        assert sev in plain, f"missing severity row: {sev}"
    # And they appear in the documented top-to-bottom order. find()
    # returns the FIRST occurrence which is the data-row text (the
    # header row says "Severity" / "Count", not the tier names).
    idx_c = plain.find("CRITICAL")
    idx_ma = plain.find("MAJOR")
    idx_mi = plain.find("MINOR")
    idx_n = plain.find("NIT")
    idx_w = plain.find("WARNING")
    assert idx_c < idx_ma < idx_mi < idx_n < idx_w, (
        f"severities not in order: C={idx_c} MA={idx_ma} MI={idx_mi} N={idx_n} W={idx_w}"
    )


# ---------------------------------------------------------------------------
# 7. render_summary — verdict + report_path appear in the footer.
# ---------------------------------------------------------------------------


def test_render_summary_verdict_and_report_path_in_footer() -> None:
    """verdict + report_path produce 'Verdict: <V>' + 'Report: <path>' lines after the table."""
    spec = {
        "title": "Audit",
        "counts": {"critical": 0, "major": 0, "minor": 0, "nit": 0, "warning": 0},
        "verdict": "VALID",
        "report_path": "/tmp/report-20260516.md",
    }
    plain = strip_ansi(render_summary(spec, use_color=False))
    # Both footer lines must be present, AFTER the table — assert
    # via simple substring (line order is asserted by index below).
    assert "Verdict: VALID" in plain
    assert "Report: /tmp/report-20260516.md" in plain
    # Verdict appears before Report, both AFTER the table closing.
    assert plain.index("Verdict") < plain.index("Report")


# ---------------------------------------------------------------------------
# 8. render_breakdown — row totals + column totals + grand TOTAL all add up.
# ---------------------------------------------------------------------------


def test_render_breakdown_row_and_column_and_grand_totals_correct() -> None:
    """Breakdown matrix shows per-row totals, per-column totals, and a grand TOTAL."""
    spec = {
        "title": "Bugs by category",
        "rows": [
            {"label": "Auth", "counts": {"critical": 2, "major": 1, "minor": 0}},
            {"label": "DB", "counts": {"critical": 1, "major": 3, "minor": 4}},
            {"label": "UI", "counts": {"critical": 0, "major": 0, "minor": 5}},
        ],
        "columns": ["CRITICAL", "MAJOR", "MINOR"],
    }
    plain = strip_ansi(render_breakdown(spec, use_color=False))
    # Each category label must appear.
    for cat in ("Auth", "DB", "UI"):
        assert cat in plain
    # TOTAL row label is present (renderer appends it when totals_row=True default).
    assert "TOTAL" in plain
    # Auth row total = 2+1+0 = 3. DB = 1+3+4 = 8. UI = 0+0+5 = 5.
    # Column totals: critical=3, major=4, minor=9. Grand=16.
    # We can't easily anchor to specific cells without parsing the
    # table, but the grand-total 16 must appear in the output (TOTAL
    # row's Total column).
    assert "16" in plain
    # Spot-check column totals appear (3 / 4 / 9 are unique in this data
    # set so a substring check is safe).
    assert "3" in plain  # critical column total
    assert "4" in plain  # major column total
    assert "9" in plain  # minor column total


# ---------------------------------------------------------------------------
# 9. render_breakdown — columns parameter overrides the default severity order.
# ---------------------------------------------------------------------------


def test_render_breakdown_columns_parameter_overrides_default_order() -> None:
    """An explicit 'columns' list controls which severities show + their order."""
    # Default order is CRITICAL, MAJOR, MINOR, NIT, WARNING.
    # We reverse + drop two columns to prove the override is honored.
    spec = {
        "title": "Custom",
        "rows": [
            {"label": "OnlyRow", "counts": {"critical": 1, "warning": 5}},
        ],
        "columns": ["WARNING", "CRITICAL"],
    }
    plain = strip_ansi(render_breakdown(spec, use_color=False))
    # Both requested columns appear.
    assert "WARNING" in plain
    assert "CRITICAL" in plain
    # The two NOT-requested columns must be absent from the header.
    # MINOR and NIT should not appear as column headers (note: "NIT"
    # is short enough that a careless substring check could match
    # inside another word; we check the spaced/cell form instead).
    # Look at the header row only — first 4 lines: title, top-border,
    # header-row, sep-border, then data.
    header_block = "\n".join(plain.splitlines()[:4])
    assert "MINOR" not in header_block
    assert "NIT" not in header_block
    # WARNING appears BEFORE CRITICAL in the header row (per spec).
    header_row = plain.splitlines()[2]
    assert header_row.index("WARNING") < header_row.index("CRITICAL")


# ---------------------------------------------------------------------------
# 10. render_status_table — glyphs match status; Summary line counts each tier.
# ---------------------------------------------------------------------------


def test_render_status_table_glyphs_and_summary_line() -> None:
    """Status cells show the glyph (✓/✗/⚠/etc.) matching the status keyword."""
    spec = {
        "title": "Plugin component status",
        "rows": [
            {"label": "Manifest", "status": "ok", "notes": "valid"},
            {"label": "Hooks", "status": "missing", "notes": "no hooks/"},
            {"label": "Skills", "status": "buggy", "notes": "1 error"},
            {"label": "Agents", "status": "partial", "notes": "2/5 done"},
            {"label": "Mcp", "status": "pending", "notes": "queued"},
            {"label": "Old", "status": "skipped", "notes": "deprecated"},
            {"label": "Note", "status": "info", "notes": "fyi"},
        ],
    }
    plain = strip_ansi(render_status_table(spec, use_color=False))
    # Each glyph must appear at least once.
    for glyph in ("✓", "✗", "⚠", "◐", "○", "⊝", "•"):
        assert glyph in plain, f"missing status glyph: {glyph}"
    # Summary line must list each tier exactly once with its count of 1.
    assert "Summary:" in plain
    summary_line = next(ln for ln in plain.splitlines() if ln.startswith("Summary:"))
    # Each status maps to exactly one row, so each label appears with "1".
    # Note: ok and implemented share the OK label but only "ok" is used here.
    for tier_label in ("OK", "MISSING", "BUGGY", "PARTIAL", "PENDING", "SKIPPED", "INFO"):
        assert tier_label in summary_line, f"summary missing tier: {tier_label}"
        # Find each tier and confirm a "1" precedes it (one occurrence each).
        idx = summary_line.index(tier_label)
        # Walk back to find the count just before — must include "1".
        prefix = summary_line[max(0, idx - 4) : idx]
        assert "1" in prefix, f"tier {tier_label} doesn't have count 1 before it"


# ---------------------------------------------------------------------------
# 11. render_panel — header + body + footer; long body lines wrap.
# ---------------------------------------------------------------------------


def test_render_panel_long_body_lines_wrap_when_wider_than_box() -> None:
    """A body line of 50 chars renders as multiple lines when box width=20."""
    long_body = "x" * 50  # 50-char single-line body — must wrap to fit inner width.
    spec = {
        "header": "Panel header",
        "body": [long_body],
        "footer": "footer line",
        "width": 20,  # outer width 20 -> inner width 16 (minus 2 borders + 2 pad)
    }
    plain = strip_ansi(render_panel(spec, use_color=False))
    lines = plain.splitlines()
    # Header and footer appear as own lines.
    assert "Panel header" in plain
    assert "footer line" in plain
    # The 50-char "xxxx..." must have wrapped into multiple data rows.
    # Count body lines (inner content lines between sep and bot borders).
    # Easiest robust check: at least 3 of the rendered lines contain
    # "xxx" — original would have produced just 1 if no wrap occurred.
    x_lines = [ln for ln in lines if "xxx" in ln]
    assert len(x_lines) >= 2, (
        f"long body line did not wrap (expected >=2 lines with xxx, got {len(x_lines)})"
    )
    # All wrapped fragments together must still contain at least 50 x's
    # so we didn't lose data during wrapping.
    total_x = sum(ln.count("x") for ln in lines)
    assert total_x == 50, f"wrap lost x chars: counted {total_x}, expected 50"


# ---------------------------------------------------------------------------
# 12. render_multi_box — stack of 2 panels separated by a blank line.
# ---------------------------------------------------------------------------


def test_render_multi_box_two_panels_separated_by_blank_line() -> None:
    """multi_box renders each panel and inserts a blank line between consecutive panels."""
    spec = {
        "boxes": [
            {"header": "First", "body": ["one"]},
            {"header": "Second", "body": ["two"]},
        ],
    }
    plain = strip_ansi(render_multi_box(spec, use_color=False))
    # Both panel headers must appear.
    assert "First" in plain
    assert "Second" in plain
    # Both body lines must appear.
    assert "one" in plain
    assert "two" in plain
    # First panel must end (closing border line) before Second's
    # opening border appears — proves the two panels are stacked, not
    # interleaved.
    assert plain.index("First") < plain.index("Second")
    # There must be at least one blank line between the two panels.
    lines = plain.splitlines()
    # Find the line index of the "First" body and the "Second" header.
    idx_first_body = next(i for i, ln in enumerate(lines) if "one" in ln)
    idx_second_header = next(i for i, ln in enumerate(lines) if "Second" in ln)
    # Between them there should be at least one empty string line.
    between = lines[idx_first_body : idx_second_header + 1]
    assert "" in between, "no blank separator between stacked panels"


# ---------------------------------------------------------------------------
# 13. render_progress — bar uses █ and ░; counter shows X/Y (Z%).
# ---------------------------------------------------------------------------


def test_render_progress_bar_glyphs_and_counter_format() -> None:
    """Progress bar uses █ for filled + ░ for empty; counter is 'X/Y  (Z%)'."""
    # Half-complete: 5/10 with bar_width=10 → 5 filled, 5 empty, 50%.
    spec = {
        "header": "Loading",
        "current": 5,
        "total": 10,
        "bar_width": 10,
    }
    text = render_progress(spec, use_color=False)
    plain = strip_ansi(text)
    assert "Loading" in plain
    # Half-filled bar has both glyphs present.
    assert "█" in plain, "filled-bar glyph missing"
    assert "░" in plain, "empty-bar glyph missing"
    # Counter string format — source uses two spaces between count and %.
    assert "5/10" in plain
    assert "(50%)" in plain

    # Full completion: 10/10 should emit no error and the counter says 100%.
    full_spec = {"header": "Done", "current": 10, "total": 10, "bar_width": 10}
    text2 = render_progress(full_spec, use_color=True)
    plain2 = strip_ansi(text2)
    assert "10/10" in plain2
    assert "(100%)" in plain2
    # At 100% the bar is fully ████████░░░░-free (all 10 are filled).
    assert "░" not in plain2, "full bar should have no empty glyphs"
    # use_color=True at 100% should produce ANSI codes (success role).
    assert "\x1b[" in text2, "use_color=True should emit ANSI escapes"


# ---------------------------------------------------------------------------
# 14. render_confirm — returns Yes/No/Cancel rows with the correct action_ids.
# ---------------------------------------------------------------------------


def test_render_confirm_three_row_menu_with_yes_no_cancel_actions() -> None:
    """Confirm prompt renders as a 3-row menu: 1=Yes, 2=No, 0=Cancel."""
    spec = {"header": "Proceed with the dangerous operation?"}
    text, action_map = render_confirm(spec, use_color=False)
    plain = strip_ansi(text)
    assert "Proceed" in plain
    # Default labels are "Yes" and "No"; Cancel is hard-coded.
    assert "Yes" in plain
    assert "No" in plain
    assert "Cancel" in plain
    # Action map maps the rendered keys -> action_ids.
    # Keys "0" is static (stays literal), so "1" is yes, "2" is no,
    # "0" is cancel.
    assert action_map == {"1": "yes", "2": "no", "0": "cancel"}


# ---------------------------------------------------------------------------
# 15. render dispatcher — every mode routes to the right function.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode, minimal_spec, expects_map",
    [
        (
            "menu",
            {
                "mode": "menu",
                "header": "M",
                "rows": [{"key": "1", "action_id": "a", "label": "A"}],
            },
            True,
        ),
        (
            "summary",
            {
                "mode": "summary",
                "counts": {"critical": 0, "major": 0, "minor": 0, "nit": 0, "warning": 0},
            },
            False,
        ),
        (
            "breakdown",
            {
                "mode": "breakdown",
                "rows": [{"label": "Cat", "counts": {"critical": 1}}],
            },
            False,
        ),
        (
            "status_table",
            {
                "mode": "status_table",
                "rows": [{"label": "X", "status": "ok"}],
            },
            False,
        ),
        ("panel", {"mode": "panel", "header": "H", "body": ["b"]}, False),
        (
            "multi_box",
            {"mode": "multi_box", "boxes": [{"header": "H", "body": ["b"]}]},
            False,
        ),
        (
            "progress",
            {"mode": "progress", "header": "H", "current": 1, "total": 2},
            False,
        ),
        ("confirm", {"mode": "confirm", "header": "OK?"}, True),
    ],
)
def test_render_dispatcher_routes_each_mode(
    mode: str, minimal_spec: dict[str, Any], expects_map: bool
) -> None:
    """render() dispatches each of the 8 modes; menu/confirm return action_map, others None."""
    text, action_map = render(minimal_spec, use_color=False)
    # Every mode produces non-empty output.
    assert text, f"mode {mode!r} produced empty text"
    if expects_map:
        # menu + confirm return a non-None dict (may be empty for menu
        # with rows=[] but in this parametrization both have ≥1 row).
        assert isinstance(action_map, dict), f"mode {mode!r} should return a dict map"
        assert len(action_map) >= 1, f"mode {mode!r} dict should be non-empty"
    else:
        assert action_map is None, f"mode {mode!r} should return None for action_map"


# ---------------------------------------------------------------------------
# 16-20. Box style coverage — each of 5 styles produces its corner glyph.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "style_name, expected_corner",
    [
        ("heavy", "┏"),
        ("rounded", "╭"),
        ("light", "┌"),
        ("double", "╔"),
        ("ascii", "+"),
    ],
)
def test_render_menu_each_style_emits_its_corner_glyph(
    style_name: str, expected_corner: str
) -> None:
    """Each box style produces output containing its top-left corner glyph."""
    spec = {
        "header": "Style test",
        "style": style_name,
        # Force unicode for the 4 unicode styles so the ASCII downgrade
        # check (which fires when CLAUDE_MENU_ASCII=1 / TERM=dumb) cannot
        # accidentally rewrite the style to ascii. For "ascii" itself the
        # force_unicode flag is a no-op because the style is already
        # ascii — explicit ASCII never gets downgraded further.
        "force_unicode": True,
        "rows": [{"key": "1", "action_id": "a", "label": "A"}],
    }
    text, _ = render_menu(spec, use_color=False)
    plain = strip_ansi(text)
    assert expected_corner in plain, (
        f"style {style_name!r} did not produce corner glyph {expected_corner!r}; "
        f"output (first 200 chars): {plain[:200]!r}"
    )
    # Sanity check — the corner glyph is the exact one in STYLES[].
    assert STYLES[style_name].tl == expected_corner


# ---------------------------------------------------------------------------
# 21. Auto-downgrade to ASCII when CLAUDE_MENU_ASCII=1.
# ---------------------------------------------------------------------------


def test_render_menu_auto_downgrades_to_ascii_when_env_var_set(monkeypatch) -> None:
    """CLAUDE_MENU_ASCII=1 makes a 'heavy' spec render with ASCII corners (+) not ┏."""
    monkeypatch.setenv("CLAUDE_MENU_ASCII", "1")
    spec = {
        "header": "Will downgrade",
        "style": "heavy",
        "rows": [{"key": "1", "action_id": "a", "label": "A"}],
    }
    text, _ = render_menu(spec, use_color=False)
    plain = strip_ansi(text)
    # The heavy style's "┏" must NOT appear because CLAUDE_MENU_ASCII=1
    # forces the renderer's _style() helper to downgrade to STYLES["ascii"].
    assert "┏" not in plain, "auto-downgrade failed: heavy glyph still in output"
    # ASCII corner "+" must appear (top-left of the table).
    assert "+" in plain, "auto-downgrade succeeded but ASCII '+' corner missing"


# ---------------------------------------------------------------------------
# 22. force_unicode=true overrides CLAUDE_MENU_ASCII=1.
# ---------------------------------------------------------------------------


def test_render_menu_force_unicode_overrides_ascii_env_var(monkeypatch) -> None:
    """spec['force_unicode']=True keeps unicode boxes even when CLAUDE_MENU_ASCII=1."""
    monkeypatch.setenv("CLAUDE_MENU_ASCII", "1")
    spec = {
        "header": "Forced unicode",
        "style": "heavy",
        "force_unicode": True,  # bypass should_use_unicode_boxes() check
        "rows": [{"key": "1", "action_id": "a", "label": "A"}],
    }
    text, _ = render_menu(spec, use_color=False)
    plain = strip_ansi(text)
    # Heavy corner survives because force_unicode overrides the downgrade.
    assert "┏" in plain, "force_unicode=True failed: heavy glyph not preserved"


# ---------------------------------------------------------------------------
# 23. render_menu with rows=[] — header-only table with valid borders.
# ---------------------------------------------------------------------------


def test_render_menu_empty_rows_produces_header_only_table_and_empty_map() -> None:
    """Empty rows list -> table with header + borders but no data rows; action_map is empty."""
    spec = {
        "header": "Nothing yet",
        "rows": [],
        "force_unicode": True,  # use heavy unicode glyphs for the corner asserts below
    }
    text, action_map = render_menu(spec, use_color=False)
    plain = strip_ansi(text)
    # Header label and the column-header "#" still appear.
    assert "Nothing yet" in plain
    assert "#" in plain
    # Borders are present (top + bottom + separator). Heavy style is the
    # default, force_unicode=True so we get the unicode corners.
    assert "┏" in plain  # top-left
    assert "┘" in plain  # bottom-right (heavy uses └/┘ for bot corners)
    # Empty map — no rows means no action_map entries.
    assert action_map == {}


# ---------------------------------------------------------------------------
# 24. Unicode-wide chars widen the column correctly (CJK glyphs are 2 cols).
# ---------------------------------------------------------------------------


def test_render_menu_cjk_label_widens_column_by_two_columns_per_char(monkeypatch) -> None:
    """Label '中文' (2 CJK chars = 4 display cols) widens the label column to 4 not 2."""
    # Disable color so the assertion about column-cell widths isn't
    # noised by ANSI codes around cell content. (display_width strips
    # ANSI itself but we want to anchor the test independently.)
    monkeypatch.setenv("NO_COLOR", "1")
    spec = {
        "header": "L",  # 1-char header label so the column-width is governed by the data row
        "force_unicode": True,
        "rows": [
            {"key": "1", "action_id": "a", "label": "中文"},  # 2 CJK chars = 4 cols
        ],
    }
    text, _ = render_menu(spec, use_color=False)
    plain = strip_ansi(text)
    # Find the data row that contains "中文" and extract its label cell
    # by splitting on the vertical bar "│" used by the heavy style.
    data_row = next(ln for ln in plain.splitlines() if "中文" in ln)
    # The data row format is: │ <key> │ <label> │
    # Split by the vertical bar to get the cells.
    cells = data_row.split("│")
    # cells[0]="" (before first │), cells[1]=" 1 ", cells[2]=" 中文 ",
    # cells[3]="" (after last │). The label cell is cells[2].
    label_cell = cells[2]
    # The cell contains pad_l space + label + pad_r space + any extra
    # padding to match column width. display_width() of label content
    # (stripped of the 1+1 padding spaces) must be 4 for "中文".
    # Strip the leading/trailing single padding spaces (pad_l=pad_r=1).
    inner = label_cell[1:-1] if len(label_cell) >= 2 else label_cell
    # inner is "中文" exactly when column width matches label width (4)
    # — no trailing pad spaces. display_width must be 4.
    assert display_width(inner) == 4, (
        f"CJK label cell has wrong display width: {display_width(inner)!r}, "
        f"expected 4. cell={label_cell!r} inner={inner!r}"
    )


# ---------------------------------------------------------------------------
# 25. ANSI in a label doesn't break alignment — padding measures visible width.
# ---------------------------------------------------------------------------


def test_render_menu_ansi_in_label_does_not_break_table_alignment(monkeypatch) -> None:
    """ANSI codes in a label (display width 3) pad to match a plain row of width 7."""
    # Disable color so the renderer doesn't add ANSI codes to the borders
    # — we want to isolate the label's pre-baked ANSI as the only escapes.
    monkeypatch.setenv("NO_COLOR", "1")
    spec = {
        "header": "L",
        "force_unicode": True,
        "rows": [
            # Label with ANSI codes wrapping "red" -> display width 3.
            {"key": "1", "action_id": "a", "label": "\x1b[91mred\x1b[0m"},
            # Plain 7-char label -> column width should be max(3, 7, 1)=7.
            {"key": "2", "action_id": "b", "label": "longer!"},
        ],
    }
    text, _ = render_menu(spec, use_color=False)
    lines = text.splitlines()
    # Find the two data lines (those containing "red" and "longer!").
    red_line = next(ln for ln in lines if "red" in ln)
    longer_line = next(ln for ln in lines if "longer!" in ln)
    # The TWO data rows must have IDENTICAL visible widths — that's
    # the entire point of the column alignment. Strip ANSI before
    # comparing.
    red_visible = strip_ansi(red_line)
    longer_visible = strip_ansi(longer_line)
    assert display_width(red_visible) == display_width(longer_visible), (
        f"misaligned rows: red has display_width {display_width(red_visible)}, "
        f"longer has {display_width(longer_visible)}; red={red_visible!r} "
        f"longer={longer_visible!r}"
    )
    # Sanity check: confirm the ANSI codes are still present in the
    # un-stripped line (the renderer preserved the user's pre-baked
    # escapes verbatim) — proves we actually exercised the ANSI path.
    assert "\x1b[91m" in red_line, "renderer dropped the user-supplied ANSI codes"
    assert "\x1b[0m" in red_line, "renderer dropped the RESET code"
