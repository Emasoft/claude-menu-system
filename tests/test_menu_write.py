"""Unit tests for ``scripts/menu_write.py`` — the public queue-a-menu CLI.

These tests exercise the real ``_cli`` / ``write_menu`` entry points. The
autouse fixture in ``conftest.py`` points ``TMPDIR`` at a per-test tmp dir
and pins ``CLAUDE_SESSION_ID``, so queue writes land in an isolated dir and
never touch the developer's real queue.

Covered here:
  - H1: ``--help`` / ``-h`` print usage and exit 0 (was mis-read as a spec
    path -> "invalid JSON" + exit 2).
  - happy path: a valid spec file is rendered, queued, and the queue path is
    printed; the sibling ``.actions.json`` exists beside it with the map.
  - M4: an all-disabled / empty menu fails fast through write_menu -> exit 3
    (the renderer raises ValueError, menu_write maps it to code 3).
  - error codes: invalid JSON -> 2, spec error -> 3.
"""

from __future__ import annotations

import json
from pathlib import Path

import menu_write
import pytest
from menu_queue import actions_path_for


def test_cli_help_flag_prints_usage_and_returns_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """H1: ``menu_write.py --help`` prints usage and exits 0."""
    for flag in ("--help", "-h"):
        rc = menu_write._cli(["menu_write.py", flag])
        assert rc == 0
        out = capsys.readouterr().out
        assert "usage: menu_write.py" in out


def test_cli_no_args_returns_two_with_usage(capsys: pytest.CaptureFixture[str]) -> None:
    """No spec argument -> usage to stderr + exit 2."""
    rc = menu_write._cli(["menu_write.py"])
    assert rc == 2
    assert "usage: menu_write.py" in capsys.readouterr().err


def test_cli_valid_menu_spec_writes_queue_file_and_actions_sidecar(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Happy path: a valid menu spec queues a file and writes the actions sidecar."""
    spec_file = tmp_path / "menu.json"
    spec_file.write_text(
        json.dumps(
            {
                "spec_version": 1,
                "mode": "menu",
                "plugin": "cms",
                "slug": "write-test",
                "header": "Pick",
                "rows": [
                    {"key": "1", "action_id": "alpha", "label": "Alpha"},
                    {"key": "2", "action_id": "beta", "label": "Beta"},
                ],
            }
        ),
        encoding="utf-8",
    )
    rc = menu_write._cli(["menu_write.py", str(spec_file)])
    assert rc == 0
    printed = capsys.readouterr().out.strip()
    queue_path = Path(printed)
    assert queue_path.is_file()
    assert queue_path.name.endswith(".menu.md")

    # The sidecar lives beside the queue file (NOT at the spec path).
    sidecar = actions_path_for(queue_path)
    assert sidecar.is_file()
    assert json.loads(sidecar.read_text(encoding="utf-8")) == {"1": "alpha", "2": "beta"}


def test_cli_all_disabled_menu_returns_three(tmp_path: Path) -> None:
    """M4: an all-disabled menu raises in the renderer -> write_menu maps to exit 3."""
    spec_file = tmp_path / "disabled.json"
    spec_file.write_text(
        json.dumps(
            {
                "spec_version": 1,
                "mode": "menu",
                "plugin": "cms",
                "slug": "all-disabled",
                "header": "Nothing",
                "rows": [
                    {"key": "1", "action_id": "a", "label": "A", "disabled": True},
                    {"key": "2", "action_id": "b", "label": "B", "disabled": True},
                ],
            }
        ),
        encoding="utf-8",
    )
    rc = menu_write._cli(["menu_write.py", str(spec_file)])
    assert rc == 3


def test_cli_invalid_json_returns_two(tmp_path: Path) -> None:
    """A malformed JSON spec is reported as exit 2."""
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    rc = menu_write._cli(["menu_write.py", str(bad)])
    assert rc == 2


def test_cli_spec_error_returns_three(tmp_path: Path) -> None:
    """A valid-JSON but schema-invalid spec is reported as exit 3."""
    bad = tmp_path / "schema-bad.json"
    # Missing required 'mode' field -> SpecError -> exit 3.
    bad.write_text(json.dumps({"spec_version": 1, "plugin": "cms", "slug": "x"}), encoding="utf-8")
    rc = menu_write._cli(["menu_write.py", str(bad)])
    assert rc == 3


def test_cli_duplicate_key_menu_returns_three(tmp_path: Path) -> None:
    """M1 integration: a dup-key menu spec fails validation via the CLI (exit 3)."""
    dup = tmp_path / "dup.json"
    dup.write_text(
        json.dumps(
            {
                "spec_version": 1,
                "mode": "menu",
                "plugin": "cms",
                "slug": "dup",
                "header": "Pick",
                "rows": [
                    {"key": "0", "action_id": "a", "label": "A"},
                    {"key": "0", "action_id": "b", "label": "B"},
                ],
            }
        ),
        encoding="utf-8",
    )
    rc = menu_write._cli(["menu_write.py", str(dup)])
    assert rc == 3
