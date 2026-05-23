"""Unit tests for ``scripts/menu_emit.py``.

Covers the Stop/StopFailure hook entry point + every helper it routes
through:

  - ``_read_hook_payload`` — stdin JSON parse with empty/invalid input
    branches.
  - ``_extract_title`` — first-non-border-line extraction + the all-
    borders fallback.
  - ``_truncate_big_menu`` — pass-through (≤ budget), bisect-with-rows-
    truncated indicator (> budget), and the <6-lines fallback to
    ``line_safe_truncate``.
  - ``_compose_payload`` — empty input, the SMALL concat path, the
    OVERFLOW title-stub path, ANSI strip when color is off, ANSI
    preservation when color is on, and the hard 9500-char cap on the
    final composed output.
  - ``main`` / ``_handle_emit_event`` — SubagentStop is a no-op (file
    remains in the queue), Stop on an empty queue cleans up, Stop with
    queued menus emits the JSON ``{"systemMessage": ...}`` to stdout
    in timestamp order and deletes consumed files. StopFailure routes
    through the same handler as Stop.

The conftest autouse fixture sets ``TMPDIR=tmp_path`` and
``CLAUDE_SESSION_ID=test-session`` per test so queue paths are fully
isolated. We bust the stdlib ``tempfile.tempdir`` cache between tests
(same trick as ``test_menu_queue.py``) so ``menu_queue.queue_root()``
re-reads TMPDIR.

External dependencies that we deliberately exercise for real:
  - real filesystem under ``tmp_path``
  - real ``fcntl.flock`` via ``menu_queue.session_lock``
  - real stdout via pytest's ``capsys`` fixture
  - real ANSI strip via ``menu_ansi.strip_ansi``

Only stdin is monkeypatched — that's how the hook reads its payload.

Coverage target: >=95% of ``menu_emit.py``. Verified by inspection of
the source — every branch in every helper has at least one test.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
from pathlib import Path

import menu_emit
import menu_queue
import pytest

# ---------------------------------------------------------------------------
# Per-test isolation of tempfile.gettempdir()
# ---------------------------------------------------------------------------
#
# stdlib ``tempfile`` caches the result of ``gettempdir()`` in
# ``tempfile.tempdir`` on first call. Without busting that cache between
# tests, every test would resolve back to whatever tempdir was first
# looked up and per-test ``tmp_path`` isolation would silently break.
# Same pattern as ``test_menu_queue.py``.


@pytest.fixture(autouse=True)
def _reset_tempfile_cache():
    """Force tempfile.gettempdir() to re-read TMPDIR each test."""
    tempfile.tempdir = None
    yield
    tempfile.tempdir = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_menu_file(content: str, *, ts_ns: int | None = None, slug: str = "menu") -> Path:
    """Write a fake menu file directly into the session queue dir.

    Bypasses ``menu_write`` so the tests focus solely on emit. Returns
    the path of the freshly created menu file. If ``ts_ns`` is given,
    the filename's leading 20-digit timestamp is set explicitly so
    tests can pin ordering without relying on ``time.sleep``.
    """
    sd = menu_queue.session_dir()
    if ts_ns is None:
        ts_ns = time.time_ns()
    name = f"{ts_ns:020d}-testplugin-{slug}{menu_queue.MENU_SUFFIX}"
    path = sd / name
    path.write_text(content, encoding="utf-8")
    return path


def _invoke_main(monkeypatch, event: str, *, session_id: str = "test-session") -> None:
    """Run ``menu_emit.main`` with a mock hook payload on stdin.

    Drives the hook end-to-end and asserts a clean return code. The
    helper does NOT return stdout — callers read it directly via the
    pytest ``capsys`` fixture after the call. (The previous ``-> str``
    annotation lied about a return value that never existed; Pyright
    correctly flagged the missing return path.)
    """
    payload = json.dumps({"hook_event_name": event, "session_id": session_id})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = menu_emit.main(["menu_emit.py"])
    assert rc == 0, f"main returned {rc}, expected 0"


# ---------------------------------------------------------------------------
# 1. SubagentStop → no-op (file stays in queue)
# ---------------------------------------------------------------------------


def test_subagentstop_is_noop_menu_remains_in_queue(monkeypatch, tmp_path, capsys):
    """SubagentStop event: main returns 0, nothing on stdout, queue file stays."""
    # Turn debug logging on with a redirected DEBUG_LOG path so we ALSO
    # exercise the _log_debug write branch (lines 63-68) without polluting
    # /tmp. Test stays focused on emit, but coverage gets the freebie.
    debug_log = tmp_path / "debug.log"
    monkeypatch.setattr(menu_emit, "DEBUG_LOG", debug_log)
    monkeypatch.setenv("CLAUDE_MENU_DEBUG", "1")

    menu_path = _write_menu_file("---\nMenu body\n---\n")
    assert menu_path.exists()

    _invoke_main(monkeypatch, "SubagentStop")

    out = capsys.readouterr().out
    assert out == "", f"Expected empty stdout for SubagentStop, got: {out!r}"
    # The hook MUST leave the menu file in place — emit happens only at
    # main-session Stop, not subagent Stop. This is the central
    # SubagentStop-is-a-no-op invariant in menu_emit.py.
    assert menu_path.exists()
    # Debug log was written (every code path that calls _log_debug touches
    # the OSError-guarded write branch).
    assert debug_log.exists()
    assert "hook fired" in debug_log.read_text()


# ---------------------------------------------------------------------------
# 2. Stop with empty queue → cleanup, no stdout
# ---------------------------------------------------------------------------


def test_stop_with_empty_queue_emits_nothing_and_cleans_up(monkeypatch, capsys):
    """Stop event with empty queue: main returns 0, nothing on stdout, session dir removed."""
    # session_dir() creates the dir on demand — make sure it exists before
    # the call so we can assert that cleanup actually removed it after.
    sd = menu_queue.session_dir()
    assert sd.exists()

    _invoke_main(monkeypatch, "Stop")

    out = capsys.readouterr().out
    assert out == ""
    # cleanup_empty_session_dir wipes the dir when no menus/actions are present.
    assert not sd.exists()

    # --- Also exercise the read-failure branches (138-139, 142) and the
    # empty-payload cleanup path (215-216): write a real menu file but
    # monkeypatch Path.read_text to raise OSError so contents stays empty,
    # _compose_payload returns ("", []), and _handle_emit_event takes the
    # empty-payload cleanup branch.
    unreadable = _write_menu_file("body content here\n", slug="unreadable")
    real_read_text = Path.read_text

    def boom(self, *args, **kwargs):
        if self == unreadable:
            raise OSError("simulated read failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    _invoke_main(monkeypatch, "Stop")
    out2 = capsys.readouterr().out
    # Empty payload → nothing emitted → no JSON on stdout.
    assert out2 == ""
    # cleanup ran (no menu files were actually consumed but the read-failure
    # leaves the file in place; the dir survives because the unreadable
    # file is still there).
    assert unreadable.exists()


# ---------------------------------------------------------------------------
# 3. Stop with one small menu → emit + queue cleanup
# ---------------------------------------------------------------------------


def test_systemMessage_always_starts_with_newline(monkeypatch, capsys):
    """Regression pin: emitted systemMessage MUST start with '\\n'.

    Claude Code's TUI prepends a 'Stop says:' banner to the systemMessage,
    rendering it on the same row. Without a leading newline, the box's
    first row sits next to the banner and every subsequent row is shifted,
    breaking the visual alignment. The leading newline forces the box to
    start on its own clean row. Empirically caught in v0.1.1 production
    use and fixed in v0.1.2; this test pins the behaviour so it doesn't
    regress.
    """
    _write_menu_file("some menu content\n")
    _invoke_main(monkeypatch, "Stop")
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["systemMessage"].startswith("\n"), (
        "systemMessage must start with '\\n' so the menu box renders on "
        "its own line under Claude Code's 'Stop says:' banner. Without "
        "this prefix the first row of the box is shifted right and "
        "every subsequent row breaks alignment."
    )


@pytest.mark.parametrize("event", ["Stop", "StopFailure"])
def test_stop_event_with_one_small_menu_emits_intact(monkeypatch, capsys, event):
    """Stop / StopFailure with one ~300-char menu: JSON emitted with full body, queue empty after."""
    body = "Menu Title\n" + ("row contents abc def\n" * 12)
    assert 200 < len(body) < 400, f"Body length {len(body)} not in ~300 range"
    menu_path = _write_menu_file(body)

    _invoke_main(monkeypatch, event)

    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert "systemMessage" in parsed
    # Whole menu body must be present verbatim, prefixed with one newline
    # so the box starts on its own line under Claude Code's "Stop says:"
    # banner (without the leading \n, the first row sits on the same row
    # as the banner and shifts every subsequent line).
    assert parsed["systemMessage"] == "\n" + body
    # File was consumed.
    assert not menu_path.exists()


# ---------------------------------------------------------------------------
# 4. Stop with multiple menus → emitted in timestamp order
# ---------------------------------------------------------------------------


def test_stop_with_multiple_menus_emits_in_timestamp_order(monkeypatch, capsys):
    """Stop with 3 menus written in order: systemMessage contains them in write order."""
    # Use explicit ns timestamps to pin ordering — no sleep() flakiness.
    base = time.time_ns()
    p1 = _write_menu_file("FIRST menu body\n", ts_ns=base + 1, slug="m1")
    p2 = _write_menu_file("SECOND menu body\n", ts_ns=base + 2, slug="m2")
    p3 = _write_menu_file("THIRD menu body\n", ts_ns=base + 3, slug="m3")

    _invoke_main(monkeypatch, "Stop")

    out = capsys.readouterr().out
    msg = json.loads(out)["systemMessage"]
    # All three present.
    assert "FIRST menu body" in msg
    assert "SECOND menu body" in msg
    assert "THIRD menu body" in msg
    # And in the timestamp-ascending order they were written.
    idx_first = msg.find("FIRST")
    idx_second = msg.find("SECOND")
    idx_third = msg.find("THIRD")
    assert idx_first < idx_second < idx_third
    # All files were consumed.
    for p in (p1, p2, p3):
        assert not p.exists()


# ---------------------------------------------------------------------------
# 5. StopFailure parity with Stop (explicit, in addition to the parametrize
#    on test 3)
# ---------------------------------------------------------------------------


def test_stopfailure_behaves_same_as_stop_for_multi_menu(monkeypatch, capsys):
    """StopFailure event: same handler as Stop — multi-menu emit + cleanup."""
    base = time.time_ns()
    _write_menu_file("FAILURE_A\n", ts_ns=base + 1, slug="a")
    _write_menu_file("FAILURE_B\n", ts_ns=base + 2, slug="b")

    _invoke_main(monkeypatch, "StopFailure")

    out = capsys.readouterr().out
    msg = json.loads(out)["systemMessage"]
    assert "FAILURE_A" in msg
    assert "FAILURE_B" in msg
    assert msg.find("FAILURE_A") < msg.find("FAILURE_B")


# ---------------------------------------------------------------------------
# 6. _extract_title — happy path
# ---------------------------------------------------------------------------


def test_extract_title_returns_first_non_border_line():
    """_extract_title returns the first non-border line (skipping borders + blanks)."""
    # The _extract_title heuristic skips lines starting with corner /
    # wall / horizontal-bar box-drawing chars, plus blank lines. Build a
    # menu where the FIRST non-pure-border, non-wall-prefixed line is
    # an actual title — that's what the helper must return.
    text = (
        "┏━━━━━━━━━━━━━━━━━━━━┓\n"
        "MyMenu Title Line\n"
        "┃ Validation Results ┃\n"
        "└──────────────────────┘\n"
    )
    assert menu_emit._extract_title(text) == "MyMenu Title Line"


# ---------------------------------------------------------------------------
# 7. _extract_title — only borders → fallback
# ---------------------------------------------------------------------------


def test_extract_title_returns_fallback_when_only_borders():
    """_extract_title returns '(menu)' when every line is a border or blank."""
    text = (
        "┏━━━━┓\n"
        "┃    ┃\n"  # leading wall — skipped by the heuristic
        "\n"  # blank — skipped
        "└────┘\n"
    )
    assert menu_emit._extract_title(text) == "(menu)"
    # Empty input also falls through to the fallback.
    assert menu_emit._extract_title("") == "(menu)"


# ---------------------------------------------------------------------------
# 8. _truncate_big_menu — pass-through when ≤ budget
# ---------------------------------------------------------------------------


def test_truncate_big_menu_returns_input_unchanged_when_within_budget():
    """_truncate_big_menu returns the input unchanged when len(text) ≤ budget."""
    text = "small menu body\nrow1\nrow2\n"
    assert menu_emit._truncate_big_menu(text, budget=1000) == text
    # Exact boundary — equal length is also "within budget" (≤).
    assert menu_emit._truncate_big_menu(text, budget=len(text)) == text


# ---------------------------------------------------------------------------
# 9. _truncate_big_menu — bisect with rows-truncated indicator
# ---------------------------------------------------------------------------


def test_truncate_big_menu_emits_rows_truncated_indicator_when_over_budget():
    """_truncate_big_menu keeps header + footer + 'rows truncated' indicator when too big."""
    # Build a menu with ≥6 lines so the bisect branch (not the fallback)
    # is exercised. Pad each body row so the total is comfortably over
    # the 200-char budget but each individual line is small.
    header = "TITLE\n┏━━━━━━━━┓\n┃ HEADER ┃\n┡━━━━━━━━┩\n"
    body = "".join(f"│ row {i:03d} padding text...... │\n" for i in range(40))
    footer = "└────────┘\nfooter text line\n"
    text = header + body + footer
    assert len(text) > 500, "input must exceed budget for this test"

    out = menu_emit._truncate_big_menu(text, budget=300)

    # The output must be within budget...
    assert len(out) <= 300
    # ...must preserve the title (it's in the first 4 header lines)...
    assert "TITLE" in out
    # ...must preserve the footer...
    assert "footer text line" in out
    # ...and must include the 'rows truncated' indicator the bisect path injects.
    assert "rows truncated" in out

    # --- Also exercise the body-empty branch (line 108): a menu with exactly
    # 6 lines (no trailing \n) slices to body=lines[4:-2]=[] → the
    # `if not body` fallback to line_safe_truncate fires. We need
    # split('\n') to produce exactly 6 elements, NOT 7 (no trailing newline).
    six_line_no_body = (
        "TITLE long content here that pushes over the tiny budget aaaaaaaa\n"
        "border1\n"
        "header\n"
        "sep\n"
        "border2\n"
        "footer"  # no trailing newline → split('\n') yields exactly 6 items
    )
    assert len(six_line_no_body.split("\n")) == 6
    # Confirm body slice is empty under the slice indices menu_emit uses.
    lines_split = six_line_no_body.split("\n")
    assert lines_split[4:-2] == []  # header_count=4, footer_count=2
    out2 = menu_emit._truncate_big_menu(six_line_no_body, budget=40)
    # line_safe_truncate's indicator is "[truncated]", NOT "rows truncated".
    assert "[truncated]" in out2
    assert len(out2) <= 40


# ---------------------------------------------------------------------------
# 9b. _truncate_big_menu — M3: the truncated-row count is EXACT (no double-count).
# ---------------------------------------------------------------------------


def test_truncate_big_menu_indicator_count_equals_rows_dropped():
    """M3: the '[N rows truncated]' integer equals exactly the body rows removed.

    Pre-fix the count double-counted (``dropped + (len(body)-len(kept_body)) + 1``)
    where ``dropped`` already equals ``len(body)-len(kept_body)`` — so the reported
    value was roughly 2x+1 the truth (a 7-row drop printed "15 rows truncated").

    The function partitions ``text.split("\\n")`` into header (first 4 lines),
    footer (last 2 lines), and a middle "body". This test reconstructs that
    same partition so the assertion is exact regardless of the menu shape:
    reported count == (body lines) - (body lines surviving in the output).
    """
    import re

    # No trailing newline — this matches a real rendered menu (footer is the
    # last line). A trailing newline would inject an empty split element that
    # shifts the footer/border slicing; production menus never have one.
    header_lines = ["TITLE", "┏━━━━━━━━┓", "┃ HEADER ┃", "┡━━━━━━━━┩"]
    body_lines = [f"│ row {i:03d} padding text...... │" for i in range(40)]
    footer_lines = ["└────────┘", "footer text line"]
    text = "\n".join(header_lines + body_lines + footer_lines)

    out = menu_emit._truncate_big_menu(text, budget=300)
    assert len(out) <= 300

    m = re.search(r"…\[(\d+) rows truncated\]", out)
    assert m is not None, f"no truncation indicator found in:\n{out}"
    reported = int(m.group(1))

    # Reconstruct the function's body partition (header_count=4, footer_count=2)
    # and count how many of those body lines survived in the output.
    out_lines = set(out.split("\n"))
    body_surviving = sum(1 for line in body_lines if line in out_lines)
    dropped = len(body_lines) - body_surviving

    assert reported == dropped, (
        f"reported={reported} but actually dropped {dropped} "
        f"({len(body_lines)} body rows, {body_surviving} surviving)"
    )
    # Regression guard: the OLD buggy formula would report ~2*dropped (+/-1).
    # The corrected count must be a single count of the dropped rows.
    assert 0 < reported <= len(body_lines)
    assert reported < 2 * dropped or dropped <= 1, (
        f"reported={reported} looks like the old double-counted value for {dropped} drops"
    )


# ---------------------------------------------------------------------------
# 10. _truncate_big_menu — <6 lines → fallback to line_safe_truncate
# ---------------------------------------------------------------------------


def test_truncate_big_menu_falls_back_to_line_safe_truncate_for_short_input():
    """_truncate_big_menu uses line_safe_truncate when input has fewer than 6 lines."""
    # 3 lines, each very long so the total massively overshoots the budget.
    text = "line1 " + ("a" * 500) + "\n" + "line2 " + ("b" * 500) + "\n" + "line3\n"
    assert text.count("\n") < 6, "test prerequisite: input must have <6 lines"
    assert len(text) > 200

    out = menu_emit._truncate_big_menu(text, budget=200)

    # Fallback path: line_safe_truncate appends "…[truncated]" as the indicator,
    # NOT "rows truncated".
    assert "[truncated]" in out
    assert "rows truncated" not in out
    # Output stays within the budget.
    assert len(out) <= 200


# ---------------------------------------------------------------------------
# 11. _compose_payload — empty input → ("", [])
# ---------------------------------------------------------------------------


def test_compose_payload_empty_list_returns_empty_tuple():
    """_compose_payload([]) returns ('', []) — nothing to emit."""
    text, to_delete = menu_emit._compose_payload([])
    assert text == ""
    assert to_delete == []


# ---------------------------------------------------------------------------
# 12. _compose_payload — SMALL path (total ≤ budget, ≤10 menus)
# ---------------------------------------------------------------------------


def test_compose_payload_small_path_concatenates_all_menus():
    """_compose_payload SMALL path: total ≤ TOTAL_BUDGET, ≤10 menus → concat all."""
    base = time.time_ns()
    p1 = _write_menu_file("alpha-body\n", ts_ns=base + 1, slug="alpha")
    p2 = _write_menu_file("beta-body\n", ts_ns=base + 2, slug="beta")
    p3 = _write_menu_file("gamma-body\n", ts_ns=base + 3, slug="gamma")

    text, to_delete = menu_emit._compose_payload([p1, p2, p3])

    # All three bodies present.
    assert "alpha-body" in text
    assert "beta-body" in text
    assert "gamma-body" in text
    # In the order passed in (timestamp ascending).
    assert text.find("alpha") < text.find("beta") < text.find("gamma")
    # All three files returned for deletion.
    assert set(to_delete) == {p1, p2, p3}
    # Joiner is "\n\n" — between alpha and beta there must be a blank line.
    assert "alpha-body\n\n\nbeta-body" in text or "alpha-body\n\nbeta-body" in text


# ---------------------------------------------------------------------------
# 13. _compose_payload — OVERFLOW path (>10 menus → stubs)
# ---------------------------------------------------------------------------


def test_compose_payload_overflow_path_renders_older_as_title_stubs():
    """_compose_payload OVERFLOW: 12 menus → older become title stubs, newest 2 stay full."""
    base = time.time_ns()
    paths = []
    # First 10 menus: small (~800 chars) — they become title stubs in the
    # 'old' bucket.
    for i in range(10):
        body = f"Title-{i:02d}\n" + ("body row data padding text...\n" * 25)
        assert 700 < len(body) < 900, f"menu {i} length {len(body)} not in 700-900 range"
        p = _write_menu_file(body, ts_ns=base + i, slug=f"m{i:02d}")
        paths.append(p)
    # Last 2 menus (the 'new' bucket that stays full-rendered): make ONE
    # of them BIG (>SMALL_MENU_THRESHOLD=1000 AND >per_new) so the
    # _truncate_big_menu(t, per_new) branch on line 175 is exercised.
    # The other stays small so the line 173 pass-through branch is also
    # exercised in the same call.
    small_new_body = "Title-10\n" + ("small new row\n" * 20)  # ~280 chars
    assert len(small_new_body) <= menu_emit.SMALL_MENU_THRESHOLD
    paths.append(_write_menu_file(small_new_body, ts_ns=base + 10, slug="m10"))
    # Big-new body must:
    #  - have ≥6 lines (so _truncate_big_menu uses the bisect branch),
    #  - exceed SMALL_MENU_THRESHOLD (1000),
    #  - exceed per_new (which is roughly (TOTAL_BUDGET - stub_block)/2).
    # A 4000-char menu with ~50 body rows satisfies all three.
    header_lines = "Title-11\n┏━━━━━━━━┓\n┃ HEAD   ┃\n┡━━━━━━━━┩\n"
    big_body = "│ row of data with padding text content x │\n" * 200
    footer_lines = "└────────┘\nbottom-footer\n"
    big_new_body = header_lines + big_body + footer_lines
    assert len(big_new_body) > menu_emit.SMALL_MENU_THRESHOLD
    # Must exceed per_new (~(9500-350)/2 ≈ 4575) so the bisect branch fires.
    assert len(big_new_body) > 5000
    paths.append(_write_menu_file(big_new_body, ts_ns=base + 11, slug="m11"))

    text, to_delete = menu_emit._compose_payload(paths)

    # Stub block header from menu_emit.py.
    assert "Older menus (truncated):" in text
    # Newest two titles must still appear (the small one verbatim, the
    # big one through the _truncate_big_menu shaper).
    assert "Title-10" in text
    assert "Title-11" in text
    # Older menus should appear as bullet-prefixed truncated entries.
    assert "• Title-00  (truncated)" in text
    assert "• Title-09  (truncated)" in text
    # The big-new menu went through _truncate_big_menu — the indicator must
    # appear since its full size (>4000) exceeds per_new (~4000ish - stub).
    assert "rows truncated" in text
    # All files are reported for deletion (we always emit everything).
    assert set(to_delete) == set(paths)
    # Final output stays under the hard cap.
    assert len(text) <= menu_emit.TOTAL_BUDGET


# ---------------------------------------------------------------------------
# 14. _compose_payload — ANSI strip when color disabled
# ---------------------------------------------------------------------------


def test_compose_payload_strips_ansi_when_color_disabled(monkeypatch):
    """_compose_payload strips ANSI escapes when CLAUDE_MENU_COLOR=0 forces color off."""
    monkeypatch.setenv("CLAUDE_MENU_COLOR", "0")
    # Build a menu containing real ANSI sequences. Use a recognisable
    # marker string ("RED-MARKER") between the escape codes so we can
    # assert the visible text survives stripping.
    body = "Header line\n\x1b[91mRED-MARKER\x1b[0m\nFooter\n"
    p = _write_menu_file(body)

    text, _ = menu_emit._compose_payload([p])

    # The visible marker survives.
    assert "RED-MARKER" in text
    # All ANSI escape introducers must be gone.
    assert "\x1b" not in text
    assert "\x1b[" not in text


# ---------------------------------------------------------------------------
# 15. _compose_payload — ANSI preserved when color enabled
# ---------------------------------------------------------------------------


def test_compose_payload_preserves_ansi_when_color_enabled(monkeypatch):
    """_compose_payload keeps ANSI escapes when color is enabled (TERM=xterm-256color)."""
    # The autouse fixture already sets TERM=xterm-256color, clears NO_COLOR,
    # clears CLAUDE_MENU_COLOR. Be explicit for the reader.
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLAUDE_MENU_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    body = "Header\n\x1b[92mGREEN-MARKER\x1b[0m\nFooter\n"
    p = _write_menu_file(body)

    text, _ = menu_emit._compose_payload([p])

    # ANSI escapes survive verbatim.
    assert "\x1b[" in text
    assert "\x1b[92m" in text
    assert "GREEN-MARKER" in text


# ---------------------------------------------------------------------------
# 16. Total output never exceeds TOTAL_BUDGET
# ---------------------------------------------------------------------------


def test_total_output_never_exceeds_total_budget_under_extreme_overflow():
    """100 menus x 200 chars: composed output stays within TOTAL_BUDGET (9500)."""
    base = time.time_ns()
    paths = []
    for i in range(100):
        body = f"M{i:03d}_title\n" + ("padding line content here\n" * 6)
        # ~200 chars each. 100 x 200 = 20_000 raw, well over the 9500 cap.
        assert 150 < len(body) < 300
        paths.append(_write_menu_file(body, ts_ns=base + i, slug=f"m{i:03d}"))

    text, _ = menu_emit._compose_payload(paths)

    # Hard cap from menu_emit.TOTAL_BUDGET.
    assert len(text) <= menu_emit.TOTAL_BUDGET, (
        f"Output length {len(text)} exceeds TOTAL_BUDGET {menu_emit.TOTAL_BUDGET}"
    )


# ---------------------------------------------------------------------------
# 17. _read_hook_payload — empty stdin → {}
# ---------------------------------------------------------------------------


def test_read_hook_payload_returns_empty_dict_for_empty_stdin(monkeypatch):
    """_read_hook_payload returns {} when stdin is empty / whitespace-only."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert menu_emit._read_hook_payload() == {}
    # Whitespace-only is also treated as empty.
    monkeypatch.setattr(sys, "stdin", io.StringIO("   \n\t  "))
    assert menu_emit._read_hook_payload() == {}


