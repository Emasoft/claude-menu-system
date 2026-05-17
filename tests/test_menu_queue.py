"""Unit tests for ``scripts/menu_queue.py``.

Covers queue path resolution, session id resolution (env / override /
transcript fallback / unknown bucket), atomic writes, the per-session
flock, TTL-based stale-menu eviction, and the per-session cleanup
sweep. The autouse fixture in ``conftest.py`` sets ``TMPDIR`` to
``tmp_path`` and ``CLAUDE_SESSION_ID`` to ``"test-session"`` for every
test, so the queue dir is isolated per-test and we can monkeypatch
those env vars on a per-test basis without polluting siblings.

Coverage target: >=95%. External dependencies (fcntl, os.replace) are
real — only ``Path.home`` is monkeypatched in tests that exercise the
transcript-fallback branch.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
import time
from pathlib import Path

import menu_queue
import pytest

# ---------------------------------------------------------------------------
# Per-test isolation of tempfile.gettempdir()
# ---------------------------------------------------------------------------
#
# The conftest autouse fixture sets ``TMPDIR=tmp_path`` so each test
# gets its own queue dir, but stdlib ``tempfile`` caches the result of
# ``gettempdir()`` in ``tempfile.tempdir`` on first call. Without
# busting that cache, every test would resolve back to whatever
# tempdir was first looked up and the per-test ``tmp_path`` isolation
# (and the conftest contract) would silently break — tests would
# pollute each other's queue dirs.
#
# We reset both ``tempfile.tempdir`` and any related cached state
# before AND after every test so:
#   (a) each test sees its own ``tmp_path`` as the tempdir,
#   (b) we leave no global state behind for unrelated test files.


@pytest.fixture(autouse=True)
def _reset_tempfile_cache():
    """Force tempfile.gettempdir() to re-read TMPDIR each test."""
    tempfile.tempdir = None
    yield
    tempfile.tempdir = None


# ---------------------------------------------------------------------------
# 1. queue_root
# ---------------------------------------------------------------------------


def test_queue_root_returns_path_under_tmpdir_and_exists(tmp_path):
    """queue_root() returns a path under TMPDIR that exists after the call."""
    root = menu_queue.queue_root()
    assert root.exists() and root.is_dir()
    # tmp_path is set as TMPDIR by the autouse fixture; on macOS tempfile
    # may canonicalise /var → /private/var, so compare via resolve().
    assert root.resolve().is_relative_to(tmp_path.resolve())
    assert root.name == menu_queue.QUEUE_ROOT_NAME


# ---------------------------------------------------------------------------
# 2-4. session_id resolution
# ---------------------------------------------------------------------------


def test_session_id_from_env_var_is_sanitized(monkeypatch):
    """session_id() returns the CLAUDE_SESSION_ID env value, sanitized."""
    # Set a value containing chars that _sanitize will rewrite to '-'.
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess/with spaces+stuff")
    assert menu_queue.session_id() == "sess-with-spaces-stuff"


def test_session_id_with_override_returns_sanitized_override(monkeypatch):
    """session_id(override=...) returns the sanitized override, ignoring env."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-value")
    # Override wins, gets sanitized (slashes -> dashes, leading/trailing strip).
    assert menu_queue.session_id(override="my/custom*id") == "my-custom-id"
    # Pure value (no special chars) passes through unchanged.
    assert menu_queue.session_id(override="plain-id") == "plain-id"


def test_session_id_returns_unknown_when_no_env_and_no_transcripts(monkeypatch, tmp_path):
    """session_id() returns 'unknown' when no env, no transcripts; transcript fallback also tested."""
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID_HOOK", raising=False)

    # --- Branch A: no ~/.claude/projects/ directory at all → 'unknown'.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert menu_queue.session_id() == "unknown"

    # --- Branch B: ~/.claude/projects/ exists but empty → 'unknown' (no candidates).
    fake_home = tmp_path / "home_b"
    (fake_home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    assert menu_queue.session_id() == "unknown"

    # --- Branch C: a project dir with a real .jsonl exists → fallback returns the stem.
    fake_home_c = tmp_path / "home_c"
    proj = fake_home_c / ".claude" / "projects" / "Some-project"
    proj.mkdir(parents=True)
    # A non-dir entry under projects/ exercises the `if not proj_dir.is_dir()` skip.
    (fake_home_c / ".claude" / "projects" / "stray.txt").write_text("ignore me")
    # Two transcripts so the sort-by-mtime branch picks the newer one.
    older = proj / "abc-OLDER-session.jsonl"
    older.write_text("{}\n")
    os.utime(older, (1000.0, 1000.0))
    newer = proj / "xyz-NEWER-session.jsonl"
    newer.write_text("{}\n")
    os.utime(newer, (2000.0, 2000.0))
    monkeypatch.setattr(Path, "home", lambda: fake_home_c)
    # session_id sanitizes the stem (here: pure alnum + dashes → unchanged).
    assert menu_queue.session_id() == "xyz-NEWER-session"


# ---------------------------------------------------------------------------
# 5. _sanitize
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # special chars (incl. spaces, slashes, punctuation) collapse to '-'
        ("hello world", "hello-world"),
        ("a/b\\c:d", "a-b-c-d"),
        # leading/trailing dashes and dots are stripped
        ("---abc---", "abc"),
        ("...abc...", "abc"),
        ("..-.-foo-.-..", "foo"),
        # empty / pure-noise input → 'unknown'
        ("", "unknown"),
        ("///", "unknown"),
        # alnum, dot, underscore, dash are preserved verbatim
        ("ok_name-v1.2", "ok_name-v1.2"),
    ],
)
def test_sanitize_handles_special_chars_strip_and_empty(raw, expected):
    """_sanitize replaces special chars with '-', strips edges, falls back to 'unknown'."""
    assert menu_queue._sanitize(raw) == expected


