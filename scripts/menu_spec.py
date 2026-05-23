#!/usr/bin/env python3
"""JSON spec schema + validator for the menu system.

A spec is a dict with required top-level fields:

    {
      "spec_version": 1,            # int — start at 1, future-compat
      "mode":        "menu",         # one of MODES
      "plugin":      "cpv",          # short caller plugin name (queue path component)
      "slug":        "first-contact",# caller-supplied tag for debugging
      ...mode-specific fields...
    }

This module centralises:
  - Mode dispatch (which renderer to call)
  - Required-field validation per mode
  - Field aliasing — e.g. ``rows`` ↔ ``items``, ``header`` ↔ ``title``
  - Forward-compatible spec versioning — unknown ``spec_version`` is a
    non-fatal warning, not an error; the renderer best-efforts on
    known fields.

Keeping spec handling out of the renderer means the renderer stays
pure (input dict in, rendered string out) and the same validation
runs whether the spec arrives via the skill, the test suite, or the
``/menu-render`` ad-hoc command.
"""

from __future__ import annotations

import os
import sys
import warnings
from typing import Any

# Ensure sibling modules resolve when this script runs from any cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Single source of truth for reserved static keys (M/B/X/0/A). Importing
# from menu_render means the validator and renderer can never disagree
# about which keys bypass renumbering. menu_render has no reverse import
# of menu_spec, so this is dependency-cycle-safe.
from menu_render import _STATIC_KEYS

# All renderer modes. Each maps to a renderer function in menu_render.
MODES: frozenset[str] = frozenset(
    {
        "menu",
        "summary",
        "breakdown",
        "status_table",
        "panel",
        "multi_box",
        "progress",
        "confirm",
    }
)

# Spec versions we know how to render. Unknown versions get a warning
# and a best-effort render against the latest known schema.
KNOWN_SPEC_VERSIONS: frozenset[int] = frozenset({1})
LATEST_SPEC_VERSION: int = 1

# Field aliases — defensive parsing so plugin authors can use either
# spelling. Token-reporter has a similar ``_USAGE_FIELD_ALIASES`` table.
_ALIASES: dict[str, list[str]] = {
    "rows": ["items", "entries"],
    "header": ["title"],
    "footer": ["caption"],
    "plugin": ["caller", "owner"],
    "slug": ["tag", "name"],
}


class SpecError(ValueError):
    """Raised when a spec is malformed in a way the renderer cannot recover."""


