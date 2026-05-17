"""Cross-session isolation tests for ``scripts/menu_queue.py`` + ``scripts/menu_emit.py``.

Two concurrent Claude Code sessions share the same ``TMPDIR`` (the
conftest autouse fixture sets ``TMPDIR=tmp_path``) but each must see
ONLY its own queued menus, hold its OWN flock, and clean up its OWN
session dir without disturbing the sibling. Tests pass an explicit
``override="session-A"`` / ``override="session-B"`` through every
queue API so the two sessions are simulated within one pytest process
without racing real env-var state.

Coverage target: the path-isolation invariants the queue layout is
designed to provide — no cross-leakage of pending menus, no
cross-leakage of emit consumption, no cross-leakage of flock state,
no cross-leakage of cleanup, and no path-traversal escape from the
queue root via a malicious session_id.

External dependencies (fcntl, os.replace, real filesystem) are real
— no mocks of internal queue logic; only ``sys.stdout`` is captured
via pytest's ``capsys`` to verify emit output structure.
"""

from __future__ import annotations

import fcntl
import json
import tempfile

import menu_emit
import menu_queue
import pytest

# ---------------------------------------------------------------------------
# Per-test isolation of tempfile.gettempdir()
# ---------------------------------------------------------------------------
#
# Same rationale as test_menu_queue.py: stdlib ``tempfile`` caches the
# result of ``gettempdir()`` in ``tempfile.tempdir`` on first call.
# Without busting that cache, every test would resolve back to whatever
# tempdir was first looked up and the per-test ``tmp_path`` isolation
# (and the conftest contract that sets TMPDIR=tmp_path) would silently
# break — tests would pollute each other's queue dirs and the
# session-isolation invariants this file is supposed to verify would
# be unobservable.


@pytest.fixture(autouse=True)
def _reset_tempfile_cache():
    """Force tempfile.gettempdir() to re-read TMPDIR each test."""
    tempfile.tempdir = None
    yield
    tempfile.tempdir = None


# ---------------------------------------------------------------------------
# 1. list_pending_menus is per-session — no cross-leakage
# ---------------------------------------------------------------------------


def test_list_pending_menus_isolates_two_sessions():
    """Two sessions writing into the same TMPDIR see only their own pending menus."""
    # Session A: 2 menus.
    a_paths = []
    for i in range(2):
        p = menu_queue.menu_path("plugin-a", f"slug-a-{i}", sid="session-A")
        p.write_text(f"session-A menu {i}\n", encoding="utf-8")
        a_paths.append(p)

    # Session B: 3 menus.
    b_paths = []
    for i in range(3):
        p = menu_queue.menu_path("plugin-b", f"slug-b-{i}", sid="session-B")
        p.write_text(f"session-B menu {i}\n", encoding="utf-8")
        b_paths.append(p)

    # ttl_seconds=0 disables eviction so the assertion is purely about
    # which session each file belongs to.
    a_listed = menu_queue.list_pending_menus(sid="session-A", ttl_seconds=0)
    b_listed = menu_queue.list_pending_menus(sid="session-B", ttl_seconds=0)

    # Session A sees its own 2 menus and nothing from B.
    assert len(a_listed) == 2
    assert set(a_listed) == set(a_paths)
    for bp in b_paths:
        assert bp not in a_listed

    # Session B sees its own 3 menus and nothing from A.
    assert len(b_listed) == 3
    assert set(b_listed) == set(b_paths)
    for ap in a_paths:
        assert ap not in b_listed


# ---------------------------------------------------------------------------
# 2. emit only consumes its own session's menus
# ---------------------------------------------------------------------------


