"""Integration tests for the 10K hook-output cap and tiered truncation.

Exercises ``menu_emit._compose_payload`` end-to-end against the three
documented tiers (SMALL ≤1000, BIG >1000, OVERFLOW >9500 or >10 menus)
and the line-safe-truncate hard cap that backstops all paths.

These are integration tests — they call ``_compose_payload`` directly
on real on-disk menu files written into the per-test queue dir, with
deterministic filenames (zero-padded explicit timestamps) so order is
reproducible. Unit coverage of menu_emit's helpers (``_extract_title``,
``_truncate_big_menu``) belongs in ``test_menu_emit.py``; this file
focuses on the budget contract: no caller ever gets a payload exceeding
``TOTAL_BUDGET`` and the deletion list always matches the input.

External deps: filesystem (real), tempfile (real). No mocks — the
conftest.py autouse fixture isolates ``TMPDIR`` per-test, so the queue
dir is a clean scratch path every run.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import menu_queue
import pytest
from menu_emit import (
    MAX_FULL_MENUS_ON_OVERFLOW,
    SMALL_MENU_THRESHOLD,
    TOTAL_BUDGET,
    _compose_payload,
)

# ---------------------------------------------------------------------------
# Tempfile cache reset — mirrors the pattern in test_menu_queue.py.
# Without this, ``tempfile.gettempdir()`` caches the first-resolved
# tempdir, so the autouse TMPDIR=tmp_path isolation from conftest.py is
# silently broken on the second test in the same process.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_tempfile_cache():
    """Force tempfile.gettempdir() to re-read TMPDIR each test."""
    tempfile.tempdir = None
    yield
    tempfile.tempdir = None


# ---------------------------------------------------------------------------
# Helpers — deterministic menu-file creation.
# ---------------------------------------------------------------------------
#
# We write directly under ``menu_queue.session_dir()`` with an explicit
# zero-padded timestamp prefix so files sort by name in a predictable
# order. ``list_pending_menus`` and ``_compose_payload`` both rely on
# ASCII sort = chronological order; explicit timestamps prevent test
# flakes from time-based collisions on fast machines.


def _write_menu(index: int, content: str) -> Path:
    """Write ``content`` to a queue file with a deterministic name.

    Filename embeds a 20-digit zero-padded index so files sort in the
    order they were written, regardless of how fast the test runs.
    """
    sd = menu_queue.session_dir()
    name = f"{index:020d}-test-{index}.menu.md"
    path = sd / name
    path.write_text(content, encoding="utf-8")
    return path


def _make_menu(line_count: int, line_width: int = 50) -> str:
    """Build a menu-shaped string with ``line_count`` non-trivial lines.

    Includes a top border, header row, data rows, and a bottom border so
    the ``_truncate_big_menu`` shape-preserving logic has something to
    keep. Each line is at most ``line_width`` chars (under typical
    terminal width).
    """
    border = "┏" + "━" * (line_width - 2) + "┓"
    bottom = "┗" + "━" * (line_width - 2) + "┛"
    header = "┃ #  ┃ Name" + " " * (line_width - 13) + "┃"
    sep = "┣" + "━" * (line_width - 2) + "┫"
    rows = [f"┃ {i:2d} ┃ row-{i:<{line_width - 12}}┃" for i in range(line_count)]
    footer = " Press [Q] to quit"
    parts = [border, header, sep, *rows, bottom, footer]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Test 1 — SMALL alone (≤1000 chars).
# ---------------------------------------------------------------------------


def test_small_alone_emitted_unchanged():
    """A single SMALL menu (300 chars) is emitted verbatim with full deletion."""
    content = "x" * 300
    p = _write_menu(0, content)

    payload, to_delete = _compose_payload([p])

    assert payload == content, "SMALL menu must be emitted untouched"
    assert to_delete == [p], "files_to_delete must equal input list"
    assert len(payload) <= TOTAL_BUDGET
    assert len(content) <= SMALL_MENU_THRESHOLD  # sanity: still SMALL


# ---------------------------------------------------------------------------
# Test 2 — SMALL × 3 (total ~900 chars, well under budget).
# ---------------------------------------------------------------------------


def test_three_small_menus_concatenated_with_blank_separators():
    """Three SMALL menus (~900 chars total) are concat'd with blank-line joiners."""
    contents = ["alpha" * 60, "beta" * 60, "gamma" * 50]  # ~300, ~240, ~250
    paths = [_write_menu(i, c) for i, c in enumerate(contents)]

    payload, to_delete = _compose_payload(paths)

    # Verify exact concatenation order + joiner.
    assert payload == "\n\n".join(contents), (
        "SMALL menus must be joined by exactly one blank line (\\n\\n)"
    )
    assert to_delete == paths
    assert len(payload) <= TOTAL_BUDGET
    # All three menu bodies must be present byte-for-byte.
    for c in contents:
        assert c in payload