def _resolve_aliases(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with every alias mapped to its canonical key.

    The original spec is not mutated. If a canonical key is already
    present, alias values are ignored.
    """
    out = dict(spec)
    for canonical, alts in _ALIASES.items():
        if canonical in out:
            continue
        for alt in alts:
            if alt in out:
                out[canonical] = out[alt]
                break
    return out


def _require(spec: dict[str, Any], key: str, expected_type: type | tuple[type, ...]) -> Any:
    if key not in spec:
        raise SpecError(f"missing required field: {key!r}")
    value = spec[key]
    if not isinstance(value, expected_type):
        type_name = (
            expected_type.__name__
            if isinstance(expected_type, type)
            else "/".join(t.__name__ for t in expected_type)
        )
        raise SpecError(f"field {key!r} must be {type_name}, got {type(value).__name__}")
    return value


def validate_top_level(spec: Any) -> dict[str, Any]:
    """Validate top-level frame + return alias-resolved copy.

    Required: spec_version (int), mode (str in MODES), plugin (str), slug (str).
    Optional: truncate_at (positive int or null) — see ``_validate_truncate_at``.
    Unknown spec_version → warning, not error.
    """
    if not isinstance(spec, dict):
        raise SpecError(f"spec must be a JSON object, got {type(spec).__name__}")
    spec = _resolve_aliases(spec)
    version = _require(spec, "spec_version", int)
    if version not in KNOWN_SPEC_VERSIONS:
        warnings.warn(
            f"unknown spec_version {version}; rendering with v{LATEST_SPEC_VERSION} schema",
            stacklevel=2,
        )
    mode = _require(spec, "mode", str)
    if mode not in MODES:
        raise SpecError(f"unknown mode {mode!r}; allowed: {sorted(MODES)}")
    _require(spec, "plugin", str)
    _require(spec, "slug", str)
    _validate_truncate_at(spec)
    return spec


def _validate_truncate_at(spec: dict[str, Any]) -> None:
    """Validate the optional ``truncate_at`` field.

    Contract:
      - absent OR null  → emit hook uses the default budget (9500 chars)
      - positive int    → emit hook caps THIS menu at that char count
      - anything else   → SpecError

    The field is consumed at emit time by ``menu_emit._compose_payload``
    via the ``.meta.json`` sidecar; it does NOT affect render output.
    A null value is an explicit "disable truncation for this menu" — the
    emit hook will still apply its overall 9500-char queue cap, but the
    per-menu shaping step is skipped so overflow fails loudly rather
    than silently lopping body rows.
    """
    if "truncate_at" not in spec:
        return
    value = spec["truncate_at"]
    if value is None:
        return
    # bool is a subclass of int — reject explicitly so True/False
    # don't sneak through as 1/0.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(
            f"field 'truncate_at' must be a positive int or null, got {type(value).__name__}"
        )
    if value <= 0:
        raise SpecError(
            f"field 'truncate_at' must be > 0 when set (got {value}); "
            f"use null to disable truncation entirely"
        )


def validate_mode(spec: dict[str, Any]) -> dict[str, Any]:
    """Per-mode required-field validation. Assumes ``validate_top_level`` already ran.

    Returns the spec (alias-resolved). Raises ``SpecError`` on failure.
    """
    mode = spec["mode"]
    if mode == "menu":
        _validate_menu(spec)
    elif mode == "summary":
        _validate_summary(spec)
    elif mode == "breakdown":
        _validate_breakdown(spec)
    elif mode == "status_table":
        _validate_status_table(spec)
    elif mode == "panel":
        _validate_panel(spec)
    elif mode == "multi_box":
        _validate_multi_box(spec)
    elif mode == "progress":
        _validate_progress(spec)
    elif mode == "confirm":
        _validate_confirm(spec)
    else:
        # Defensive — should be caught by validate_top_level already.
        raise SpecError(f"no validator for mode {mode!r}")
    return spec


def validate(spec: Any) -> dict[str, Any]:
    """Full validation — top-level + mode-specific. Returns alias-resolved spec."""
    spec = validate_top_level(spec)
    return validate_mode(spec)


# --- Per-mode validators ----------------------------------------------------


def _validate_menu(spec: dict[str, Any]) -> None:
    _require(spec, "header", str)
    rows = _require(spec, "rows", list)
    seen_keys: dict[str, int] = {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SpecError(f"menu row {i} must be an object, got {type(row).__name__}")
        if "key" not in row or not isinstance(row["key"], str):
            raise SpecError(f"menu row {i} missing 'key' (string)")
        if "label" not in row or not isinstance(row["label"], str):
            raise SpecError(f"menu row {i} missing 'label' (string)")
        # Key shape enforcement — silent collisions in action_map cause
        # the wrong route to fire when two rows share a key (last-write
        # wins), so we reject duplicates / empties / overlong unknowns
        # at spec-load time.
        key = row["key"]
        if key == "":
            raise SpecError(f"menu row {i} has empty 'key' (must be a non-empty string)")
        # Multi-char keys are only valid when they're in the static
        # allow-list (e.g. future-proofing for new reserved nav letters).
        # Anything else (e.g. "Esc", "Tab", "Enter") must be a single
        # character — the renderer + the user's reply are character-level.
        if len(key) != 1 and key not in _STATIC_KEYS:
            raise SpecError(
                f"menu row {i} has multi-character 'key' {key!r}; "
                f"keys must be a single character unless they are in the "
                f"reserved static set {sorted(_STATIC_KEYS)}"
            )
        if key in seen_keys:
            raise SpecError(
                f"menu row {i} duplicates 'key' {key!r} already used by row "
                f"{seen_keys[key]}; every row must have a unique key (silent "
                f"collision in action_map would otherwise overwrite earlier rows)"
            )
        seen_keys[key] = i


def _validate_summary(spec: dict[str, Any]) -> None:
    counts = _require(spec, "counts", dict)
    for sev, value in counts.items():
        if not isinstance(sev, str):
            raise SpecError(f"summary count key {sev!r} must be a string")
        if not isinstance(value, int) or value < 0:
            raise SpecError(f"summary count for {sev!r} must be a non-negative int")


def _validate_breakdown(spec: dict[str, Any]) -> None:
    rows = _require(spec, "rows", list)
    columns = spec.get("columns")
    if columns is not None and not isinstance(columns, list):
        raise SpecError("breakdown 'columns' must be a list of strings if present")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SpecError(f"breakdown row {i} must be an object")
        if "label" not in row or not isinstance(row["label"], str):
            raise SpecError(f"breakdown row {i} missing 'label' (string)")
        if "counts" not in row or not isinstance(row["counts"], dict):
            raise SpecError(f"breakdown row {i} missing 'counts' (object)")


def _validate_status_table(spec: dict[str, Any]) -> None:
    rows = _require(spec, "rows", list)
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SpecError(f"status_table row {i} must be an object")
        if "label" not in row or not isinstance(row["label"], str):
            raise SpecError(f"status_table row {i} missing 'label' (string)")
        if "status" not in row or not isinstance(row["status"], str):
            raise SpecError(f"status_table row {i} missing 'status' (string)")


def _validate_panel(spec: dict[str, Any]) -> None:
    _require(spec, "header", str)
    body = spec.get("body", [])
    if not isinstance(body, list):
        raise SpecError("panel 'body' must be a list of strings if present")
    for i, line in enumerate(body):
        if not isinstance(line, str):
            raise SpecError(f"panel body[{i}] must be a string")


def _validate_multi_box(spec: dict[str, Any]) -> None:
    boxes = _require(spec, "boxes", list)
    if not boxes:
        raise SpecError("multi_box 'boxes' must be a non-empty list")
    for i, box in enumerate(boxes):
        if not isinstance(box, dict):
            raise SpecError(f"multi_box boxes[{i}] must be an object")
        # Each box must have its own valid panel spec embedded.
        if "header" not in box or not isinstance(box["header"], str):
            raise SpecError(f"multi_box boxes[{i}] missing 'header' (string)")


def _validate_progress(spec: dict[str, Any]) -> None:
    _require(spec, "header", str)
    current = _require(spec, "current", int)
    total = _require(spec, "total", int)
    if current < 0:
        raise SpecError(f"progress 'current' must be >= 0, got {current}")
    if total <= 0:
        raise SpecError(f"progress 'total' must be > 0, got {total}")


def _validate_confirm(spec: dict[str, Any]) -> None:
    _require(spec, "header", str)
    # 'yes_label' and 'no_label' are optional (default Yes/No).
    for opt in ("yes_label", "no_label"):
        if opt in spec and not isinstance(spec[opt], str):
            raise SpecError(f"confirm {opt!r} must be a string if present")


# --- Stdlib-friendly CLI hook for ad-hoc validation -------------------------


def _cli(argv: list[str]) -> int:
    import json

    if len(argv) < 2:
        print("usage: menu_spec.py <spec-path|->", file=sys.stderr)
        return 2
    src = argv[1]
    raw = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    try:
        spec = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"menu_spec: invalid JSON — {exc}", file=sys.stderr)
        return 2
    try:
        validate(spec)
    except SpecError as exc:
        print(f"menu_spec: {exc}", file=sys.stderr)
        return 3
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