# ---------------------------------------------------------------------------
# 18. _read_hook_payload — invalid JSON → {}
# ---------------------------------------------------------------------------


def test_read_hook_payload_returns_empty_dict_for_invalid_json(monkeypatch):
    """_read_hook_payload returns {} when stdin contains malformed JSON."""
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not valid json at all"))
    assert menu_emit._read_hook_payload() == {}
    # Another malformed shape — bare token that isn't a JSON value.
    monkeypatch.setattr(sys, "stdin", io.StringIO("hook_event_name=Stop"))
    assert menu_emit._read_hook_payload() == {}

    # --- Also exercise the OSError branch on sys.stdin.read() (lines 191-192).
    # Synthesise a stdin replacement whose read() raises OSError.
    class BrokenStdin:
        def read(self):
            raise OSError("simulated stdin read failure")

    monkeypatch.setattr(sys, "stdin", BrokenStdin())
    assert menu_emit._read_hook_payload() == {}


# ---------------------------------------------------------------------------
# 19. _truncate_big_menu — "[N rows truncated]" count is ACCURATE
# ---------------------------------------------------------------------------


def test_truncate_big_menu_rows_truncated_count_is_accurate():
    """The integer in '[N rows truncated]' equals rows ACTUALLY dropped.

    Pre-PR the indicator used ``dropped + len(body) - len(kept_body) + 1``
    which is ``2*dropped + 1`` — both terms count the same quantity, then
    add 1 for good measure. Result: a menu that dropped 5 body rows
    advertised "[11 rows truncated]". This test pins the corrected count.
    """
    # Build a menu with EXACTLY 30 body rows so we can predict the
    # drop count. Header = 4 lines, footer = 2 lines, body = 30 rows.
    header_lines = "TITLE\n┏━━━━━━━━━━┓\n┃ # | ITEM ┃\n┡━━━━━━━━━━┩\n"
    # Each body row ~30 chars × 30 rows = ~900 chars body.
    body_rows = "".join(f"│ {i:2d} | item-{i:02d}-padding-x │\n" for i in range(30))
    footer_lines = "└──────────┘\nfooter line\n"
    text = header_lines + body_rows + footer_lines
    # Total ~1000 chars. Budget = 400 forces dropping enough body rows
    # to fit header + remaining body + indicator + footer in 400 chars.
    out = menu_emit._truncate_big_menu(text, budget=400)
    assert len(out) <= 400
    assert "rows truncated" in out

    # Extract the claimed count from the indicator and confirm it
    # matches the rows ACTUALLY absent from the output.
    import re

    m = re.search(r"\[(\d+) rows truncated\]", out)
    assert m, f"indicator missing from output: {out!r}"
    claimed_count = int(m.group(1))

    # Count how many original body rows survive in the output.
    surviving_rows = sum(1 for i in range(30) if f"item-{i:02d}" in out)
    actually_dropped = 30 - surviving_rows
    assert claimed_count == actually_dropped, (
        f"indicator claimed [{claimed_count} rows truncated] but {actually_dropped} "
        f"rows were actually dropped (30 total, {surviving_rows} survive in output). "
        f"Pre-PR bug computed 2*dropped+1 — this test pins the fix."
    )
    # And the count must be > 0 — if zero rows were dropped, the
    # indicator must not appear at all.
    assert claimed_count > 0


