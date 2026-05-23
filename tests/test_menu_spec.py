"""Unit tests for ``scripts/menu_spec.py``.

These tests exercise the real validation logic — they call the public
``validate`` / ``validate_top_level`` / ``validate_mode`` functions with
realistic spec dicts and inspect the actual ``SpecError`` / ``UserWarning``
raised. No internals are mocked; the only "external" surface is the
``warnings`` module, which is captured (not patched) via
``pytest.warns``.

Coverage target: ≥95% line coverage of ``menu_spec.py``.

Test-data construction policy: every spec is a dict literal built inside
the test body. Shared fixtures would obscure which top-level fields are
required for the path under test, and the module's per-mode validators
have non-trivial cross-field dependencies (e.g. menu requires ``header``
+ ``rows[*].key`` + ``rows[*].label``) that are easier to read when the
spec is co-located with the assertions.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest
from menu_spec import (
    KNOWN_SPEC_VERSIONS,
    LATEST_SPEC_VERSION,
    MODES,
    SpecError,
    validate,
    validate_mode,
    validate_top_level,
)

# ---------------------------------------------------------------------------
# Test 1 — MODES contains exactly the 8 documented renderer modes
# ---------------------------------------------------------------------------


def test_modes_contains_exactly_the_eight_documented_renderer_modes() -> None:
    """``MODES`` must equal the eight modes the renderer + skill documents."""
    expected = {
        "menu",
        "summary",
        "breakdown",
        "status_table",
        "panel",
        "multi_box",
        "progress",
        "confirm",
    }
    assert MODES == expected
    # Belt-and-braces: confirm immutability + size so a future PR that
    # accidentally adds a 9th mode without updating callers breaks here.
    assert isinstance(MODES, frozenset)
    assert len(MODES) == 8
    # Sanity-check the surrounding version constants exist + are
    # internally consistent. Not strictly part of the modes assertion,
    # but the docstring couples these in the same top-level frame.
    assert LATEST_SPEC_VERSION in KNOWN_SPEC_VERSIONS


# ---------------------------------------------------------------------------
# Test 2 — minimal valid menu spec round-trips through validate()
# ---------------------------------------------------------------------------


def test_validate_accepts_minimal_valid_menu_spec_and_returns_alias_resolved_copy() -> None:
    """A spec with the minimum required fields for ``mode='menu'`` must pass."""
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "first-contact",
        "header": "Pick an action",
        "rows": [
            {"key": "1", "label": "Validate plugin"},
            {"key": "2", "label": "Validate marketplace"},
        ],
    }
    result = validate(spec)
    # validate() returns the alias-resolved copy — for a spec with no
    # aliases used, this is value-equal to the input.
    assert result == spec
    # _resolve_aliases() builds a new dict, so the returned object must
    # not be the same identity as the input — otherwise downstream
    # mutation in the renderer would leak back to the caller.
    assert result is not spec


# ---------------------------------------------------------------------------
# Test 3 — non-dict specs (list / str / int) raise SpecError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_spec",
    [
        [],
        ["spec_version", 1],
        "not a spec",
        42,
        None,
        3.14,
        (1, 2),
    ],
    ids=["empty-list", "list-of-strings", "string", "int", "none", "float", "tuple"],
)
def test_validate_raises_when_spec_is_not_a_dict(bad_spec: Any) -> None:
    """Top-level frame validation must reject anything that isn't a JSON object."""
    with pytest.raises(SpecError, match="spec must be a JSON object"):
        validate(bad_spec)


# ---------------------------------------------------------------------------
# Test 4 — missing spec_version
# ---------------------------------------------------------------------------


def test_validate_raises_when_spec_version_missing() -> None:
    """Required field ``spec_version`` is enforced at the top-level frame."""
    spec = {
        "mode": "menu",
        "plugin": "cpv",
        "slug": "first-contact",
        "header": "x",
        "rows": [{"key": "1", "label": "y"}],
    }
    with pytest.raises(SpecError, match="missing required field: 'spec_version'"):
        validate(spec)


# ---------------------------------------------------------------------------
# Test 5 — unknown mode value
# ---------------------------------------------------------------------------


def test_validate_raises_when_mode_is_not_in_modes_set() -> None:
    """``mode`` must be one of the eight registered renderer modes."""
    spec = {
        "spec_version": 1,
        "mode": "carousel",  # not a real renderer mode
        "plugin": "cpv",
        "slug": "first-contact",
    }
    with pytest.raises(SpecError, match="unknown mode 'carousel'"):
        validate(spec)