def test_emit_event_for_session_a_does_not_touch_session_b_queue(capsys):
    """_handle_emit_event for session-A consumes session-A's menus; session-B queue untouched."""
    # Pre-stage session-A with 2 menus.
    a_paths = []
    for i in range(2):
        p = menu_queue.menu_path("plugin-a", f"slug-a-{i}", sid="session-A")
        p.write_text(f"A menu body {i}", encoding="utf-8")
        a_paths.append(p)

    # Pre-stage session-B with 3 menus.
    b_paths = []
    for i in range(3):
        p = menu_queue.menu_path("plugin-b", f"slug-b-{i}", sid="session-B")
        p.write_text(f"B menu body {i}", encoding="utf-8")
        b_paths.append(p)

    # Fire the emit handler with session-A's session_id in the payload.
    # _handle_emit_event takes the payload dict directly (no stdin involved).
    rc = menu_emit._handle_emit_event({"hook_event_name": "Stop", "session_id": "session-A"})
    assert rc == 0

    # Verify the printed systemMessage payload contains A bodies and NO B bodies.
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert "systemMessage" in out
    sm = out["systemMessage"]
    assert "A menu body 0" in sm
    assert "A menu body 1" in sm
    assert "B menu body 0" not in sm
    assert "B menu body 1" not in sm
    assert "B menu body 2" not in sm

    # Session-A queue is now empty (all consumed + deleted by emit).
    for ap in a_paths:
        assert not ap.exists(), f"session-A menu {ap.name} should have been removed"
    a_remaining = menu_queue.list_pending_menus(sid="session-A", ttl_seconds=0)
    assert a_remaining == []

    # Session-B queue is COMPLETELY untouched — all 3 menus still there.
    for bp in b_paths:
        assert bp.exists(), f"session-B menu {bp.name} should NOT have been touched"
    b_remaining = menu_queue.list_pending_menus(sid="session-B", ttl_seconds=0)
    assert set(b_remaining) == set(b_paths)
    assert len(b_remaining) == 3


# ---------------------------------------------------------------------------
# 3. session_dir paths are distinct directories under the queue root
# ---------------------------------------------------------------------------


def test_session_dirs_are_separate_directories_under_queue_root():
    """session_dir('session-A') and session_dir('session-B') are distinct dirs under queue_root."""
    sd_a = menu_queue.session_dir(sid="session-A")
    sd_b = menu_queue.session_dir(sid="session-B")

    # Distinct paths — different names, different directories on disk.
    assert sd_a != sd_b
    assert sd_a.name == "session-A"
    assert sd_b.name == "session-B"

    # Both materially exist as separate directories.
    assert sd_a.exists() and sd_a.is_dir()
    assert sd_b.exists() and sd_b.is_dir()

    # Both live directly under the shared queue root (single shared TMPDIR).
    root = menu_queue.queue_root()
    assert sd_a.parent == root
    assert sd_b.parent == root

    # And the parent dir is the same — sessions share the queue root, not the dir.
    assert sd_a.parent == sd_b.parent


# ---------------------------------------------------------------------------
# 4. flock is per-session — A's lock does not block B's lock acquisition
# ---------------------------------------------------------------------------