def test_truncate_big_menu_drops_at_least_one_before_claiming_truncation():
    """The indicator only appears AFTER at least one row was actually dropped.

    Pre-PR, the size check happened BEFORE any pop(), so the first
    iteration could return ``header + ALL_body + "[1 rows truncated]" + footer``
    when that string happened to fit the budget — claiming a drop that
    never occurred. Post-PR, ``kept_body.pop()`` always runs before the
    size check, so the indicator's count is at least 1.
    """
    header_lines = "TITLE\n┏━┓\n┃ ┃\n┡━┩\n"
    body_rows = "".join(f"│ row-{i:03d} │\n" for i in range(20))
    footer_lines = "└─┘\nfooter\n"
    text = header_lines + body_rows + footer_lines

    # Pick a budget JUST barely enough that the first iteration's
    # candidate (with full body + indicator) would have fit if the
    # indicator had said "1 rows truncated" with no actual drop.
    # We deliberately pick a budget slightly less than full input so
    # at least one drop is required.
    budget = len(text) - 50
    out = menu_emit._truncate_big_menu(text, budget=budget)
    assert len(out) <= budget

    import re

    m = re.search(r"\[(\d+) rows truncated\]", out)
    assert m
    claimed = int(m.group(1))
    assert claimed >= 1, "indicator claimed 0-row truncation — must drop AT LEAST one row"