# ---------------------------------------------------------------------------
# Test 6 — missing plugin or slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_field", ["plugin", "slug"])
def test_validate_raises_when_plugin_or_slug_missing(missing_field: str) -> None:
    """Both ``plugin`` and ``slug`` are required at the top-level frame."""
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "first-contact",
        "header": "x",
        "rows": [{"key": "1", "label": "y"}],
    }
    del spec[missing_field]
    with pytest.raises(SpecError, match=f"missing required field: '{missing_field}'"):
        validate(spec)


# ---------------------------------------------------------------------------
# Test 7 — unknown spec_version → UserWarning + best-effort acceptance
# ---------------------------------------------------------------------------


def test_validate_warns_on_unknown_spec_version_but_still_returns_spec() -> None:
    """Forward-compat: future ``spec_version`` values warn, don't raise."""
    future_version = max(KNOWN_SPEC_VERSIONS) + 99
    assert future_version not in KNOWN_SPEC_VERSIONS  # sanity

    spec = {
        "spec_version": future_version,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "first-contact",
        "header": "Header",
        "rows": [{"key": "1", "label": "Item"}],
    }
    with pytest.warns(UserWarning, match="unknown spec_version"):
        result = validate(spec)
    assert result["spec_version"] == future_version
    assert result["mode"] == "menu"

    # Conversely, a KNOWN version must NOT warn. This pinning catches a
    # future regression where the warn() call drifts above the version
    # check.
    known_spec = dict(spec, spec_version=LATEST_SPEC_VERSION)
    with warnings.catch_warnings():
        warnings.simplefilter("error", UserWarning)
        validate(known_spec)  # would raise if a UserWarning fired


# ---------------------------------------------------------------------------
# Test 8 — alias resolution: ``items`` → ``rows``
# ---------------------------------------------------------------------------


def test_alias_items_resolves_to_rows_for_menu_mode() -> None:
    """``items`` is a documented alias for ``rows``."""
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "alias-test",
        "header": "Pick an action",
        "items": [
            {"key": "1", "label": "First"},
            {"key": "2", "label": "Second"},
        ],
    }
    result = validate(spec)
    # Canonical key is now populated by alias resolution.
    assert "rows" in result
    assert result["rows"] == spec["items"]
    # Original alias key is preserved (alias resolution copies, doesn't move).
    assert result["items"] == spec["items"]

    # And the third alias ``entries`` must also resolve to ``rows``.
    spec2 = dict(spec)
    del spec2["items"]
    spec2["entries"] = [{"key": "a", "label": "Alpha"}]
    result2 = validate(spec2)
    assert result2["rows"] == [{"key": "a", "label": "Alpha"}]


# ---------------------------------------------------------------------------
# Test 9 — alias resolution: ``title`` → ``header``
# ---------------------------------------------------------------------------


def test_alias_title_resolves_to_header_for_menu_and_panel() -> None:
    """``title`` is a documented alias for ``header``."""
    # Menu mode — header is mandatory; supplying it as ``title`` must work.
    menu_spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "title-alias",
        "title": "Pick an action",
        "rows": [{"key": "1", "label": "Run"}],
    }
    result = validate(menu_spec)
    assert result["header"] == "Pick an action"

    # Panel mode — header is mandatory there too.
    panel_spec = {
        "spec_version": 1,
        "mode": "panel",
        "plugin": "cpv",
        "slug": "title-alias",
        "title": "Status",
        "body": ["line 1", "line 2"],
    }
    result = validate(panel_spec)
    assert result["header"] == "Status"
    assert result["body"] == ["line 1", "line 2"]


# ---------------------------------------------------------------------------
# Test 10 — menu row missing ``key``
# ---------------------------------------------------------------------------