def test_sanitize_caps_at_max_len():
    """_sanitize caps output at max_len characters."""
    long = "a" * 200
    assert menu_queue._sanitize(long, max_len=10) == "a" * 10
    # Default max_len is 60.
    assert len(menu_queue._sanitize("b" * 500)) == 60


# ---------------------------------------------------------------------------
# 6. session_dir
# ---------------------------------------------------------------------------


def test_session_dir_creates_dir_on_demand():
    """session_dir() creates the per-session queue dir on demand."""
    sd = menu_queue.session_dir()
    assert sd.exists() and sd.is_dir()
    # Default sid comes from CLAUDE_SESSION_ID=test-session (autouse fixture).
    assert sd.name == "test-session"
    # Calling again with an explicit sid creates a different dir.
    sd2 = menu_queue.session_dir("other-sid")
    assert sd2.exists() and sd2.is_dir()
    assert sd2.name == "other-sid"
    assert sd2 != sd


# ---------------------------------------------------------------------------
# 7-8. menu_path
# ---------------------------------------------------------------------------


def test_menu_path_contains_timestamp_plugin_slug_and_suffix():
    """menu_path() produces a path containing timestamp, plugin, slug, ending with .menu.md."""
    p = menu_queue.menu_path("my-plugin", "do-thing")
    assert p.name.endswith(menu_queue.MENU_SUFFIX)
    assert "my-plugin" in p.name
    assert "do-thing" in p.name
    # The leading component is a 20-digit zero-padded nanosecond timestamp.
    ts_str = p.name.split("-", 1)[0]
    assert ts_str.isdigit() and len(ts_str) == 20
    # Parent is the session dir.
    assert p.parent == menu_queue.session_dir()


def test_menu_path_two_consecutive_calls_produce_different_paths():
    """menu_path() called twice produces distinct paths (distinct timestamps)."""
    p1 = menu_queue.menu_path("plugin", "slug")
    # Sleep just enough to guarantee a distinct nanosecond timestamp on any
    # platform where time_ns() resolution is coarse.
    time.sleep(0.001)
    p2 = menu_queue.menu_path("plugin", "slug")
    assert p1 != p2
    # Second call's filename sorts strictly after the first (ASCII == time order).
    assert p2.name > p1.name


# ---------------------------------------------------------------------------
# 9. actions_path_for
# ---------------------------------------------------------------------------


def test_actions_path_for_derives_sibling_actions_json():
    """actions_path_for() derives a sibling .actions.json from a .menu.md path."""
    menu_file = menu_queue.menu_path("plugin", "slug")
    actions = menu_queue.actions_path_for(menu_file)
    # Sibling — same parent dir.
    assert actions.parent == menu_file.parent
    # Suffix swapped from .menu.md to .actions.json.
    assert actions.suffix == ".json"
    assert actions.name.endswith(menu_queue.ACTIONS_SUFFIX)
    # The stem before the .menu.md / .actions.json suffixes is identical.
    menu_stem = menu_file.name[: -len(menu_queue.MENU_SUFFIX)]
    actions_stem = actions.name[: -len(menu_queue.ACTIONS_SUFFIX)]
    assert menu_stem == actions_stem


# ---------------------------------------------------------------------------
# 10-11. write_atomic
# ---------------------------------------------------------------------------


def test_write_atomic_writes_content_to_target():
    """write_atomic() writes content and the file appears at target path."""
    target = menu_queue.session_dir() / "out.txt"
    menu_queue.write_atomic(target, "hello world\n")
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "hello world\n"