# ---------------------------------------------------------------------------
# 20. _read_truncate_at — sidecar absent / null / int / malformed
# ---------------------------------------------------------------------------


def test_read_truncate_at_returns_none_when_sidecar_absent(tmp_path):
    """No .meta.json sidecar → None (fall back to default heuristic)."""
    menu = tmp_path / "00000000000000000001-cpv-test.menu.md"
    menu.write_text("body\n")
    assert menu_emit._read_truncate_at(menu) is None


def test_read_truncate_at_returns_int_for_positive_value(tmp_path):
    """``{"truncate_at": 5000}`` sidecar → int 5000."""
    menu = tmp_path / "00000000000000000002-cpv-test.menu.md"
    menu.write_text("body\n")
    meta = menu_emit.meta_path_for(menu)
    meta.write_text(json.dumps({"truncate_at": 5000}))
    assert menu_emit._read_truncate_at(menu) == 5000


def test_read_truncate_at_returns_sentinel_for_explicit_null(tmp_path):
    """``{"truncate_at": null}`` sidecar → TRUNCATE_DISABLED sentinel."""
    menu = tmp_path / "00000000000000000003-cpv-test.menu.md"
    menu.write_text("body\n")
    meta = menu_emit.meta_path_for(menu)
    meta.write_text(json.dumps({"truncate_at": None}))
    result = menu_emit._read_truncate_at(menu)
    assert result is menu_emit.TRUNCATE_DISABLED


