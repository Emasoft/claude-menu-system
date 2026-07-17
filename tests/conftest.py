"""Shared pytest fixtures and bootstrap for claude-menu-system tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make scripts/ importable. pyproject.toml [tool.pytest.ini_options]
# pythonpath also handles this, but adding it here too is belt-and-braces
# for callers running pytest from a non-root cwd.
PROJECT_ROOT = Path(__file__).parent.parent
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture(autouse=True)
def _menu_env_isolation(monkeypatch, tmp_path):
    """Isolate every test from the developer's environment.

    Resets the env vars that affect terminal-capability detection so
    tests are deterministic regardless of the shell that launched
    pytest. Tests can opt back in by setting the var explicitly inside
    the test body.
    """
    for var in (
        "NO_COLOR",
        "CLAUDE_MENU_COLOR",
        "CLAUDE_MENU_ASCII",
        "CLAUDE_MENU_MAX_WIDTH",
        "CLAUDE_MENU_TTL_SEC",
        "CLAUDE_MENU_DEBUG",
        "COLORTERM",
        "TERM_PROGRAM",
        "COLUMNS",
        # Session-id env vars: clear the primary + the alternate spelling so the
        # explicit CLAUDE_SESSION_ID default below is the ONLY session source in
        # tests. session_id() checks CLAUDE_CODE_SESSION_ID FIRST, so a real
        # Claude session's value would otherwise leak in and every test that
        # expects "test-session" would get the real id instead.
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID_HOOK",
    ):
        monkeypatch.delenv(var, raising=False)
    # Set TERM to a real-looking value so should_use_color() returns True
    # by default — most tests want color-aware rendering.
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    # Isolate the queue dir per-test so concurrent tests don't collide.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    # Default session id so menu_queue doesn't try to scan transcripts.
    monkeypatch.setenv("CLAUDE_SESSION_ID", "test-session")


@pytest.fixture
def session_id():
    """The deterministic session id set by the autouse fixture."""
    return "test-session"


@pytest.fixture
def queue_root(tmp_path):
    """Absolute path to the queue root under tmp_path."""
    return tmp_path / "claude-menu-system"