def test_validate_menu_raises_when_row_missing_key_with_row_index() -> None:
    """``_validate_menu`` must name the offending row index in the error."""
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "menu-key-missing",
        "header": "Pick",
        "rows": [
            {"key": "1", "label": "Good row"},
            {"label": "Bad row — no key"},  # index 1
        ],
    }
    with pytest.raises(SpecError, match=r"menu row 1 missing 'key'"):
        validate(spec)

    # Non-string ``key`` is also rejected with the same per-index message.
    spec_bad_type = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "menu-key-type",
        "header": "Pick",
        "rows": [{"key": 1, "label": "int key"}],  # key must be str
    }
    with pytest.raises(SpecError, match=r"menu row 0 missing 'key'"):
        validate(spec_bad_type)

    # Non-dict row is rejected with its own type-mentioning message.
    spec_bad_row = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "menu-row-type",
        "header": "Pick",
        "rows": ["not-a-dict"],
    }
    with pytest.raises(SpecError, match=r"menu row 0 must be an object, got str"):
        validate(spec_bad_row)

    # Missing 'label' covers the sibling branch (line 167 in menu_spec.py).
    spec_no_label = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "menu-no-label",
        "header": "Pick",
        "rows": [{"key": "1"}],  # no 'label'
    }
    with pytest.raises(SpecError, match=r"menu row 0 missing 'label'"):
        validate(spec_no_label)

    # Non-string 'label' triggers the same branch.
    spec_bad_label = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "menu-bad-label",
        "header": "Pick",
        "rows": [{"key": "1", "label": 42}],
    }
    with pytest.raises(SpecError, match=r"menu row 0 missing 'label'"):
        validate(spec_bad_label)


# ---------------------------------------------------------------------------
# Test 11 — summary count negative / non-int
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("counts", "expected_match"),
    [
        ({"CRITICAL": -1}, r"summary count for 'CRITICAL' must be a non-negative int"),
        ({"MAJOR": "many"}, r"summary count for 'MAJOR' must be a non-negative int"),
        ({"MINOR": 1.5}, r"summary count for 'MINOR' must be a non-negative int"),
        ({"NIT": None}, r"summary count for 'NIT' must be a non-negative int"),
    ],
    ids=["negative-int", "string", "float", "none"],
)
def test_validate_summary_rejects_negative_or_non_int_count(
    counts: dict[str, Any], expected_match: str
) -> None:
    """Counts must be non-negative ints — anything else is a spec error."""
    spec = {
        "spec_version": 1,
        "mode": "summary",
        "plugin": "cpv",
        "slug": "sum-bad-count",
        "counts": counts,
    }
    with pytest.raises(SpecError, match=expected_match):
        validate(spec)


def test_validate_summary_rejects_non_string_count_key() -> None:
    """Severity keys must be strings (loaders sometimes pass int keys)."""
    spec = {
        "spec_version": 1,
        "mode": "summary",
        "plugin": "cpv",
        "slug": "sum-bad-key",
        "counts": {1: 5},  # int key, not str
    }
    with pytest.raises(SpecError, match=r"summary count key 1 must be a string"):
        validate(spec)


def test_validate_summary_accepts_valid_counts_including_zero() -> None:
    """Zero is non-negative — a summary with all-zero counts must pass."""
    spec = {
        "spec_version": 1,
        "mode": "summary",
        "plugin": "cpv",
        "slug": "sum-ok",
        "counts": {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0, "NIT": 0, "PASSED": 12},
    }
    result = validate(spec)
    assert result["counts"]["PASSED"] == 12


# ---------------------------------------------------------------------------
# Test 12 — breakdown row.counts not a dict
# ---------------------------------------------------------------------------


def test_validate_breakdown_raises_when_row_counts_is_not_a_dict() -> None:
    """Each breakdown row needs a ``counts: {...}`` object."""
    spec = {
        "spec_version": 1,
        "mode": "breakdown",
        "plugin": "cpv",
        "slug": "bd-counts-type",
        "columns": ["CRITICAL", "MAJOR"],
        "rows": [
            {"label": "skill-validator", "counts": "not-a-dict"},
        ],
    }
    with pytest.raises(SpecError, match=r"breakdown row 0 missing 'counts'"):
        validate(spec)

    # Missing 'counts' entirely triggers the same error path.
    spec_missing = {
        "spec_version": 1,
        "mode": "breakdown",
        "plugin": "cpv",
        "slug": "bd-counts-missing",
        "rows": [{"label": "skill-validator"}],
    }
    with pytest.raises(SpecError, match=r"breakdown row 0 missing 'counts'"):
        validate(spec_missing)

    # Bad 'columns' shape (non-list) is its own error path.
    spec_bad_cols = {
        "spec_version": 1,
        "mode": "breakdown",
        "plugin": "cpv",
        "slug": "bd-cols",
        "columns": "CRITICAL,MAJOR",  # not a list
        "rows": [{"label": "x", "counts": {}}],
    }
    with pytest.raises(SpecError, match=r"breakdown 'columns' must be a list"):
        validate(spec_bad_cols)

    # Non-dict row covers the ``not isinstance(row, dict)`` branch.
    spec_bad_row = {
        "spec_version": 1,
        "mode": "breakdown",
        "plugin": "cpv",
        "slug": "bd-row-type",
        "rows": ["whoops"],
    }
    with pytest.raises(SpecError, match=r"breakdown row 0 must be an object"):
        validate(spec_bad_row)

    # Missing 'label' covers the label branch.
    spec_no_label = {
        "spec_version": 1,
        "mode": "breakdown",
        "plugin": "cpv",
        "slug": "bd-no-label",
        "rows": [{"counts": {}}],
    }
    with pytest.raises(SpecError, match=r"breakdown row 0 missing 'label'"):
        validate(spec_no_label)


