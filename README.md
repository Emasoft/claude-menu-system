# claude-menu-system

[![CI](https://github.com/Emasoft/claude-menu-system/actions/workflows/ci.yml/badge.svg)](https://github.com/Emasoft/claude-menu-system/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Emasoft/claude-menu-system?label=release)](https://github.com/Emasoft/claude-menu-system/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Universal terminal-menu system for Claude Code plugins.** A haiku-fork
skill renders a JSON menu spec to a tempfile, then a `Stop` hook emits
it as `systemMessage` at main-session turn end — the menu appears in
the user's terminal **exactly when they can type a reply**, without
burning opus tokens on copy-into-prose.

## Why this exists

Until now, plugin orchestrators that wanted to display a menu had to:

1. Spend opus tokens rendering the menu themselves in prose, OR
2. Invoke a haiku fork via a `Skill` tool call to render — but the
   skill's reply was only visible to the parent agent, NOT the user.
   To display the menu, the parent then had to **copy the rendered
   text into its prose response**, burning the same opus tokens it
   was trying to save.

Empirically (see `Emasoft/token-reporter-plugin` source, line 3811),
`systemMessage` emitted from a **main-session `Stop` hook** routes
directly to the user's terminal — bypassing the parent agent entirely.
This plugin packages that pattern as a reusable building block:

- A `context: fork` haiku skill writes the rendered menu to a tempfile
- A bundled `Stop` hook reads the tempfile and emits it as `systemMessage`
- The menu appears at turn-end, right when the user is about to type

**Token cost:** the opus orchestrator never sees the menu text. Spends
≈ 50 tokens (for the Skill call + a one-word ack) instead of ≈ 2,000
tokens (for copy-into-prose).

## Install

```bash
claude plugin install Emasoft/claude-menu-system@emasoft-plugins
/reload-plugins
```

The plugin's hooks/skills load on `/reload-plugins` — no full session
restart needed. Self-test:

```
/menu-test
```

A demo menu should appear in your terminal at the end of the current turn.

## How to use it from your own plugin

In an orchestrator slash command (or any skill that needs to display
a menu), write the spec to a tempfile, invoke the skill, and let the
hook emit at turn end:

```bash
# In your orchestrator's body (Bash block):
cat > /tmp/my-plugin-menu-spec.json <<'EOF'
{
  "spec_version": 1,
  "mode":         "menu",
  "plugin":       "my-plugin",
  "slug":         "first-contact",
  "header":       "What do you want to do?",
  "rows": [
    {"key": "1", "action_id": "scan",      "label": "Scan the codebase"},
    {"key": "2", "action_id": "validate",  "label": "Validate the manifest"},
    {"key": "3", "action_id": "publish",   "label": "Publish to marketplace"},
    {"key": "0", "action_id": "cancel",    "label": "Cancel"}
  ],
  "footer": "Type a number to choose:"
}
EOF
```

```
# Then invoke the skill:
Skill({skill: "claude-menu-system:render-menu", args: "/tmp/my-plugin-menu-spec.json"})
```

The skill returns the queue file path. End your turn — the menu appears
in the user's terminal. Their reply (`1`, `2`, `3`, or `0`) arrives in
your next turn; route it via the sibling `.actions.json` file the skill
also wrote (it maps rendered key → `action_id`).

## Spec schema

Top-level required fields for every mode:

| Field          | Type   | Notes                                              |
|----------------|--------|----------------------------------------------------|
| `spec_version` | int    | Start at `1`. Unknown versions warn, not error.    |
| `mode`         | str    | One of the 8 modes below                           |
| `plugin`       | str    | Your plugin's short name (queue path component)    |
| `slug`         | str    | Short tag for debugging (queue path component)     |

### Modes

| Mode           | Purpose                                                                   |
|----------------|---------------------------------------------------------------------------|
| `menu`         | Numbered table with disabled-row drop + action map                        |
| `summary`      | Severity counts (CRITICAL/MAJOR/MINOR/NIT/WARNING) + verdict + report path |
| `breakdown`    | Per-category × per-severity matrix with row + column totals               |
| `status_table` | Component / Status / Notes table (✓/✗/⚠/◐/○/⊝/•)                          |
| `panel`        | Single titled box with body lines (auto-wrap to fit terminal width)       |
| `multi_box`    | Stack of panels with even-budget distribution                              |
| `progress`     | Title + bar + counter (`12/24 (50%)`)                                     |
| `confirm`      | Yes / No / Cancel three-row prompt                                        |

### Box styles

Set `"style"` on the spec (or accept the mode default):

| Style     | Glyphs                | Default for                                       |
|-----------|-----------------------|---------------------------------------------------|
| `heavy`   | `┏━┓ ┃ ┗━┛`           | menu / summary / breakdown / status_table         |
| `rounded` | `╭─╮ │ ╰─╯`           | panel / multi_box / progress / confirm            |
| `light`   | `┌─┐ │ └─┘`           | —                                                 |
| `double`  | `╔═╗ ║ ╚═╝`           | —                                                 |
| `ascii`   | `+-+ \| +-+`          | auto-fallback for non-unicode terminals           |

The renderer **auto-downgrades to `ascii`** when the terminal can't
render unicode box-drawing (TERM=dumb, LANG=C, `CLAUDE_MENU_ASCII=1`).
Add `"force_unicode": true` to the spec to bypass the downgrade.

### ANSI colors

Colors are embedded at render time and stripped at emit time when the
terminal doesn't want them. Auto-strip triggers (any one):

- `NO_COLOR=1` (per [no-color.org](https://no-color.org))
- `CLAUDE_MENU_COLOR=0`
- `TERM=dumb` or unset

Override with `CLAUDE_MENU_COLOR=1` to force colors even when other
signals say no.

## Environment variables

| Variable                 | Default | Purpose                                       |
|--------------------------|---------|-----------------------------------------------|
| `CLAUDE_MENU_COLOR`      | (auto)  | `0` strips color, `1` forces color            |
| `CLAUDE_MENU_ASCII`      | `0`     | `1` forces ASCII box-drawing                  |
| `CLAUDE_MENU_MAX_WIDTH`  | `120`   | Cap auto-width at this many columns           |
| `CLAUDE_MENU_TTL_SEC`    | `60`    | Stale menus older than this are dropped       |
| `CLAUDE_MENU_DEBUG`      | `0`     | `1` writes hook activity to `/tmp/claude-menu-system-debug.log` |
| `NO_COLOR`               | —       | Industry-standard color suppression           |
| `TERM`                   | (auto)  | `dumb` or empty → ASCII + no color            |
| `COLUMNS`                | (auto)  | Terminal width hint (overrides auto-detect)   |

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ orchestrator (any plugin, any model)                         │
│  1. write JSON spec to /tmp/<spec>.json                      │
│  2. Skill({skill: "claude-menu-system:render-menu", args})   │
└────────────────┬─────────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────────┐
│ render-menu skill (haiku fork, agent: general-purpose)       │
│  body: `python3 $CLAUDE_PLUGIN_ROOT/scripts/menu_write.py`   │
│   → reads spec, renders via menu_render, queues file         │
│   → replies with queue path (one line, nothing else)         │
└────────────────┬─────────────────────────────────────────────┘
                 │
                 ▼ skill fork ends → SubagentStop fires
┌─────────────────────────────────────────────────────────────┐
│ SubagentStop hook (menu_emit.py)                             │
│  · logs only — systemMessage from SubagentStop routes to AI  │
│    context, NOT the user terminal. Menu file stays queued.   │
└──────────────────────────────────────────────────────────────┘

                 ... main session continues, eventually turn ends ...

┌─────────────────────────────────────────────────────────────┐
│ Stop hook (menu_emit.py)  ← fires at main-session turn end   │
│  · scans ${TMPDIR}/claude-menu-system/<session_id>/          │
│  · concatenates menu files in timestamp order                 │
│  · applies 10K cap with tiered truncation                    │
│  · emits as {"systemMessage": "..."} on stdout               │
│  · deletes processed files                                    │
└────────────────┬─────────────────────────────────────────────┘
                 │
                 ▼
        ╔═══════════════════╗
        ║  user's terminal  ║  ← menu rendered, user types reply
        ╚═══════════════════╝
```

## Performance + cap behavior

Claude Code caps hook output at **10,000 chars** including the JSON
wrapper. The emit script enforces a 9,500-char hard budget and applies
a three-tier strategy:

- **SMALL** (≤ 1,000 chars per menu) → emitted untouched
- **BIG** (> 1,000 chars) → body rows dropped from the bottom, header +
  footer + `[N rows truncated]` indicator preserved
- **OVERFLOW** (> 10 queued menus OR combined > 9,500 chars) →
  older menus collapse to title-only stubs, newest 2 stay full

Line-safe truncation (cut only at `\n` boundaries) ensures ANSI codes
are never severed mid-escape.

## Development

```bash
git clone https://github.com/Emasoft/claude-menu-system.git
cd claude-menu-system
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"

# Run tests
uv run pytest -n auto

# Run lint
uv run ruff check .
uv run ruff format .
```

The test suite is 188 tests covering 95%+ of every module. All tests
run in <1 second on a modern laptop.

## License

MIT — see [LICENSE](LICENSE).
