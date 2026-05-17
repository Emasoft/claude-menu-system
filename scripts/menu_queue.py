"""Queue path resolution + session ID lookup + concurrent-write safety.

The queue is one directory per session, holding one file per queued menu:

    ${TMPDIR}/claude-menu-system/<session_id>/<timestamp_ns>-<plugin>-<slug>.menu.md

with a sibling ``.actions.json`` for menu/confirm modes that need to
route user replies.

Session ID resolution order:

1. ``CLAUDE_SESSION_ID`` env var — set by Claude Code in hook +
   skill-fork process environments.
2. Most-recent ``.jsonl`` file under the current project's
   ``~/.claude/projects/<encoded-cwd>/`` directory — when env is not
   set (rare but possible in test harnesses).
3. ``"unknown"`` bucket — last-resort fallback so the script never
   crashes. The emit hook will still pick up menus from this bucket
   but multi-session isolation is lost.

Concurrent-write safety: each menu write is atomic via tempfile +
``os.replace``. The hook's emit step uses ``flock`` on the session
dir to serialise concurrent Stop hooks (rare but possible if the user
has multiple plugins all wired to emit).
"""

from __future__ import annotations

import fcntl
import os
import re
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

QUEUE_ROOT_NAME = "claude-menu-system"
MENU_SUFFIX = ".menu.md"
ACTIONS_SUFFIX = ".actions.json"
LOCK_FILE_NAME = ".queue.lock"

_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize(name: str, max_len: int = 60) -> str:
    """Make ``name`` safe for use as a filename component."""
    out = _SANITIZE_RE.sub("-", name).strip("-.")
    if not out:
        out = "unknown"
    return out[:max_len]


def queue_root() -> Path:
    """Return the top-level queue dir. Created on demand."""
    base = Path(tempfile.gettempdir()) / QUEUE_ROOT_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def _resolve_from_transcripts() -> str | None:
    """Last-resort: find the newest session transcript under ~/.claude/projects/."""
    home = Path.home() / ".claude" / "projects"
    if not home.is_dir():
        return None
    # Claude Code encodes the project cwd by replacing path separators with hyphens.
    # We don't strictly need to find the right project dir — just the newest .jsonl
    # globally is good enough for the fallback.
    candidates = []
    for proj_dir in home.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                candidates.append((jsonl.stat().st_mtime, jsonl.stem))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def session_id(*, override: str | None = None) -> str:
    """Resolve the current Claude Code session ID.

    Order:
    1. ``override`` argument (tests use this).
    2. ``CLAUDE_SESSION_ID`` env var.
    3. ``CLAUDE_SESSION_ID_HOOK`` env var (alternative spelling
       some Claude Code versions use).
    4. Most-recent transcript under ``~/.claude/projects/``.
    5. ``"unknown"`` fallback.
    """
    if override is not None:
        return _sanitize(override)
    for key in ("CLAUDE_SESSION_ID", "CLAUDE_SESSION_ID_HOOK"):
        val = os.environ.get(key, "").strip()
        if val:
            return _sanitize(val)
    fallback = _resolve_from_transcripts()
    if fallback:
        return _sanitize(fallback)
    return "unknown"


def session_dir(sid: str | None = None) -> Path:
    """Path to this session's queue dir. Created on demand."""
    sid = sid or session_id()
    d = queue_root() / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def menu_path(plugin: str, slug: str, sid: str | None = None) -> Path:
    """Allocate a fresh menu file path for the given plugin/slug.

    Filename: ``<timestamp_ns>-<plugin>-<slug>.menu.md``. The nanosecond
    timestamp guarantees total order across concurrent writers.
    """
    sd = session_dir(sid)
    ts = time.time_ns()
    name = f"{ts:020d}-{_sanitize(plugin, 32)}-{_sanitize(slug, 32)}{MENU_SUFFIX}"
    return sd / name


def actions_path_for(menu_file: Path) -> Path:
    """Sibling .actions.json path for a given menu file."""
    return menu_file.with_suffix("").with_suffix(ACTIONS_SUFFIX)


def write_atomic(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically (tempfile + replace).

    Avoids partial reads if another process is scanning the queue dir
    while we're writing.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".tmp.{os.getpid()}.{time.time_ns()}.{target.name}"
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


@contextmanager
def session_lock(sid: str | None = None) -> Iterator[None]:
    """Acquire an exclusive flock on the session dir for the duration.

    Used by the emit hook to ensure two concurrent Stop hooks don't
    race to read+delete the same files. Released on context exit.
    """
    sd = session_dir(sid)
    lockfile = sd / LOCK_FILE_NAME
    # Open in append mode so the file exists even on first run.
    with lockfile.open("a") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def list_pending_menus(sid: str | None = None, *, ttl_seconds: int | None = None) -> list[Path]:
    """Return queued menu files in ascending timestamp order.

    If ``ttl_seconds`` is set, files older than that are deleted
    silently and NOT returned. The default TTL is read from the
    ``CLAUDE_MENU_TTL_SEC`` env var (default 60).
    """
    if ttl_seconds is None:
        try:
            ttl_seconds = int(os.environ.get("CLAUDE_MENU_TTL_SEC", "60"))
        except ValueError:
            ttl_seconds = 60
    sd = session_dir(sid)
    now = time.time()
    files = []
    for path in sd.glob(f"*{MENU_SUFFIX}"):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if ttl_seconds > 0 and age > ttl_seconds:
            # Stale — silently drop the menu + its actions sidecar.
            path.unlink(missing_ok=True)
            actions_path_for(path).unlink(missing_ok=True)
            continue
        files.append(path)
    # Filename starts with nanosecond timestamp → ASCII sort = time order.
    files.sort()
    return files


def remove_menu(menu_file: Path) -> None:
    """Delete a menu file + its actions sidecar."""
    menu_file.unlink(missing_ok=True)
    actions_path_for(menu_file).unlink(missing_ok=True)


def cleanup_empty_session_dir(sid: str | None = None) -> None:
    """If the session dir has no menu/actions files, remove the lock + dir."""
    sd = session_dir(sid)
    has_content = any(sd.glob(f"*{MENU_SUFFIX}")) or any(sd.glob(f"*{ACTIONS_SUFFIX}"))
    if has_content:
        return
    # Drop the lock file too. We don't care if another process is mid-acquisition.
    lock = sd / LOCK_FILE_NAME
    lock.unlink(missing_ok=True)
    try:
        sd.rmdir()
    except OSError:
        # Other files exist (e.g. .tmp during a race) — leave the dir alone.
        pass