# ---------------------------------------------------------------------------
# Test 13 — status_table row missing label
# ---------------------------------------------------------------------------


def test_validate_status_table_raises_when_row_missing_label_or_status() -> None:
    """Each status_table row needs both ``label`` and ``status`` strings."""
    spec_no_label = {
        "spec_version": 1,
        "mode": "status_table",
        "plugin": "cpv",
        "slug": "st-no-label",
        "rows": [
            {"status": "OK"},  # label missing
        ],
    }
    with pytest.raises(SpecError, match=r"status_table row 0 missing 'label'"):
        validate(spec_no_label)

    # Non-string label triggers the same branch.
    spec_bad_label = {
        "spec_version": 1,
        "mode": "status_table",
        "plugin": "cpv",
        "slug": "st-bad-label",
        "rows": [{"label": 123, "status": "OK"}],
    }
    with pytest.raises(SpecError, match=r"status_table row 0 missing 'label'"):
        validate(spec_bad_label)

    # Missing 'status' covers the status branch (separate code path).
    spec_no_status = {
        "spec_version": 1,
        "mode": "status_table",
        "plugin": "cpv",
        "slug": "st-no-status",
        "rows": [{"label": "tool: gh"}],
    }
    with pytest.raises(SpecError, match=r"status_table row 0 missing 'status'"):
        validate(spec_no_status)

    # Non-dict row covers the type-check branch.
    spec_bad_row = {
        "spec_version": 1,
        "mode": "status_table",
        "plugin": "cpv",
        "slug": "st-bad-row",
        "rows": [42],
    }
    with pytest.raises(SpecError, match=r"status_table row 0 must be an object"):
        validate(spec_bad_row)


# ---------------------------------------------------------------------------
# Test 14 — panel: body non-string element rejected; absent body OK
# ---------------------------------------------------------------------------


def test_validate_panel_rejects_non_string_body_elements_but_accepts_absent_body() -> None:
    """Panel body is optional; if present every element must be a string."""
    # 14a — body absent → OK (panel defaults to []).
    spec_no_body = {
        "spec_version": 1,
        "mode": "panel",
        "plugin": "cpv",
        "slug": "panel-no-body",
        "header": "Heads-up",
    }
    result = validate(spec_no_body)
    assert result["header"] == "Heads-up"

    # 14b — body present + all strings → OK.
    spec_good = dict(spec_no_body, body=["first line", "second line"])
    result_good = validate(spec_good)
    assert result_good["body"] == ["first line", "second line"]

    # 14c — body present with a non-string element → SpecError, index named.
    spec_bad_elem = dict(spec_no_body, body=["ok line", 42, "another line"])
    with pytest.raises(SpecError, match=r"panel body\[1\] must be a string"):
        validate(spec_bad_elem)

    # 14d — body not a list at all → its own error path.
    spec_bad_body = dict(spec_no_body, body="single string instead of list")
    with pytest.raises(SpecError, match=r"panel 'body' must be a list"):
        validate(spec_bad_body)


# ---------------------------------------------------------------------------
# Test 15 — multi_box / progress / confirm edge-case sweep + the two helpers
#            (validate_top_level + validate_mode standalone, no_validator path)
# ---------------------------------------------------------------------------


