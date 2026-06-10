#!/usr/bin/env python3
"""Public API for queuing a menu — invoked directly via Bash.

A caller (agent, command, or test) invokes this script with one
argument — the path to the JSON spec file (the former render-menu skill
was removed in v0.1.3; Bash-invoking this script is now the only path).
The script:

  1. Reads + parses the JSON spec
  2. Validates the spec via ``menu_spec.validate``
  3. Renders via ``menu_render.render`` (always with use_color=True; the
     emit hook strips codes if the env says to)
  4. Writes the rendered text to a fresh queue file via ``menu_queue.menu_path``
  5. Writes the action map (if any) to a sibling ``.actions.json``
  6. Prints the queue file path to stdout (one line)

Exit codes:
  0 — success, queue path printed
  2 — invalid JSON / unreadable spec file
  3 — spec validation failure
  4 — write failure

Why a script and not inline LLM work: an LLM composing the queue file
by hand leaves ambiguity about exactly what gets written. A deterministic
Python script removes that — the caller just runs
``python3 menu_write.py <spec>`` and reports the printed queue path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Ensure sibling modules resolve when this script runs from any cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from menu_queue import actions_path_for, menu_path, write_atomic
from menu_render import render
from menu_spec import SpecError, validate


def write_menu(spec_path_or_json: str) -> Path:
    """Render + queue a menu from a JSON spec file or inline JSON string.

    Returns the queue file path (Path).

    The argument is auto-detected:
      - If it's a path to an existing file, read the file.
      - If it parses as JSON, use it directly.
      - Otherwise, raise FileNotFoundError.
    """
    # Try as file path first.
    p = Path(spec_path_or_json)
    if p.is_file():
        raw = p.read_text(encoding="utf-8")
    else:
        # Try inline JSON.
        raw = spec_path_or_json
    try:
        spec_raw = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit((f"invalid JSON: {exc}", 2)) from exc
    try:
        spec = validate(spec_raw)
    except SpecError as exc:
        raise SystemExit((f"spec error: {exc}", 3)) from exc

    # Always render with color codes embedded. The emit hook strips
    # them at emit-time if the actual terminal env doesn't want color.
    # This keeps the render-time decision reversible.
    try:
        rendered, action_map = render(spec, use_color=True)
    except (ValueError, KeyError) as exc:
        raise SystemExit((f"render failure: {exc}", 3)) from exc

    target = menu_path(spec["plugin"], spec["slug"])
    try:
        write_atomic(target, rendered)
    except OSError as exc:
        raise SystemExit((f"queue write failed: {exc}", 4)) from exc

    if action_map is not None:
        try:
            write_atomic(actions_path_for(target), json.dumps(action_map, indent=2))
        except OSError as exc:
            raise SystemExit((f"actions write failed: {exc}", 4)) from exc

    return target


_USAGE = (
    "usage: menu_write.py <spec-path-or-json>\n"
    "\n"
    "Render + queue a menu from a JSON spec (a file path or inline JSON).\n"
    "Prints the queue file path on success; writes a sibling .actions.json\n"
    "mapping rendered key -> action_id.\n"
    "\n"
    "exit codes: 0 ok | 2 invalid-JSON/unreadable | 3 spec/render failure | 4 write failure"
)


def _cli(argv: list[str]) -> int:
    if len(argv) < 2:
        print(_USAGE, file=sys.stderr)
        return 2
    # H1: recognize the help flags so ``--help`` prints usage cleanly instead
    # of being mis-read as a spec path (which printed "invalid JSON" + exit 2).
    if argv[1] in ("-h", "--help"):
        print(_USAGE)
        return 0
    try:
        path = write_menu(argv[1])
    except SystemExit as exc:
        if isinstance(exc.code, tuple) and len(exc.code) == 2:
            msg, code = exc.code
            print(f"menu_write: {msg}", file=sys.stderr)
            return int(code)
        raise
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