def test_session_lock_is_per_session_a_blocks_a_but_not_b():
    """session_lock('session-A') blocks a non-blocking same-sid attempt but does NOT block session-B."""
    sd_a = menu_queue.session_dir(sid="session-A")
    sd_b = menu_queue.session_dir(sid="session-B")
    lock_a = sd_a / menu_queue.LOCK_FILE_NAME
    lock_b = sd_b / menu_queue.LOCK_FILE_NAME

    with menu_queue.session_lock(sid="session-A"):
        # Session-A's lockfile must exist while we hold the lock.
        assert lock_a.exists()

        # A non-blocking attempt on the SAME (session-A) lock file from
        # a separate fd must fail with BlockingIOError — fcntl.flock
        # holds per-open-file-description locks, so a second open() of
        # the same path gives us a competing candidate that clashes
        # with the held LOCK_EX.
        with open(lock_a, "a") as competing_a:
            with pytest.raises(BlockingIOError):
                fcntl.flock(
                    competing_a.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )

        # A non-blocking attempt on session-B's lock file MUST succeed
        # immediately — the two sessions hold independent flocks on
        # independent lock files. If this raised, the queue layout
        # would have leaked one session's lock state into the other.
        with open(lock_b, "a") as competing_b:
            fcntl.flock(competing_b.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Release immediately so the test cleanup is clean.
            fcntl.flock(competing_b.fileno(), fcntl.LOCK_UN)

    # After session-A's context exits, the same-sid non-blocking attempt now succeeds.
    with open(lock_a, "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# 5. cleanup_empty_session_dir is per-session — removes A but spares B
# ---------------------------------------------------------------------------


def test_cleanup_empty_session_dir_removes_only_target_session():
    """cleanup_empty_session_dir('session-A') removes the empty session-A dir but session-B (with content) stays intact."""
    # Stage session-A with one menu, then remove it so the dir is empty.
    a_menu = menu_queue.menu_path("plugin-a", "slug-a", sid="session-A")
    a_menu.write_text("A body", encoding="utf-8")
    # Materialise session-A's lock file so cleanup also sweeps it.
    with menu_queue.session_lock(sid="session-A"):
        pass
    menu_queue.remove_menu(a_menu)
    sd_a = menu_queue.session_dir(sid="session-A")
    lock_a = sd_a / menu_queue.LOCK_FILE_NAME
    assert lock_a.exists()
    # Sanity: session-A has no menu/actions files now, only the lock.
    assert not any(sd_a.glob(f"*{menu_queue.MENU_SUFFIX}"))
    assert not any(sd_a.glob(f"*{menu_queue.ACTIONS_SUFFIX}"))

    # Stage session-B with two menus (non-empty — must survive cleanup of A).
    b_paths = []
    for i in range(2):
        bp = menu_queue.menu_path("plugin-b", f"slug-b-{i}", sid="session-B")
        bp.write_text(f"B body {i}", encoding="utf-8")
        b_paths.append(bp)
    sd_b = menu_queue.session_dir(sid="session-B")
    assert sd_b.exists()

    # Sweep ONLY session-A.
    menu_queue.cleanup_empty_session_dir(sid="session-A")

    # Session-A dir + lock are gone.
    assert not lock_a.exists()
    assert not sd_a.exists()

    # Session-B dir and ALL its menus are completely untouched.
    assert sd_b.exists() and sd_b.is_dir()
    for bp in b_paths:
        assert bp.exists()
    b_listed = menu_queue.list_pending_menus(sid="session-B", ttl_seconds=0)
    assert set(b_listed) == set(b_paths)
    assert len(b_listed) == 2


# ---------------------------------------------------------------------------
# 6. session_id sanitization prevents path-traversal escape from queue root
# ---------------------------------------------------------------------------


def test_session_id_with_path_traversal_does_not_escape_queue_root():
    """session_id(override='../evil') is sanitized so session_dir() stays inside queue_root()."""
    # _sanitize collapses non-alnum runs (including '/' and '.') to '-' and
    # strips leading/trailing dots+dashes; "../evil" must therefore reduce
    # to a safe filesystem name with NO path separators.
    sid = menu_queue.session_id(override="../evil")
    # Must not contain a path separator after sanitization — that's the
    # primary attack surface this test is defending.
    assert "/" not in sid
    assert "\\" not in sid
    # Must not contain ".." either — leading dots are stripped.
    assert ".." not in sid
    # The expected sanitized form: "../evil" → "evil" (leading "../"
    # collapses to "-", which is then stripped from the left edge).
    assert sid == "evil"

    # session_dir using the sanitized id must resolve inside queue_root.
    result = menu_queue.session_dir(sid=sid)
    root = menu_queue.queue_root()

    # The dir was created — required so .resolve() returns the real path
    # on macOS (/var → /private/var symlink) — and lives under the root.
    assert result.exists() and result.is_dir()
    resolved = result.resolve()
    resolved_root = root.resolve()
    assert resolved_root in resolved.parents, (
        f"session_dir escaped queue_root: dir={resolved} root={resolved_root}"
    )
    # Stricter: the dir is a DIRECT child of queue_root, not nested deeper.
    assert resolved.parent == resolved_root

    # And calling session_dir directly with the malicious override (which
    # internally re-sanitizes via session_id) must produce the same safe
    # result — no escape through that code path either.
    result2 = menu_queue.session_dir(sid=menu_queue.session_id(override="../evil"))
    assert result2.resolve() == resolved
    assert resolved_root in result2.resolve().parents