# ---------------------------------------------------------------------------
# Test 3 — BIG alone (5000 chars, still within budget).
# ---------------------------------------------------------------------------


def test_big_alone_under_budget_emitted_unchanged():
    """A single BIG menu (5000 chars) fits under TOTAL_BUDGET and emits intact."""
    content = "z" * 5000  # > SMALL_MENU_THRESHOLD (1000) but < TOTAL_BUDGET (9500)
    p = _write_menu(0, content)

    payload, to_delete = _compose_payload([p])

    # Single menu under TOTAL_BUDGET → no overflow path triggered →
    # whole content is emitted as-is. ``_truncate_big_menu`` is NOT
    # invoked in this branch because total fits.
    assert payload == content
    assert to_delete == [p]
    assert len(payload) <= TOTAL_BUDGET
    assert len(content) > SMALL_MENU_THRESHOLD  # sanity: actually BIG


# ---------------------------------------------------------------------------
# Test 4 — BIG × 3 fitting under budget (~6000 chars total).
# ---------------------------------------------------------------------------


def test_three_big_menus_under_budget_emitted_intact():
    """Three BIG menus (2000 chars each) totalling ~6004 fit and emit intact."""
    contents = ["A" * 2000, "B" * 2000, "C" * 2000]
    paths = [_write_menu(i, c) for i, c in enumerate(contents)]

    # Total = 6000 content + 4 joiner chars = 6004 — under TOTAL_BUDGET
    # AND under 10-menu count, so no overflow path runs.
    payload, to_delete = _compose_payload(paths)

    assert payload == "\n\n".join(contents)
    assert to_delete == paths
    assert len(payload) <= TOTAL_BUDGET
    # Each BIG menu present byte-for-byte (no truncation).
    for c in contents:
        assert c in payload


# ---------------------------------------------------------------------------
# Test 5 — OVERFLOW by count (12 menus × 100 chars, under size cap).
# ---------------------------------------------------------------------------


def test_overflow_by_count_keeps_newest_two_full_and_stubs_older():
    """12 SMALL menus trigger the count-overflow path: 10 stubs + 2 full."""
    # 12 menus × 100 chars = 1200 chars — well under TOTAL_BUDGET but
    # ``len(contents) > 10`` triggers the overflow branch.
    contents = [_make_menu(line_count=3, line_width=20) for _ in range(12)]
    # _make_menu produces ~100-200 chars each — small enough for SMALL,
    # large enough for _extract_title to return the header row.
    paths = [_write_menu(i, c) for i, c in enumerate(contents)]

    payload, to_delete = _compose_payload(paths)

    # All input files are slated for deletion (overflow still consumes them).
    assert to_delete == paths
    assert len(payload) <= TOTAL_BUDGET
    # Overflow path emits a stub block header.
    assert "Older menus (truncated):" in payload, "OVERFLOW path must label the stub block"
    # The newest MAX_FULL_MENUS_ON_OVERFLOW (=2) menus stay full → their
    # full content must be present.
    for c in contents[-MAX_FULL_MENUS_ON_OVERFLOW:]:
        assert c in payload, "newest 2 menus must appear in full"
    # Stub block has one "• ... (truncated)" bullet per older menu.
    # Count bullets specifically (not "(truncated)" — that substring also
    # appears in the "Older menus (truncated):" block header).
    older_count = len(contents) - MAX_FULL_MENUS_ON_OVERFLOW
    bullet_count = sum(
        1 for line in payload.splitlines() if line.startswith("• ") and line.endswith("(truncated)")
    )
    assert bullet_count == older_count, f"expected {older_count} stub bullets, got {bullet_count}"