def test_multi_box_progress_confirm_and_helper_edge_cases() -> None:
    """Combined edge cases for the three remaining per-mode validators
    plus the public ``validate_top_level`` / ``validate_mode`` split.

    Combined into one test (per the brief) so the file stays at exactly
    15 tests while still exercising every per-mode branch.
    """
    # --- 15a — multi_box: empty boxes list ---------------------------------
    spec_empty_boxes = {
        "spec_version": 1,
        "mode": "multi_box",
        "plugin": "cpv",
        "slug": "mb-empty",
        "boxes": [],
    }
    with pytest.raises(SpecError, match=r"multi_box 'boxes' must be a non-empty list"):
        validate(spec_empty_boxes)

    # --- 15b — multi_box: box missing header -------------------------------
    spec_box_no_header = {
        "spec_version": 1,
        "mode": "multi_box",
        "plugin": "cpv",
        "slug": "mb-no-hdr",
        "boxes": [{"body": ["x"]}],  # no 'header'
    }
    with pytest.raises(SpecError, match=r"multi_box boxes\[0\] missing 'header'"):
        validate(spec_box_no_header)

    # --- 15c — multi_box: box is not a dict --------------------------------
    spec_box_not_dict = {
        "spec_version": 1,
        "mode": "multi_box",
        "plugin": "cpv",
        "slug": "mb-bad-box",
        "boxes": ["not-a-dict"],
    }
    with pytest.raises(SpecError, match=r"multi_box boxes\[0\] must be an object"):
        validate(spec_box_not_dict)

    # --- 15d — multi_box: well-formed → OK ---------------------------------
    spec_mb_ok = {
        "spec_version": 1,
        "mode": "multi_box",
        "plugin": "cpv",
        "slug": "mb-ok",
        "boxes": [{"header": "Left"}, {"header": "Right"}],
    }
    assert validate(spec_mb_ok)["boxes"][0]["header"] == "Left"

    # --- 15e — progress: current < 0 ---------------------------------------
    spec_prog_neg = {
        "spec_version": 1,
        "mode": "progress",
        "plugin": "cpv",
        "slug": "p-neg",
        "header": "Working",
        "current": -1,
        "total": 10,
    }
    with pytest.raises(SpecError, match=r"progress 'current' must be >= 0"):
        validate(spec_prog_neg)

    # --- 15f — progress: total <= 0 ----------------------------------------
    spec_prog_zero = {
        "spec_version": 1,
        "mode": "progress",
        "plugin": "cpv",
        "slug": "p-zero",
        "header": "Working",
        "current": 0,
        "total": 0,
    }
    with pytest.raises(SpecError, match=r"progress 'total' must be > 0"):
        validate(spec_prog_zero)

    # Negative total triggers the same branch.
    spec_prog_negtotal = dict(spec_prog_zero, total=-5)
    with pytest.raises(SpecError, match=r"progress 'total' must be > 0"):
        validate(spec_prog_negtotal)

    # --- 15g — progress: well-formed ---------------------------------------
    spec_prog_ok = dict(spec_prog_zero, current=3, total=10)
    assert validate(spec_prog_ok)["current"] == 3

    # --- 15h — confirm: yes_label non-string -------------------------------
    spec_confirm_bad = {
        "spec_version": 1,
        "mode": "confirm",
        "plugin": "cpv",
        "slug": "cf-bad",
        "header": "Proceed?",
        "yes_label": 1,  # must be str
    }
    with pytest.raises(SpecError, match=r"confirm 'yes_label' must be a string"):
        validate(spec_confirm_bad)

    # no_label non-string also rejected.
    spec_confirm_no = {
        "spec_version": 1,
        "mode": "confirm",
        "plugin": "cpv",
        "slug": "cf-bad",
        "header": "Proceed?",
        "no_label": [],
    }
    with pytest.raises(SpecError, match=r"confirm 'no_label' must be a string"):
        validate(spec_confirm_no)

    # --- 15i — confirm: well-formed (labels omitted = defaults) ------------
    spec_confirm_ok = {
        "spec_version": 1,
        "mode": "confirm",
        "plugin": "cpv",
        "slug": "cf-ok",
        "header": "Proceed?",
    }
    assert validate(spec_confirm_ok)["header"] == "Proceed?"

    # --- 15j — validate_top_level returns alias-resolved copy --------------
    top = validate_top_level(
        {
            "spec_version": 1,
            "mode": "menu",
            "caller": "cpv",  # alias for 'plugin'
            "tag": "first-contact",  # alias for 'slug'
        }
    )
    assert top["plugin"] == "cpv"
    assert top["slug"] == "first-contact"

    # --- 15k — validate_mode called directly on a pre-validated dict -------
    pre = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "x",
        "header": "h",
        "rows": [{"key": "1", "label": "a"}],
    }
    assert validate_mode(pre) is pre  # validate_mode returns the same dict

    # --- 15l — top-level: spec_version wrong TYPE (str not int) ------------
    with pytest.raises(SpecError, match=r"field 'spec_version' must be int"):
        validate(
            {
                "spec_version": "1",  # str, not int
                "mode": "menu",
                "plugin": "cpv",
                "slug": "x",
                "header": "h",
                "rows": [],
            }
        )

    # --- 15m — top-level: mode wrong TYPE (int not str) --------------------
    with pytest.raises(SpecError, match=r"field 'mode' must be str"):
        validate(
            {
                "spec_version": 1,
                "mode": 42,
                "plugin": "cpv",
                "slug": "x",
            }
        )

    # --- 15n — alias precedence: canonical key wins over alias -------------
    # When BOTH 'rows' and 'items' are present, 'rows' is kept and 'items'
    # is ignored. This pins the documented behaviour of _resolve_aliases.
    spec_both = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "alias-precedence",
        "header": "h",
        "rows": [{"key": "1", "label": "canonical"}],
        "items": [{"key": "2", "label": "alias — should be ignored"}],
    }
    result = validate(spec_both)
    assert result["rows"][0]["label"] == "canonical"

    # --- 15o — validate_mode defensive branch (no_validator path) ----------
    # Forge a spec that passed top-level validation by spoofing MODES, then
    # restore. This is the only way to hit the ``no validator for mode``
    # defensive arm without monkeypatching the dispatcher.
    spoofed = {
        "spec_version": 1,
        "mode": "ghost_mode",
        "plugin": "cpv",
        "slug": "x",
    }
    # Direct call to validate_mode skips the top-level mode-in-MODES check,
    # so the dispatcher's else-branch fires.
    with pytest.raises(SpecError, match=r"no validator for mode 'ghost_mode'"):
        validate_mode(spoofed)