def test_write_atomic_cleans_up_tmp_file_on_success(monkeypatch):
    """write_atomic() leaves no leftover .tmp.* on success and cleans up tmp on failure too."""
    parent = menu_queue.session_dir()
    target = parent / "atomic-out.txt"
    menu_queue.write_atomic(target, "payload")
    # Only the target survives — no `.tmp.<pid>.<ns>.<name>` debris.
    siblings = [p.name for p in parent.iterdir()]
    leftovers = [n for n in siblings if n.startswith(".tmp.")]
    assert leftovers == []
    assert target.name in siblings

    # --- Failure-path coverage: monkeypatch os.replace to raise so the
    # ``except: tmp.unlink(missing_ok=True); raise`` branch runs.
    fail_target = parent / "atomic-fail.txt"

    def boom(_src, _dst):
        raise RuntimeError("simulated replace failure")

    monkeypatch.setattr(menu_queue.os, "replace", boom)
    with pytest.raises(RuntimeError, match="simulated replace failure"):
        menu_queue.write_atomic(fail_target, "should not land")
    # Target was never created (atomicity).
    assert not fail_target.exists()
    # And the .tmp.* file was cleaned up by the except branch.
    leftovers_after_fail = [n for n in os.listdir(parent) if n.startswith(".tmp.")]
    assert leftovers_after_fail == []


# ---------------------------------------------------------------------------
# 12. session_lock
# ---------------------------------------------------------------------------


def test_session_lock_acquires_and_blocks_concurrent_non_blocking_attempt():
    """session_lock() acquires + releases; a nested LOCK_NB attempt fails (blocked)."""
    sd = menu_queue.session_dir()
    lockfile_path = sd / menu_queue.LOCK_FILE_NAME

    with menu_queue.session_lock():
        # Lock file must exist while we're inside the context.
        assert lockfile_path.exists()
        # A second non-blocking attempt on the SAME file from a separate
        # FD must fail — fcntl.flock locks are per-open-file-description,
        # so opening the file a second time gives us a distinct lock
        # candidate that should clash with the held LOCK_EX.
        with open(lockfile_path, "a") as competing_fh:
            with pytest.raises(BlockingIOError):
                fcntl.flock(
                    competing_fh.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )

    # After context exit, the lock is released → LOCK_NB now succeeds.
    with open(lockfile_path, "a") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# 13. list_pending_menus — ordering
# ---------------------------------------------------------------------------


def test_list_pending_menus_returns_files_in_ascending_timestamp_order():
    """list_pending_menus() returns queued menu files in ascending timestamp order."""
    # Write 3 menus with small delays so each gets a distinct ns timestamp.
    paths = []
    for i in range(3):
        p = menu_queue.menu_path("plugin", f"slug-{i}")
        menu_queue.write_atomic(p, f"menu {i}\n")
        paths.append(p)
        time.sleep(0.001)

    # Pass ttl_seconds=0 so the TTL branch is disabled (every file kept).
    listed = menu_queue.list_pending_menus(ttl_seconds=0)
    assert listed == sorted(paths)
    # Strict ordering: the first written must come first.
    assert listed[0] == paths[0]
    assert listed[-1] == paths[-1]


# ---------------------------------------------------------------------------
# 14. list_pending_menus — TTL eviction
# ---------------------------------------------------------------------------