# ---------------------------------------------------------------------------
# Test 6 — OVERFLOW by size (5 menus × 3000 chars = ~15K, over budget).
# ---------------------------------------------------------------------------


def test_overflow_by_size_truncates_to_under_budget():
    """5 menus × 3000 chars (15K total) overflow by size → ≤ TOTAL_BUDGET."""
    # Build menus with real shape so _truncate_big_menu can do its job
    # (header + body + footer). Each ~3000 chars.
    contents = [_make_menu(line_count=40, line_width=70) for _ in range(5)]
    # Sanity: each is > SMALL_MENU_THRESHOLD so they trigger the
    # ``_truncate_big_menu`` branch inside the overflow path.
    assert all(len(c) > SMALL_MENU_THRESHOLD for c in contents)
    total_raw = sum(len(c) for c in contents)
    assert total_raw > TOTAL_BUDGET  # confirms we're forcing overflow

    paths = [_write_menu(i, c) for i, c in enumerate(contents)]

    payload, to_delete = _compose_payload(paths)

    assert to_delete == paths
    # HARD CAP — this is the whole contract of this test file.
    assert len(payload) <= TOTAL_BUDGET, (
        f"payload {len(payload)} exceeded TOTAL_BUDGET {TOTAL_BUDGET}"
    )
    # Older menus became stubs.
    assert "Older menus (truncated):" in payload
    assert payload.count("(truncated)") >= len(contents) - MAX_FULL_MENUS_ON_OVERFLOW


# ---------------------------------------------------------------------------
# Test 7 — EXTREME OVERFLOW (100 menus × 200 chars).
# ---------------------------------------------------------------------------


def test_extreme_overflow_holds_hard_cap():
    """100 small menus stress the overflow path; hard cap must hold."""
    contents = [_make_menu(line_count=5, line_width=30) for _ in range(100)]
    paths = [_write_menu(i, c) for i, c in enumerate(contents)]

    payload, to_delete = _compose_payload(paths)

    # The whole point: regardless of how many menus pile up, the
    # emitted payload MUST NOT exceed TOTAL_BUDGET. This is the
    # invariant the 10K cap exists to enforce.
    assert len(payload) <= TOTAL_BUDGET, (
        f"100-menu payload {len(payload)} exceeded TOTAL_BUDGET {TOTAL_BUDGET}"
    )
    # All 100 files queued for deletion — emit always consumes everything
    # it processed, even when most became stubs or got truncated.
    assert to_delete == paths
    assert len(to_delete) == 100


# ---------------------------------------------------------------------------
# Test 8 — Parametrized hard-cap check across multiple sizes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "menu_count,line_count,line_width",
    [
        (1, 5, 40),  # tiny SMALL alone
        (5, 5, 40),  # five SMALL — no overflow
        (10, 10, 50),  # boundary: exactly 10 menus
        (11, 10, 50),  # 11 menus → triggers count-overflow
        (3, 100, 80),  # 3 BIG menus (~8000 chars each) → size-overflow
        (50, 5, 30),  # 50 small menus → count-overflow
        (500, 10, 40),  # extreme volume → stress hard cap
    ],
)
def test_hard_cap_holds_across_sizes(menu_count, line_count, line_width):
    """For every menu-volume combination, len(payload) ≤ TOTAL_BUDGET."""
    contents = [_make_menu(line_count, line_width) for _ in range(menu_count)]
    paths = [_write_menu(i, c) for i, c in enumerate(contents)]

    payload, to_delete = _compose_payload(paths)

    assert len(payload) <= TOTAL_BUDGET, (
        f"len(payload)={len(payload)} > TOTAL_BUDGET={TOTAL_BUDGET} "
        f"(menu_count={menu_count}, line_count={line_count}, "
        f"line_width={line_width})"
    )
    # All inputs always go on the deletion list — emit is atomic over
    # what it processed, never partial.
    assert to_delete == paths