# ---------------------------------------------------------------------------
# Test 16 — duplicate menu keys are rejected at spec-load time
# ---------------------------------------------------------------------------


def test_validate_menu_rejects_duplicate_keys_with_indices_named() -> None:
    """Two rows with the same key would collide silently in action_map.

    Pre-PR, the second row's action_id would overwrite the first's in
    the action_map (dict last-write-wins). That's an undetectable
    routing bug — the user sees both rows, presses the key, and the
    WRONG action fires. Rejecting duplicates at validate-time turns
    a silent miscompile into a loud spec error.
    """
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "dup-key",
        "header": "Pick",
        "rows": [
            {"key": "1", "action_id": "first", "label": "First"},
            {"key": "2", "action_id": "second", "label": "Second"},
            {"key": "1", "action_id": "third", "label": "Third"},  # COLLIDES with row 0
        ],
    }
    with pytest.raises(SpecError, match=r"menu row 2 duplicates 'key' '1' .* row 0"):
        validate(spec)

    # Duplicate of a reserved key (M) is also rejected — the static
    # key only buys you predictable rendering, not duplicate immunity.
    spec_dup_reserved = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "dup-reserved",
        "header": "Pick",
        "rows": [
            {"key": "M", "action_id": "main", "label": "Main"},
            {"key": "M", "action_id": "main2", "label": "Main again"},
        ],
    }
    with pytest.raises(SpecError, match=r"menu row 1 duplicates 'key' 'M' .* row 0"):
        validate(spec_dup_reserved)


# ---------------------------------------------------------------------------
# Test 17 — empty-string keys are rejected
# ---------------------------------------------------------------------------


def test_validate_menu_rejects_empty_string_key() -> None:
    """An empty key is unreachable (no character a user can type for it)."""
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "empty-key",
        "header": "Pick",
        "rows": [
            {"key": "", "action_id": "ghost", "label": "Unreachable row"},
        ],
    }
    with pytest.raises(SpecError, match=r"menu row 0 has empty 'key'"):
        validate(spec)


# ---------------------------------------------------------------------------
# Test 18 — multi-char non-reserved keys are rejected
# ---------------------------------------------------------------------------