def test_read_truncate_at_returns_none_for_malformed_sidecar(tmp_path):
    """Garbage JSON / wrong type / negative int → None (defensive fallback)."""
    menu = tmp_path / "00000000000000000004-cpv-test.menu.md"
    menu.write_text("body\n")
    meta = menu_emit.meta_path_for(menu)
    # Garbage JSON.
    meta.write_text("{not valid json")
    assert menu_emit._read_truncate_at(menu) is None
    # Wrong type at top level.
    meta.write_text(json.dumps(["a", "list"]))
    assert menu_emit._read_truncate_at(menu) is None
    # No truncate_at key.
    meta.write_text(json.dumps({"something_else": 42}))
    assert menu_emit._read_truncate_at(menu) is None
    # Negative int.
    meta.write_text(json.dumps({"truncate_at": -1}))
    assert menu_emit._read_truncate_at(menu) is None
    # Zero.
    meta.write_text(json.dumps({"truncate_at": 0}))
    assert menu_emit._read_truncate_at(menu) is None
    # Bool (subclass of int — must be rejected explicitly).
    meta.write_text(json.dumps({"truncate_at": True}))
    assert menu_emit._read_truncate_at(menu) is None


# ---------------------------------------------------------------------------
# 21. _compose_payload — truncate_at: null disables per-menu shaping
# ---------------------------------------------------------------------------


