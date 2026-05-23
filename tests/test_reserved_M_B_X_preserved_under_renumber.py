"""Regression pin for CPV's fixed-key navigation contract.

CPV (and any caller using fixed-key routing) expects the navigation
letters ``M``, ``B``, ``X`` to survive verbatim through ``renumber:true``
— same way ``0`` and ``A`` already did pre-PR. Without this guarantee a
``renumber:true`` menu would renumber a row keyed ``M`` to ``1`` /``2``
/etc., breaking every downstream router that read the spec key and
assumed it would still be ``M`` after render.

These tests pin:
  - The single-source-of-truth ``_STATIC_KEYS`` frozenset.
  - Renderer preservation under ``renumber:true``.
  - Renderer preservation under default-renumber (no explicit field).
  - action_map round-trips reserved keys to their action_ids.
  - Validator + renderer share the same allow-list (no drift).
"""

from __future__ import annotations

import pytest
from menu_render import _STATIC_KEYS, render_menu
from menu_spec import _STATIC_KEYS as SPEC_STATIC_KEYS


def test_static_keys_set_contains_zero_a_m_b_x() -> None:
    """``_STATIC_KEYS`` must be exactly the documented reserved set."""
    assert _STATIC_KEYS == frozenset({"0", "A", "M", "B", "X"})
    assert isinstance(_STATIC_KEYS, frozenset)


def test_static_keys_single_source_of_truth_across_render_and_spec() -> None:
    """``menu_spec`` imports the SAME ``_STATIC_KEYS`` from menu_render.

    If a future refactor accidentally redefines the set in menu_spec.py,
    the two would silently drift and a key valid for the validator could
    be renumbered by the renderer (or vice versa). The import-not-copy
    pattern in menu_spec.py guarantees identity, not just equality.
    """
    assert SPEC_STATIC_KEYS is _STATIC_KEYS


def test_render_menu_renumber_true_keeps_m_b_x_verbatim() -> None:
    """With explicit ``renumber:true``, M/B/X rows keep their literal keys."""
    spec = {
        "header": "CPV main menu",
        "renumber": True,
        "rows": [
            {"key": "1", "action_id": "validate", "label": "Validate plugin"},
            {"key": "2", "action_id": "fix", "label": "Fix findings"},
            {"key": "M", "action_id": "main", "label": "Main menu"},
            {"key": "B", "action_id": "back", "label": "Back"},
            {"key": "X", "action_id": "exit", "label": "Exit"},
        ],
    }
    _text, action_map = render_menu(spec, use_color=False)
    # Numeric rows renumber starting at 1 (they happen to match originals).
    assert action_map["1"] == "validate"
    assert action_map["2"] == "fix"
    # Reserved letters keep their literal keys — that's the whole point.
    assert action_map["M"] == "main"
    assert action_map["B"] == "back"
    assert action_map["X"] == "exit"
    # No phantom keys created from the renumber pass.
    assert set(action_map.keys()) == {"1", "2", "M", "B", "X"}


def test_render_menu_default_renumber_keeps_m_b_x_verbatim() -> None:
    """Default ``renumber`` (unset → True) also preserves M/B/X verbatim.

    Belt-and-braces: the default could regress to ``False`` in a future
    refactor and this test would still pass. So we also assert that the
    purely-numeric rows DID renumber, proving the default is ``True``.
    """
    spec = {
        "header": "CPV submenu",
        # 'renumber' deliberately omitted → renderer default applies.
        "rows": [
            {"key": "7", "action_id": "alpha", "label": "Alpha"},
            {"key": "9", "action_id": "beta", "label": "Beta"},
            {"key": "M", "action_id": "main", "label": "Main"},
        ],
    }
    _text, action_map = render_menu(spec, use_color=False)
    # Numeric rows renumber 1, 2 (the original 7/9 are NOT preserved —
    # proves renumber defaulted to True). If renumber were False, the
    # keys would be "7" and "9" verbatim.
    assert action_map == {"1": "alpha", "2": "beta", "M": "main"}


def test_static_keys_do_not_consume_next_num_slots() -> None:
    """A reserved key sitting BETWEEN numeric rows must not skew renumbering.

    Pre-PR, ``0``/``A`` already had this behaviour; the same must hold
    for ``M``/``B``/``X``. The next-num counter only advances on
    numeric renumber, never on a static-key row.
    """
    spec = {
        "header": "Mixed menu",
        "rows": [
            {"key": "1", "action_id": "first", "label": "First"},
            {"key": "M", "action_id": "main", "label": "Main"},
            {"key": "2", "action_id": "second", "label": "Second"},
            {"key": "B", "action_id": "back", "label": "Back"},
            {"key": "3", "action_id": "third", "label": "Third"},
        ],
    }
    _text, action_map = render_menu(spec, use_color=False)
    # Numeric rows renumber consecutively 1..3, not 1,3,5 with gaps
    # for the static-key rows. That's the contract.
    assert action_map["1"] == "first"
    assert action_map["2"] == "second"
    assert action_map["3"] == "third"
    assert action_map["M"] == "main"
    assert action_map["B"] == "back"
    assert set(action_map.keys()) == {"1", "2", "3", "M", "B"}


@pytest.mark.parametrize("reserved_key", ["0", "A", "M", "B", "X"])
def test_each_reserved_key_individually_survives_renumber(reserved_key: str) -> None:
    """Each reserved key, on its own, must come through ``renumber:true`` intact."""
    spec = {
        "header": "One reserved key",
        "renumber": True,
        "rows": [
            {"key": "1", "action_id": "a", "label": "A"},
            {"key": reserved_key, "action_id": "navigation", "label": "Nav"},
        ],
    }
    _text, action_map = render_menu(spec, use_color=False)
    assert action_map[reserved_key] == "navigation"
    # The numeric row still renumbers to "1".
    assert action_map["1"] == "a"
