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
import time
from pathlib import Path
from typing import Any

# Ensure sibling modules resolve when this script runs from any cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from menu_ansi import should_use_color, strip_ansi
from menu_queue import (
    cleanup_empty_session_dir,
    list_pending_menus,
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

# Debug log path — written when CLAUDE_MENU_DEBUG=1.
DEBUG_LOG = Path("/tmp/claude-menu-system-debug.log")


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
    bottom border + footer text). Drop body rows from the middle and
    inject a ``[N rows truncated]`` indicator line.

    Falls back to ``line_safe_truncate`` if we can't preserve shape.
    """
    if len(text) <= budget:
        return text
    lines = text.split("\n")
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
    kept_body = body[:]
    dropped = 0
    while kept_body:
        indicator = f"  …[{dropped + len(body) - len(kept_body) + 1} rows truncated]"
        candidate = "\n".join(header + kept_body + [indicator] + footer)
        if len(candidate) <= budget:
            return candidate
        kept_body.pop()
        dropped += 1
    # Body became empty — last resort.
    return line_safe_truncate(text, budget)


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
    for _, t in new:
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


if __name__ == "__main__":
    sys.exit(main(sys.argv))
