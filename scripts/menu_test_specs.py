#!/usr/bin/env python3
"""Helper script — build canonical demo specs to a JSON file.

Used by the `/menu-test` command to produce a deterministic demo spec
on disk that ``menu_write.py`` (invoked directly via Bash) can consume.
Also re-used by the test suite + examples gallery.

Usage:
    python3 scripts/menu_test_specs.py <preset> <output-path>

Presets:
    first-contact       — 5-row first-contact menu
    severity-summary    — summary mode with all 5 tiers
    breakdown-matrix    — breakdown mode with 4 categories
    status-report       — status_table mode with 6 components
    panel-message       — single-panel mode
    multi-box-report    — 3-panel multi_box stack
    progress-bar        — progress mode at 60%
    confirm-prompt      — confirm mode (Yes/No/Cancel)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PRESETS: dict[str, dict[str, Any]] = {
    "first-contact": {
        "spec_version": 1,
        "mode": "menu",
        "plugin": "cms",
        "slug": "first-contact",
        "header": "claude-menu-system self-test — pick anything",
        "rows": [
            {"key": "1", "action_id": "demo-summary", "label": "See a severity summary"},
            {
                "key": "2",
                "action_id": "demo-breakdown",
                "label": "See a per-category breakdown matrix",
            },
            {"key": "3", "action_id": "demo-status", "label": "See a status table (✓/✗/⚠)"},
            {
                "key": "4",
                "action_id": "demo-panel",
                "label": "See a single panel with wrapped text",
            },
            {"key": "A", "action_id": "freeform", "label": "Free-form question"},
            {"key": "0", "action_id": "cancel", "label": "Cancel / Exit"},
        ],
        "footer": "Type a number to choose:",
    },
    "severity-summary": {
        "spec_version": 1,
        "mode": "summary",
        "plugin": "cms",
        "slug": "demo-summary",
        "title": "Demo validation summary",
        "counts": {"critical": 0, "major": 1, "minor": 3, "nit": 7, "warning": 2},
        "verdict": "VALID",
        "report_path": "/tmp/demo-report.md",
    },
    "breakdown-matrix": {
        "spec_version": 1,
        "mode": "breakdown",
        "plugin": "cms",
        "slug": "demo-breakdown",
        "title": "Demo findings breakdown",
        "row_header": "Source",
        "rows": [
            {
                "label": "Schema validation",
                "counts": {"CRITICAL": 0, "MAJOR": 0, "MINOR": 1, "NIT": 0, "WARNING": 0},
            },
            {
                "label": "Security scan",
                "counts": {"CRITICAL": 0, "MAJOR": 1, "MINOR": 2, "NIT": 1, "WARNING": 1},
            },
            {
                "label": "Style check",
                "counts": {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0, "NIT": 6, "WARNING": 0},
            },
            {
                "label": "Cross-reference",
                "counts": {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0, "NIT": 0, "WARNING": 1},
            },
        ],
        "verdict": "VALID",
    },
    "status-report": {
        "spec_version": 1,
        "mode": "status_table",
        "plugin": "cms",
        "slug": "demo-status",
        "title": "Demo plugin build status",
        "row_header": "Component",
        "rows": [
            {"label": "plugin.json", "status": "ok", "notes": "valid manifest"},
            {"label": "hooks/hooks.json", "status": "ok", "notes": "3 events wired"},
            {
                "label": "hooks/menu_emit.py",
                "status": "ok",
                "notes": "Stop/SubagentStop/StopFailure wired",
            },
            {"label": "scripts/menu_render.py", "status": "ok", "notes": "8 modes, 5 styles"},
            {"label": "tests/", "status": "ok", "notes": "test suite green"},
            {"label": "publish.py", "status": "ok", "notes": "canonical publish pipeline"},
        ],
    },
    "panel-message": {
        "spec_version": 1,
        "mode": "panel",
        "plugin": "cms",
        "slug": "demo-panel",
        "header": "Welcome to claude-menu-system",
        "body": [
            "A universal terminal-menu system for Claude Code plugins. A caller Bash-invokes menu_write.py to render a JSON menu spec to a tempfile; the bundled Stop hook emits it as systemMessage at main-session turn end.",
            "",
            "The menu appears in the user terminal exactly when the user can type a reply — no opus tokens spent on copy-into-prose.",
        ],
        "footer": "https://github.com/Emasoft/claude-menu-system",
    },
    "multi-box-report": {
        "spec_version": 1,
        "mode": "multi_box",
        "plugin": "cms",
        "slug": "demo-multi",
        "boxes": [
            {
                "header": "Summary",
                "body": ["green test suite, high coverage, sub-second runtime"],
            },
            {
                "header": "Modes available",
                "body": [
                    "menu / summary / breakdown / status_table",
                    "panel / multi_box / progress / confirm",
                ],
            },
            {
                "header": "Box styles",
                "body": ["heavy / rounded / light / double / ascii (auto-downgrade)"],
            },
        ],
    },
    "progress-bar": {
        "spec_version": 1,
        "mode": "progress",
        "plugin": "cms",
        "slug": "demo-progress",
        "header": "Demo progress",
        "current": 12,
        "total": 20,
    },
    "confirm-prompt": {
        "spec_version": 1,
        "mode": "confirm",
        "plugin": "cms",
        "slug": "demo-confirm",
        "header": "Proceed with the demo action?",
        "yes_label": "Yes, do it",
        "no_label": "No, abort",
    },
}


def _cli(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: menu_test_specs.py <preset> <output-path>", file=sys.stderr)
        print(f"presets: {', '.join(sorted(PRESETS))}", file=sys.stderr)
        return 2
    preset = argv[1]
    out_path = Path(argv[2])
    if preset not in PRESETS:
        print(f"unknown preset {preset!r}; available: {sorted(PRESETS)}", file=sys.stderr)
        return 2
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(PRESETS[preset], indent=2) + "\n", encoding="utf-8")
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