def test_validate_menu_rejects_multi_char_non_reserved_key() -> None:
    """Keys must be single chars unless they're in the reserved static set.

    ``Esc``, ``Tab``, ``Enter`` etc. look reasonable but the renderer +
    user reply are CHARACTER-LEVEL: a user types one char, not a word.
    A spec with ``"key": "Esc"`` would render the literal string in the
    table but no keystroke could match it. Reject at validation time.
    """
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "multi-char-key",
        "header": "Pick",
        "rows": [
            {"key": "Esc", "action_id": "cancel", "label": "Cancel"},
        ],
    }
    with pytest.raises(SpecError, match=r"menu row 0 has multi-character 'key' 'Esc'"):
        validate(spec)

    # Two-char alphanumeric is also rejected (e.g. "12" — not a single keystroke).
    spec_two_char = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "two-char-key",
        "header": "Pick",
        "rows": [{"key": "12", "action_id": "twelve", "label": "Twelve"}],
    }
    with pytest.raises(SpecError, match=r"menu row 0 has multi-character 'key' '12'"):
        validate(spec_two_char)


# ---------------------------------------------------------------------------
# Test 19 — valid reserved keys pass validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reserved_key", ["0", "A", "M", "B", "X"])
def test_validate_menu_accepts_each_reserved_key(reserved_key: str) -> None:
    """``0``, ``A``, ``M``, ``B``, ``X`` are all valid as single-char keys.

    They're "reserved" only in the sense that the renderer skips
    renumber for them — validation-wise they're just normal single-char
    keys. The parametrize sweep proves every member of ``_STATIC_KEYS``
    passes individually.
    """
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": f"reserved-{reserved_key}",
        "header": "Pick",
        "rows": [
            {"key": "1", "action_id": "normal", "label": "Normal row"},
            {"key": reserved_key, "action_id": "nav", "label": "Nav row"},
        ],
    }
    # No exception expected — the validator must accept reserved keys
    # as valid (they're single chars AND in the static allow-list).
    result = validate(spec)
    assert result["rows"][1]["key"] == reserved_key


# ---------------------------------------------------------------------------
# Test 20 — truncate_at validation: positive int / null / absent / invalid
# ---------------------------------------------------------------------------


def test_validate_truncate_at_positive_int_passes() -> None:
    """A positive int ``truncate_at`` overrides the default emit cap."""
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "ta-int",
        "header": "Pick",
        "rows": [{"key": "1", "action_id": "a", "label": "A"}],
        "truncate_at": 5000,
    }
    result = validate(spec)
    assert result["truncate_at"] == 5000


def test_validate_truncate_at_null_passes() -> None:
    """``truncate_at: null`` is the documented "disable truncation" signal."""
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "ta-null",
        "header": "Pick",
        "rows": [{"key": "1", "action_id": "a", "label": "A"}],
        "truncate_at": None,
    }
    result = validate(spec)
    assert result["truncate_at"] is None


def test_validate_truncate_at_absent_passes() -> None:
    """Absent ``truncate_at`` is the most common case (default heuristic)."""
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "ta-absent",
        "header": "Pick",
        "rows": [{"key": "1", "action_id": "a", "label": "A"}],
    }
    result = validate(spec)
    assert "truncate_at" not in result


@pytest.mark.parametrize(
    ("bad_value", "match"),
    [
        (0, r"truncate_at' must be > 0 when set \(got 0\)"),
        (-1, r"truncate_at' must be > 0 when set \(got -1\)"),
        (-100, r"truncate_at' must be > 0 when set \(got -100\)"),
        ("5000", r"truncate_at' must be a positive int or null, got str"),
        (5.0, r"truncate_at' must be a positive int or null, got float"),
        ([5000], r"truncate_at' must be a positive int or null, got list"),
        # bool is a subclass of int but explicitly rejected — True would
        # otherwise be silently interpreted as truncate_at=1.
        (True, r"truncate_at' must be a positive int or null, got bool"),
        (False, r"truncate_at' must be a positive int or null, got bool"),
    ],
    ids=["zero", "negative-1", "negative-100", "str", "float", "list", "true", "false"],
)
def test_validate_truncate_at_rejects_invalid_value(bad_value: object, match: str) -> None:
    """Anything that isn't ``None`` or ``int > 0`` is a SpecError."""
    spec = {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cpv",
        "slug": "ta-bad",
        "header": "Pick",
        "rows": [{"key": "1", "action_id": "a", "label": "A"}],
        "truncate_at": bad_value,
    }
    with pytest.raises(SpecError, match=match):
        validate(spec)