def test_compose_payload_truncate_at_null_skips_per_menu_shaping():
    """With ``truncate_at:null`` sidecar, the menu passes through unshaped.

    Without this PR, ``_compose_payload`` would shape ANY oversized menu
    via ``_truncate_big_menu``. With ``truncate_at:null`` set, the per-
    menu shaping step is bypassed — only the final 9500-char safety net
    can clip the output. This pins the "fail loudly" semantics the user
    asked for.
    """
    # Build 12 menus to force the overflow path. The newest 2 get the
    # full-render treatment. We set ``truncate_at:null`` on the newest
    # one and assert its content survives unshaped.
    base = time.time_ns()
    paths = []
    # First 10: tiny menus that become title stubs.
    for i in range(10):
        body = f"Title-{i:02d}\nbody\n"
        paths.append(_write_menu_file(body, ts_ns=base + i, slug=f"m{i:02d}"))
    # Newest-1: a small menu that fits everywhere.
    paths.append(_write_menu_file("Small\nbody\n", ts_ns=base + 10, slug="m10"))
    # Newest-0: a LARGE menu with truncate_at:null. It would otherwise
    # be passed to _truncate_big_menu since it exceeds per_new.
    header_lines = "BIG-MENU-TITLE\n┏━━━┓\n┃ x ┃\n┡━━━┩\n"
    body_rows = "".join(f"│ DISTINCT-ROW-MARKER-{i:03d} │\n" for i in range(200))
    footer_lines = "└───┘\nbig-footer\n"
    big_body = header_lines + body_rows + footer_lines
    big_path = _write_menu_file(big_body, ts_ns=base + 11, slug="m11")
    paths.append(big_path)
    # Write the meta sidecar with truncate_at:null for the BIG one.
    meta = menu_emit.meta_path_for(big_path)
    meta.write_text(json.dumps({"truncate_at": None}))

    text, _ = menu_emit._compose_payload(paths)

    # With truncate_at:null, per-menu shaping is skipped. The final
    # safety net (line_safe_truncate at 9500) still applies, so the
    # output is bounded — but it's NOT the per-row-bisect form that
    # _truncate_big_menu produces.
    assert len(text) <= menu_emit.TOTAL_BUDGET
    # The 'rows truncated' marker from _truncate_big_menu must NOT
    # appear — that would prove per-menu shaping ran despite our null.
    assert "rows truncated" not in text, (
        "per-menu shaping ran despite truncate_at:null — the 'rows truncated' "
        "marker is a fingerprint of _truncate_big_menu execution"
    )