# ---------------------------------------------------------------------------
# Test 9 — files_to_delete contract: always equals the input list.
# ---------------------------------------------------------------------------


def test_files_to_delete_always_matches_input_list():
    """Across all tiers, files_to_delete must equal the input list verbatim."""
    # Build a mix of SMALL, BIG, OVERFLOW scenarios in one test by running
    # _compose_payload over three distinct queue layouts and asserting the
    # delete-list contract for each.
    scenarios = [
        # (label, contents)
        ("single SMALL", ["x" * 200]),
        ("multiple SMALL", ["a" * 300, "b" * 300, "c" * 300]),
        ("single BIG fits", ["q" * 5000]),
        ("size overflow", [_make_menu(40, 70) for _ in range(5)]),
        ("count overflow", [_make_menu(3, 20) for _ in range(15)]),
        ("extreme volume", [_make_menu(5, 30) for _ in range(80)]),
    ]
    for label, contents in scenarios:
        # Fresh queue dir state per scenario — wipe any leftovers from
        # the prior iteration so we're measuring this scenario only.
        sd = menu_queue.session_dir()
        for old in sd.glob(f"*{menu_queue.MENU_SUFFIX}"):
            old.unlink()

        paths = [_write_menu(i, c) for i, c in enumerate(contents)]
        _, to_delete = _compose_payload(paths)

        assert to_delete == paths, (
            f"[{label}] files_to_delete must equal input list — "
            f"emit always consumes everything it processed. "
            f"input={len(paths)} files, returned={len(to_delete)} files"
        )


# ---------------------------------------------------------------------------
# Test 10 — line_safe_truncate fallback when stubs alone exceed budget.
# ---------------------------------------------------------------------------


def test_line_safe_truncate_fallback_when_stubs_overflow():
    """200 menus → stub block alone exceeds budget → line_safe_truncate kicks in."""
    # Each stub line is ~80 chars (``• <title>  (truncated)\\n``). With
    # ~200 menus, the stub block alone (≈16K chars) blows past the
    # 9500-char TOTAL_BUDGET, forcing the ``remaining <= 0`` branch in
    # _compose_payload that line-safe-truncates the stub block itself.
    # Use a 50-char title so each stub line is solidly readable.
    title_text = "A" * 50  # 50-char title — well-formed for _extract_title
    # Menu shape: title on its own line so _extract_title returns it.
    contents = [f"{title_text}\nbody-{i}\nfoot-{i}" for i in range(200)]
    paths = [_write_menu(i, c) for i, c in enumerate(contents)]

    payload, to_delete = _compose_payload(paths)

    # The hard cap MUST hold — this is the line-safe-truncate fallback
    # path. If this assertion fails, the 10K cap is not being enforced.
    assert len(payload) <= TOTAL_BUDGET, (
        f"line-safe-truncate fallback failed: payload={len(payload)} > TOTAL_BUDGET={TOTAL_BUDGET}"
    )
    # All 200 files still scheduled for deletion (truncation does not
    # exempt them from being consumed).
    assert to_delete == paths
    # When line_safe_truncate actually fires, its default indicator
    # ``…[truncated]`` is appended on its own line.
    assert payload.endswith("…[truncated]"), (
        f"line_safe_truncate must append its indicator on overflow; "
        f"payload tail was {payload[-40:]!r}"
    )
