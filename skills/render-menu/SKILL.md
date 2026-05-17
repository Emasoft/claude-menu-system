---
name: render-menu
description: Render a menu/summary/breakdown/panel spec to a tempfile so the Stop hook can emit it as systemMessage at the next main-session turn end. Used by orchestrator commands (in any plugin) that need to display interactive menus without burning opus tokens on copy-into-prose. NEVER user-invocable — other plugins invoke via Skill({skill: "claude-menu-system:render-menu", args: "<spec-path>"}).
context: fork
model: haiku
agent: general-purpose
user-invocable: false
allowed-tools: Bash(python3:*)
---

# render-menu skill

You are a renderer. You receive ONE argument — the absolute path to a
JSON spec file. Your only job is to invoke the menu_write.py script
with that path and reply with the queue file path it produces.

## Steps

1. The argument is in `$ARGUMENTS`. It MUST be the absolute path to a
   readable JSON file. Do not parse it, do not interpret it.

2. Invoke the writer script:

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/scripts/menu_write.py" "$ARGUMENTS"
   ```

3. The script prints exactly one line: the absolute path of the queue
   file it created. Reply to the parent with that path, nothing else.
   No prose, no commentary, no markdown — just the path.

4. If the script exits non-zero, reply with `error: <exit-code>` and
   nothing else. The parent orchestrator will surface the error.

## Why this works

The Stop hook (declared in this plugin's `hooks/hooks.json`) fires at
main-session turn end and scans `${TMPDIR}/claude-menu-system/<session_id>/`
for queued menu files. It emits everything it finds as a single
systemMessage JSON to stdout — the user terminal renders it. Multi-menu
queues are emitted in timestamp order with the 10K char cap honored
via tiered truncation.

The fork running THIS skill is haiku — cheap. The opus orchestrator
that invoked it never sees the menu text, never copies it into prose,
never spends tokens on rendering.

## Argument shape

`$ARGUMENTS` must be a path to a JSON file matching one of the
supported modes (menu, summary, breakdown, status_table, panel,
multi_box, progress, confirm). See the plugin README for the full
spec schema with examples.

## Do NOT

- Do NOT render the menu yourself. The script handles every detail
  (display-width-aware padding, ANSI colors with env-aware downgrade,
  Unicode box-drawing with ASCII fallback for dumb terminals, etc.).
- Do NOT echo the spec content into your reply.
- Do NOT add prose, headers, or explanation.
- Do NOT call any other tool besides the single Bash invocation above.