# ---------------------------------------------------------------------------
# 22. _compose_payload — truncate_at: <int> overrides per_new
# ---------------------------------------------------------------------------


def test_compose_payload_truncate_at_int_overrides_per_new_cap():
    """An explicit ``truncate_at: 800`` caps THIS menu's contribution at 800 chars."""
    base = time.time_ns()
    paths = []
    # 10 small menus → title stubs.
    for i in range(10):
        paths.append(_write_menu_file(f"Title-{i:02d}\nbody\n", ts_ns=base + i, slug=f"m{i:02d}"))
    # Newest-1: small filler.
    paths.append(_write_menu_file("Small\nbody\n", ts_ns=base + 10, slug="m10"))
    # Newest-0: LARGE menu with truncate_at=800. _truncate_big_menu
    # gets called with budget=800 instead of per_new.
    header_lines = "BIG\n┏━┓\n┃ ┃\n┡━┩\n"
    body_rows = "".join(f"│ ROW-{i:03d} │\n" for i in range(300))
    footer_lines = "└─┘\nfoot\n"
    big = header_lines + body_rows + footer_lines
    assert len(big) > 3000, "test prerequisite: big menu must be much larger than 800"
    big_path = _write_menu_file(big, ts_ns=base + 11, slug="m11")
    paths.append(big_path)
    meta = menu_emit.meta_path_for(big_path)
    meta.write_text(json.dumps({"truncate_at": 800}))

    text, _ = menu_emit._compose_payload(paths)

    # Final composition stays under the overall budget.
    assert len(text) <= menu_emit.TOTAL_BUDGET
    # The big menu's contribution was shaped via _truncate_big_menu
    # because len(big) > 800 — confirm by presence of the indicator.
    assert "rows truncated" in text

    # The big menu's "BIG" header survives.
    assert "BIG" in text


