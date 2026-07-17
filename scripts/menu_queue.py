#!/usr/bin/env python3
"""Queue path resolution + session ID lookup + concurrent-write safety.

The queue is one directory per session, holding one file per queued menu:

    ${TMPDIR}/claude-menu-system/<session_id>/<timestamp_ns>-<plugin>-<slug>.menu.md

with a sibling ``.actions.json`` for menu/confirm modes that need to
route user replies.

Session ID resolution order:

1. ``CLAUDE_CODE_SESSION_ID`` env var — the name Claude Code actually
   exports to command + hook subprocesses (verified on CC 2.1.212).
   ``CLAUDE_SESSION_ID`` / ``CLAUDE_SESSION_ID_HOOK`` are also accepted
   for forward/backward compat.
2. Most-recent ``.jsonl`` file under THIS project's
   ``~/.claude/projects/<encoded-cwd>/`` directory; if that dir has
   none, the most-recent ``.jsonl`` globally — a best-effort fallback
   for when env is not set (rare, mostly test harnesses).
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
# Sidecar for per-menu emit-time metadata (currently: truncate_at). The
# rendered text is text-only by design — anything the emit hook needs at
# truncation time but can't infer from the rendered bytes lives here.
META_SUFFIX = ".meta.json"
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
    """Last-resort session id from transcript files under ~/.claude/projects/.

    Prefer the newest ``.jsonl`` in THIS project's cwd folder: under concurrent
    sessions the globally-newest transcript often belongs to a DIFFERENT project's
    session, so cwd-scoping keeps the fallback at least in the right project. Only
    if the cwd folder has no transcript do we fall back to the globally-newest one.
    Now rarely reached (``CLAUDE_CODE_SESSION_ID`` resolves first) but must not
    silently pick a sibling session when it is.
    """
    home = Path.home() / ".claude" / "projects"
    if not home.is_dir():
        return None

    def _newest_stem(proj_dir: Path) -> tuple[float, str] | None:
        best: tuple[float, str] | None = None
        for jsonl in proj_dir.glob("*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, jsonl.stem)
        return best

    # 1. cwd-scoped: Claude Code names the per-project folder after the cwd with
    #    every non-alphanumeric char replaced by '-' (e.g. '/Users/me/Code/Foo'
    #    -> '-Users-me-Code-Foo'). A cwd-encoding miss just falls through to (2).
    cwd_dir = home / re.sub(r"[^A-Za-z0-9]", "-", os.getcwd())
    if cwd_dir.is_dir():
        hit = _newest_stem(cwd_dir)
        if hit:
            return hit[1]

    # 2. global-newest fallback (only when the cwd folder holds no transcript).
    best: tuple[float, str] | None = None
    for proj_dir in home.iterdir():
        if not proj_dir.is_dir():
            continue
        hit = _newest_stem(proj_dir)
        if hit and (best is None or hit[0] > best[0]):
            best = hit
    return best[1] if best else None


def session_id(*, override: str | None = None) -> str:
    """Resolve the current Claude Code session ID.

    Order:
    1. ``override`` argument (tests use this).
    2. ``CLAUDE_CODE_SESSION_ID`` env var — the name Claude Code actually
       exports to command + hook subprocesses (verified on CC 2.1.212). This
       is the primary, authoritative source.
    3. ``CLAUDE_SESSION_ID`` / ``CLAUDE_SESSION_ID_HOOK`` env vars — older /
       alternative spellings, kept for forward/backward compat.
    4. Most-recent transcript for THIS project's cwd (else globally) under
       ``~/.claude/projects/``.
    5. ``"unknown"`` fallback.

    WHY step 2 matters: without it none of the env names are ever set, so
    resolution always fell to the transcript heuristic — which returns the
    globally-newest ``.jsonl`` and is WRONG whenever a second Claude session is
    active (the menu is queued under a sibling session's id and the requesting
    session's emit hook never finds it, so nothing renders).
    ``CLAUDE_CODE_SESSION_ID`` is inherited by the queue-writer subprocess and
    matches the ``session_id`` the emit hook already reads from its Stop-hook
    payload, so both sides agree on one id.
    """
    if override is not None:
        return _sanitize(override)
    for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_SESSION_ID_HOOK"):
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


def meta_path_for(menu_file: Path) -> Path:
    """Sibling .meta.json path for a given menu file (per-menu emit metadata)."""
    return menu_file.with_suffix("").with_suffix(META_SUFFIX)


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
            # Stale — silently drop the menu + every sidecar.
            path.unlink(missing_ok=True)
            actions_path_for(path).unlink(missing_ok=True)
            meta_path_for(path).unlink(missing_ok=True)
            continue
        files.append(path)
    # Filename starts with nanosecond timestamp → ASCII sort = time order.
    files.sort()
    return files


def remove_menu(menu_file: Path) -> None:
    """Delete a menu file + every sidecar (.actions.json, .meta.json)."""
    menu_file.unlink(missing_ok=True)
    actions_path_for(menu_file).unlink(missing_ok=True)
    meta_path_for(menu_file).unlink(missing_ok=True)


def cleanup_empty_session_dir(sid: str | None = None) -> None:
    """If the session dir has no menu/actions/meta files, remove the lock + dir."""
    sd = session_dir(sid)
    has_content = (
        any(sd.glob(f"*{MENU_SUFFIX}"))
        or any(sd.glob(f"*{ACTIONS_SUFFIX}"))
        or any(sd.glob(f"*{META_SUFFIX}"))
    )
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
