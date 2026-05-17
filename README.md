# claude-menu-system

[![CI](https://github.com/Emasoft/claude-menu-system/actions/workflows/ci.yml/badge.svg)](https://github.com/Emasoft/claude-menu-system/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Emasoft/claude-menu-system?label=release)](https://github.com/Emasoft/claude-menu-system/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

**Universal terminal-menu system for Claude Code plugins.** Any agent
writes a JSON menu spec to a tempfile via Bash; a bundled `Stop` hook
emits it as `systemMessage` at main-session turn end — the menu appears
in the user's terminal **exactly when they can type a reply**.

The renderer runs in **~25 ms** (Python interpreter startup + 0.4 ms of
actual work). No subagent fork, no extended-thinking pass, no token cost
to the orchestrator — just a normal Bash tool call.

## Why this exists

Empirically (see `Emasoft/token-reporter-plugin` source, line 3811),
`systemMessage` emitted from a **main-session `Stop` hook** routes
directly to the user's terminal — bypassing the parent agent entirely.
This plugin packages that pattern as a reusable building block:

- Any agent runs `python3 $CLAUDE_PLUGIN_ROOT/scripts/menu_write.py spec.json` via Bash
- The script renders the menu (Unicode box-drawing, ANSI colors, auto-wrap, auto-width) and queues it
- The bundled `Stop` hook emits the queue as `systemMessage` at turn end
- The menu appears in the user's terminal, right when they can type

**Why not a skill?** Earlier versions shipped a `render-menu` skill that
spawned a haiku fork. Benchmarks (v0.1.2) showed the skill path took
**15-25 seconds** vs **25 ms** for direct Bash, dominated by model
inference overhead (especially with `CLAUDE_CODE_EFFORT_LEVEL=max`).
The skill path was removed in v0.1.3 because no one would wait that
long for a menu. Use Bash.

## Install

```bash
claude plugin install Emasoft/claude-menu-system@emasoft-plugins
/reload-plugins
```

The plugin's hooks load on `/reload-plugins` — no full session restart
needed. Self-test:

```
/menu-test
```

A demo menu should appear in your terminal at the end of the current turn.

## How to use it from your own plugin

In any orchestrator slash command, agent, or skill body — write the spec
to a tempfile and invoke the writer script via Bash. The hook does the rest:

```bash
# In your orchestrator's body (single Bash block):
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
python3 "$CLAUDE_PLUGIN_ROOT/scripts/menu_write.py" /tmp/my-plugin-menu-spec.json
```

The script prints the queue file path on stdout (one line) and exits.
End your turn — the menu appears in the user's terminal via the Stop
hook. Their reply (`1`, `2`, `3`, or `0`) arrives in your next turn;
route it via the sibling `.actions.json` file the script also wrote
(it maps rendered key → `action_id`).

**Cross-plugin invocation:** other plugins call into this one via
the script path under the cache root:

```bash
python3 ~/.claude/plugins/cache/emasoft-plugins/claude-menu-system/<version>/scripts/menu_write.py spec.json
```

Resolve the version dynamically:

```bash
CMS_ROOT=$(ls -d ~/.claude/plugins/cache/emasoft-plugins/claude-menu-system/*/ | sort -V | tail -1)
python3 "$CMS_ROOT/scripts/menu_write.py" spec.json
```

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

See `examples/` for canonical specs you can copy-paste — one per mode.

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
│ orchestrator (any plugin, any model, any agent)             │
│  python3 $CLAUDE_PLUGIN_ROOT/scripts/menu_write.py spec.json│
│                                                              │
│  · validates spec via menu_spec.py                          │
│  · renders via menu_render.py (8 modes, 5 styles, ANSI)     │
│  · queues to $TMPDIR/claude-menu-system/<session_id>/       │
│  · prints queue path (~25ms total)                          │
└────────────────┬────────────────────────────────────────────┘
                 │
                 ▼  ... orchestrator continues working ...
                    eventually the main-session turn ends ...

┌─────────────────────────────────────────────────────────────┐
│ Stop hook (menu_emit.py)  ← fires at main-session turn end  │
│  · scans $TMPDIR/claude-menu-system/<session_id>/            │
│  · concatenates menu files in timestamp order                │
│  · applies 10K cap with tiered truncation                    │
│  · strips ANSI if NO_COLOR / TERM=dumb at hook-fire time     │
│  · emits as {"systemMessage": "\n..."} on stdout             │
│  · deletes processed files                                   │
└────────────────┬────────────────────────────────────────────┘
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

End-to-end timing measured on Apple Silicon Python 3.12 (50 iterations):

| Phase | Median | Notes |
|---|---:|---|
| `python3 menu_write.py spec.json` | **25 ms** | dominated by interpreter startup (~13ms) + 3 module imports (~12ms) |
| Pure work after imports paid | 0.4 ms | json load + validate + render + 2 atomic writes |
| Stop hook end-to-end | 25 ms | same shape — interpreter + emit script imports + JSON encode + print |

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

The test suite is 189 tests covering 95%+ of every module. All tests
run in <1 second on a modern laptop.

## License

MIT — see [LICENSE](LICENSE).