def test_list_pending_menus_ttl_zero_keeps_all_short_ttl_evicts_stale(monkeypatch):
    """list_pending_menus(ttl_seconds=0) keeps all; short ttl deletes stale files; env TTL + bad TTL + stat OSError paths covered."""
    # Fresh menu — should always survive.
    fresh = menu_queue.menu_path("plugin", "fresh")
    menu_queue.write_atomic(fresh, "fresh")

    # Stale menu — backdate its mtime by 1 hour using os.utime so the TTL
    # branch in list_pending_menus considers it expired.
    time.sleep(0.001)
    stale = menu_queue.menu_path("plugin", "stale")
    menu_queue.write_atomic(stale, "stale")
    stale_actions = menu_queue.actions_path_for(stale)
    menu_queue.write_atomic(stale_actions, "{}")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    # ttl_seconds=0 -> the `ttl_seconds > 0` guard is False, every file kept.
    all_files = menu_queue.list_pending_menus(ttl_seconds=0)
    assert fresh in all_files
    assert stale in all_files
    # Stale file (still) on disk because ttl=0 disabled eviction.
    assert stale.exists()
    assert stale_actions.exists()

    # --- Default TTL is read from CLAUDE_MENU_TTL_SEC env (covers lines 178-182).
    # Set TTL=1 in env so the stale file is evicted when ttl_seconds is None.
    monkeypatch.setenv("CLAUDE_MENU_TTL_SEC", "1")
    after_env_ttl = menu_queue.list_pending_menus()  # ttl_seconds=None → reads env
    assert stale not in after_env_ttl
    assert fresh in after_env_ttl
    assert not stale.exists()
    assert not stale_actions.exists()

    # --- Bad env value (non-int) falls back to default 60 (covers ValueError except).
    monkeypatch.setenv("CLAUDE_MENU_TTL_SEC", "not-a-number")
    fresh2 = menu_queue.menu_path("plugin", "fresh2")
    menu_queue.write_atomic(fresh2, "fresh2")
    after_bad_env = menu_queue.list_pending_menus()
    assert fresh2 in after_bad_env  # fresh, ttl=60 (default), kept

    # --- ttl_seconds=1 again (re-create a stale file) to assert sidecar removal.
    monkeypatch.delenv("CLAUDE_MENU_TTL_SEC", raising=False)
    stale2 = menu_queue.menu_path("plugin", "stale2")
    menu_queue.write_atomic(stale2, "stale2")
    stale2_actions = menu_queue.actions_path_for(stale2)
    menu_queue.write_atomic(stale2_actions, "{}")
    os.utime(stale2, (old, old))
    after_ttl = menu_queue.list_pending_menus(ttl_seconds=1)
    assert stale2 not in after_ttl
    assert not stale2.exists()
    assert not stale2_actions.exists()

    # --- OSError from stat() (covers lines 188-190): replace Path.stat with one
    # that raises for a specific path; the loop's `except OSError: continue` swallows it.
    racy = menu_queue.menu_path("plugin", "racy")
    menu_queue.write_atomic(racy, "racy")
    real_stat = Path.stat

    def stat_that_breaks_for_racy(self, *args, **kwargs):
        if self == racy:
            raise OSError("simulated stat() race")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat_that_breaks_for_racy)
    listed = menu_queue.list_pending_menus(ttl_seconds=0)
    # The racy file is silently skipped (continue), not returned.
    assert racy not in listed


# ---------------------------------------------------------------------------
# 15. remove_menu + cleanup_empty_session_dir
# ---------------------------------------------------------------------------


def test_remove_menu_deletes_pair_and_cleanup_removes_empty_session_dir():
    """remove_menu() deletes both .menu.md + .actions.json; cleanup wipes empty dir + lock."""
    sd = menu_queue.session_dir()
    menu_file = menu_queue.menu_path("plugin", "to-be-removed")
    actions_file = menu_queue.actions_path_for(menu_file)
    menu_queue.write_atomic(menu_file, "menu body")
    menu_queue.write_atomic(actions_file, '{"a":1}')
    assert menu_file.exists() and actions_file.exists()

    # remove_menu deletes both siblings.
    menu_queue.remove_menu(menu_file)
    assert not menu_file.exists()
    assert not actions_file.exists()

    # Calling remove_menu again on a path that no longer exists is a no-op
    # (missing_ok=True under the hood); it must not raise.
    menu_queue.remove_menu(menu_file)

    # Acquire + release the lock to materialise the .queue.lock sidecar
    # — this is the state cleanup_empty_session_dir is supposed to sweep.
    with menu_queue.session_lock():
        pass
    lock_path = sd / menu_queue.LOCK_FILE_NAME
    assert lock_path.exists()

    # Session dir has no .menu.md or .actions.json now — cleanup wipes the
    # lock file AND the (now-empty) session dir itself.
    menu_queue.cleanup_empty_session_dir()
    assert not lock_path.exists()
    assert not sd.exists()

    # When called on a dir that still has menus, cleanup is a no-op: it
    # creates the dir again (session_dir is called inside), drops a menu,
    # then calls cleanup — the dir must remain because content is present.
    sd2 = menu_queue.session_dir()
    keeper = menu_queue.menu_path("plugin", "keeper")
    menu_queue.write_atomic(keeper, "x")
    menu_queue.cleanup_empty_session_dir()
    assert sd2.exists()
    assert keeper.exists()

    # --- rmdir() OSError swallow path (covers lines 221-223): no menus +
    # no actions sidecars (so has_content is False, cleanup proceeds to
    # rmdir), but a stray .tmp.* file is present → rmdir raises OSError
    # and the except branch leaves the dir alone.
    menu_queue.remove_menu(keeper)
    # Confirm no .menu.md / .actions.json files remain.
    assert not any(sd2.glob(f"*{menu_queue.MENU_SUFFIX}"))
    assert not any(sd2.glob(f"*{menu_queue.ACTIONS_SUFFIX}"))
    # Drop a stray .tmp.* file so rmdir fails with ENOTEMPTY.
    stray = sd2 / ".tmp.99999.0.stray"
    stray.write_text("stray")
    menu_queue.cleanup_empty_session_dir()
    # Dir survives because rmdir raised OSError (caught and swallowed).
    assert sd2.exists()
    # The stray file is untouched.
    assert stray.exists()
