#!/usr/bin/env python3
"""Hook script — emits queued menus as systemMessage at Stop/StopFailure.

Wired by ``hooks/hooks.json`` to:
  - ``SubagentStop``: log only (systemMessage from subagent-lifecycle
    hooks routes to AI context, NOT the user terminal — confirmed
    empirically and documented at token-reporter line 3811).
  - ``Stop`` / ``StopFailure``: scan queue, concat in timestamp order,
    apply 10K cap with tiered truncation, emit as systemMessage JSON,
    delete emitted files.

10K-char hook output cap strategy (ported from token-reporter):
  - **SMALL** menus (≤1000 chars) → emit whole, untouched.
  - **BIG** menus (>1000 chars) → drop body rows from the bottom,
    keep header + footer + ``[N rows truncated — see <path>]`` indicator.
  - **OVERFLOW** (>10 queued menus OR combined >9500 chars) → title-only
    stubs for older menus, full render for the newest 2.

Line-safe truncation: cuts only at newline boundaries, never mid-ANSI
escape (preserves color state).

Color stripping: at hook-fire time, ``should_use_color()`` is consulted
against the live env. If the user has ``NO_COLOR=1`` set or TERM=dumb,
ANSI codes embedded in the queued file are stripped before emission.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Ensure sibling modules resolve when this script runs from any cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from menu_ansi import should_use_color, strip_ansi
from menu_queue import (
    cleanup_empty_session_dir,
    list_pending_menus,
    meta_path_for,
    remove_menu,
    session_id,
    session_lock,
)
from menu_widthwrap import line_safe_truncate

# 10K hook-output cap with budget for the JSON wrapper overhead.
# Claude Code documents the cap at 10,000 chars — reserve 500 for
# the surrounding `{"systemMessage": "..."}` and any escape expansion.
TOTAL_BUDGET = 9500
SMALL_MENU_THRESHOLD = 1000
MAX_FULL_MENUS_ON_OVERFLOW = 2  # newest 2 stay full, rest go to title stubs

# Debug log path — written when CLAUDE_MENU_DEBUG=1. Use the platform's
# tempdir (honors TMPDIR / TEMP / TMP per stdlib precedence) rather than
# a hardcoded "/tmp" — fixes bandit B108 (hardcoded_tmp_directory) and
# makes the log discoverable on Windows / sandboxed runners where /tmp
# is not the system tempdir.
DEBUG_LOG = Path(tempfile.gettempdir()) / "claude-menu-system-debug.log"


def _log_debug(message: str) -> None:
    """Append a timestamped message to the debug log if debug enabled."""
    if os.environ.get("CLAUDE_MENU_DEBUG") != "1":
        return
    try:
        with DEBUG_LOG.open("a") as fh:
            ts = time.strftime("%Y%m%d_%H%M%S%z")
            fh.write(f"[{ts}] {message}\n")
    except OSError:
        pass


def _extract_title(text: str) -> str:
    """Pull the first non-border line of a menu as its title stub."""
    for line in strip_ansi(text).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip pure box-drawing lines (heuristic: starts with a corner / wall).
        if stripped and stripped[0] in "┏┓┗┛╭╮╰╯╔╗╚╝┌┐└┘+│┃║|═━─":
            continue
        return stripped
    return "(menu)"


def _truncate_big_menu(text: str, budget: int) -> str:
    """Truncate a too-large menu to ``budget`` chars, keeping shape.

    Strategy: keep the header (first few lines including the top border
    and column headers) and the footer (last few lines including the
    bottom border + footer text). Drop body rows from the bottom and
    inject a ``[N rows truncated]`` indicator line whose count is the
    number of rows ACTUALLY removed.

    Count-accuracy contract: the integer in the indicator is exactly
    ``len(body) - len(kept_body)`` at the moment the candidate is
    accepted — no off-by-one, no double-counting. A pre-v0.1.6 bug
    used ``dropped + len(body) - len(kept_body) + 1`` which is
    ``2 * dropped + 1`` (the loop body incremented ``dropped`` and
    popped simultaneously, so the two terms were the same quantity
    counted twice, plus the leading ``+1``). Tests pin this.

    Trailing-newline normalisation: callers conventionally finish menu
    text with a trailing ``\\n``; ``str.split("\\n")`` on such input
    yields an empty-string sentinel at the end. Before the v0.1.6 fix
    that empty string was treated as a footer line, so ``footer_count=2``
    captured ``["footer_text", ""]`` and the actual bottom border drifted
    into the body slice. The border then got counted as a "row" that
    could be dropped — inflating the indicator by 1 per call. We now
    strip the trailing-newline sentinel before slicing so the header /
    body / footer boundaries always land where the input shape implies.

    Falls back to ``line_safe_truncate`` if we can't preserve shape.
    """
    if len(text) <= budget:
        return text
    lines = text.split("\n")
    # Strip the trailing empty-string sentinel that a final \n produces
    # so it isn't mistakenly treated as a footer line. See docstring.
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if len(lines) < 6:
        # Too short to bisect meaningfully — line-safe truncate.
        return line_safe_truncate(text, budget)
    # Find the top border (likely line 0 or 1) and the bottom border (last).
    # Keep header_count + footer_count lines, drop the middle.
    header_count = 4  # title + top border + header row + sep
    footer_count = 2  # bottom border + footer text
    header = lines[:header_count]
    footer = lines[-footer_count:]
    body = lines[header_count:-footer_count]
    if not body:
        return line_safe_truncate(text, budget)
    # Iteratively drop body rows from the bottom until size fits.
    # Drop AT LEAST ONE row before the first size check — without an
    # actual drop the indicator would claim "N rows truncated" with
    # N=0, which is a lie. If header+footer+indicator alone exceeds
    # the budget, fall through to line_safe_truncate.
    kept_body = body[:]
    while kept_body:
        # M3 (count accuracy): pop one body row, THEN compute the indicator as
        # exactly ``len(body) - len(kept_body)`` — the number of rows actually
        # removed. Popping before the size check guarantees at least one drop,
        # so the indicator never claims "0 rows truncated". The old (pre-v0.1.6)
        # formula ``dropped + (len(body) - len(kept_body)) + 1`` double-counted
        # the same difference and added a spurious +1 — a 7-row drop printed
        # "15 rows truncated". This keeps the corrected ``len(body) - len(kept_body)``
        # form; ``dropped`` is just a readable alias for it, never an extra counter.
        kept_body.pop()
        dropped = len(body) - len(kept_body)
        indicator = f"  …[{dropped} rows truncated]"
        candidate = "\n".join(header + kept_body + [indicator] + footer)
        if len(candidate) <= budget:
            return candidate
    # Body became empty — last resort.
    return line_safe_truncate(text, budget)


# Sentinel returned by _read_truncate_at when the spec said
# "disable truncation entirely for this menu" (sidecar present with
# ``truncate_at: null``). Distinguishes from "sidecar absent / field
# not set" (which returns Python's None and means "default behavior").
TRUNCATE_DISABLED: object = object()


def _read_truncate_at(menu_file: Path) -> int | None | object:
    """Return the per-menu ``truncate_at`` from the .meta.json sidecar.

    Three return values, three meanings:
      - ``int > 0`` — caller-supplied per-menu cap; emit uses this in
        place of the default ``per_new`` slice for this single menu.
      - ``TRUNCATE_DISABLED`` (sentinel) — caller set
        ``"truncate_at": null`` explicitly; emit must NOT do any
        per-menu shaping (the final 9500-char safety net still applies,
        but body-row trimming is skipped so overflow fails loudly).
      - ``None`` — sidecar absent OR malformed; emit falls back to the
        default heuristic shaping (current pre-truncate-at behaviour).
    """
    meta_file = meta_path_for(menu_file)
    if not meta_file.is_file():
        return None
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log_debug(f"meta read failed for {meta_file}: {exc}")
        return None
    if not isinstance(meta, dict) or "truncate_at" not in meta:
        return None
    raw = meta["truncate_at"]
    if raw is None:
        return TRUNCATE_DISABLED
    # Validation already happened at write time (menu_spec). Defensive
    # re-check here covers hand-edited sidecars / queue corruption —
    # garbage values fall back to default heuristic shaping.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return None
    return raw


def _compose_payload(menu_files: list[Path]) -> tuple[str, list[Path]]:
    """Build the systemMessage payload from queued menu files.

    Returns ``(payload_text, files_to_delete)``. ``files_to_delete``
    is the subset of ``menu_files`` whose content was successfully
    incorporated (everything we emit; we always emit everything,
    truncating as needed).
    """
    if not menu_files:
        return "", []

    contents: list[tuple[Path, str]] = []
    for f in menu_files:
        try:
            contents.append((f, f.read_text(encoding="utf-8")))
        except OSError as exc:
            _log_debug(f"read failed for {f}: {exc}")

    if not contents:
        return "", []

    # Strip ANSI codes if terminal doesn't want color RIGHT NOW (env
    # may have changed between render-time and emit-time).
    if not should_use_color():
        contents = [(p, strip_ansi(t)) for p, t in contents]

    # Compute total size.
    total = sum(len(t) for _, t in contents) + (2 * (len(contents) - 1))  # joiners
    if total <= TOTAL_BUDGET and len(contents) <= 10:
        # No overflow — just concat in order with blank lines between.
        body = "\n\n".join(t for _, t in contents)
        return body, [p for p, _ in contents]

    # Overflow path: keep the newest MAX_FULL_MENUS_ON_OVERFLOW full,
    # convert older to title stubs.
    old = contents[:-MAX_FULL_MENUS_ON_OVERFLOW]
    new = contents[-MAX_FULL_MENUS_ON_OVERFLOW:]
    stubs = [f"• {_extract_title(t)}  (truncated)" for _, t in old]
    stub_block = "Older menus (truncated):\n" + "\n".join(stubs) if stubs else ""

    # Allocate remaining budget to the new menus, evenly.
    remaining = TOTAL_BUDGET - len(stub_block) - 2
    if remaining <= 0:
        # Stubs alone overflow; line-safe-truncate the stub block.
        return line_safe_truncate(stub_block, TOTAL_BUDGET), [p for p, _ in contents]

    per_new = remaining // max(1, len(new))
    rendered_new: list[str] = []
    for path, t in new:
        override = _read_truncate_at(path)
        if override is TRUNCATE_DISABLED:
            # Spec explicitly disabled per-menu truncation — pass through
            # unchanged. The composed payload's final safety net (the
            # ``line_safe_truncate`` step below) still enforces the
            # 9500-char queue cap, but no body-row shaping happens here.
            rendered_new.append(t)
        elif isinstance(override, int):
            # Explicit per-menu cap — always honor it (override the
            # SMALL/per_new heuristic). The user asked for a specific
            # ceiling on this one menu's contribution.
            if len(t) <= override:
                rendered_new.append(t)
            else:
                rendered_new.append(_truncate_big_menu(t, override))
        else:
            # No per-menu override (sidecar absent / malformed) — default
            # heuristic shaping: pass through if small or already fits,
            # otherwise shape to per_new.
            if len(t) <= SMALL_MENU_THRESHOLD or len(t) <= per_new:
                rendered_new.append(t)
            else:
                rendered_new.append(_truncate_big_menu(t, per_new))
    body_parts = []
    if stub_block:
        body_parts.append(stub_block)
    body_parts.extend(rendered_new)
    payload = "\n\n".join(body_parts)
    # Final safety net.
    if len(payload) > TOTAL_BUDGET:
        payload = line_safe_truncate(payload, TOTAL_BUDGET)
    return payload, [p for p, _ in contents]


def _read_hook_payload() -> dict[str, Any]:
    """Read + parse the hook event JSON from stdin."""
    try:
        raw = sys.stdin.read()
    except OSError:
        return {}
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        _log_debug(f"hook payload parse failed: {exc}")
        return {}


def _handle_emit_event(payload: dict[str, Any]) -> int:
    """Stop / StopFailure path: scan queue, emit, delete."""
    sid_override = payload.get("session_id") or None
    sid = session_id(override=sid_override) if sid_override else session_id()
    _log_debug(f"emit handler — session_id={sid}")
    with session_lock(sid):
        menus = list_pending_menus(sid)
        _log_debug(f"emit handler — {len(menus)} pending menus")
        if not menus:
            cleanup_empty_session_dir(sid)
            return 0
        payload_text, files_to_delete = _compose_payload(menus)
        if not payload_text:
            cleanup_empty_session_dir(sid)
            return 0
        # Prepend a newline so the menu starts on its own line in the
        # terminal. Without this, Claude Code's TUI displays the box on
        # the same row as its "Stop says:" prefix, shifting the first
        # row right and breaking the box's alignment for the reader.
        out = json.dumps({"systemMessage": "\n" + payload_text})
        print(out)
        for f in files_to_delete:
            remove_menu(f)
        cleanup_empty_session_dir(sid)
    return 0


def main(argv: list[str] | None = None) -> int:
    _ = argv  # unused — hook scripts read stdin, not argv
    payload = _read_hook_payload()
    event = payload.get("hook_event_name", "")
    _log_debug(f"hook fired — event={event!r}")
    if event in ("Stop", "StopFailure"):
        return _handle_emit_event(payload)
    # SubagentStop / any other event: do nothing. The menu file was
    # written by the fork as a side effect and stays in the queue
    # until the main session's Stop fires.
    return 0


def _cli_entry() -> None:
    """CLI entry point — wraps main() so the sys.exit call lives inside
    a function body, not at module scope.

    Why: CPV's validate_hook.py flags module-scope sys.exit() because it
    would kill the hook process at import time. The `if __name__ == "__main__":`
    guard means the call only fires on direct invocation, never on import —
    but the AST detector cannot tell the difference (it treats every If
    block at module scope as import-time-reachable). Moving sys.exit into
    a function body satisfies the rule without changing behaviour.
    """
    sys.exit(main(sys.argv))


if __name__ == "__main__":
    _cli_entry()