# ---------------------------------------------------------------------------
# 23. _compose_payload — truncate_at: <int> that menu already fits = pass-through
# ---------------------------------------------------------------------------


def test_compose_payload_truncate_at_int_passes_through_when_menu_already_fits():
    """If ``truncate_at: 5000`` and the menu is 400 chars, it's emitted whole.

    The override only kicks in when shaping is needed (``len(t) > override``).
    """
    base = time.time_ns()
    paths = []
    for i in range(10):
        paths.append(_write_menu_file(f"T-{i:02d}\nbody\n", ts_ns=base + i, slug=f"m{i:02d}"))
    paths.append(_write_menu_file("Small\nbody\n", ts_ns=base + 10, slug="m10"))

    # Build a 400-char menu — small enough that any cap >= 400 leaves it intact.
    body = "M-HEADER\n" + ("padding-row-data\n" * 20)  # ~350 chars
    assert 200 < len(body) < 500
    p = _write_menu_file(body, ts_ns=base + 11, slug="m11")
    meta = menu_emit.meta_path_for(p)
    meta.write_text(json.dumps({"truncate_at": 5000}))
    paths.append(p)

    text, _ = menu_emit._compose_payload(paths)
    # The menu's content survived intact (no shaping needed).
    assert "M-HEADER" in text
    # All body rows present (every 'padding-row-data' line survived).
    assert text.count("padding-row-data") == 20
    assert len(text) <= menu_emit.TOTAL_BUDGET
