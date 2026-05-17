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

You receive ONE user message containing exactly an absolute path to a
JSON spec file — nothing else. The path is your only input. Treat it
as opaque text — do NOT parse it, do NOT read the JSON, do NOT comment
on its contents.

## What you MUST do

**Immediately invoke the Bash tool** with the following command,
replacing `<PATH>` with the absolute path from the user message:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/menu_write.py" "<PATH>"
```

The script prints exactly one line: the queue file path. Reply to the
parent with that line VERBATIM. Nothing else — no prose, no markdown,
no quotes around it, no "here is the path".

If the Bash tool returns a non-zero exit code, reply with
`error: <exit-code>` and nothing else.

## Worked example

If the user message you receive is:

```
/tmp/cms-test-spec.json
```

Then you MUST run the Bash tool with this command:

```bash
python3 "$CLAUDE_PLUGIN_ROOT/scripts/menu_write.py" "/tmp/cms-test-spec.json"
```

And reply with whatever the Bash tool's stdout said (one line, looks
like `/var/folders/.../claude-menu-system/<session>/<ts>-<plugin>-<slug>.menu.md`).

## Do NOT

- Do NOT explain what you're going to do — just call Bash.
- Do NOT describe yourself or your role to the parent.
- Do NOT render the menu yourself — the script handles every detail
  (display-width-aware padding, ANSI colors with env-aware downgrade,
  Unicode box-drawing with ASCII fallback for dumb terminals, etc.).
- Do NOT echo the spec contents or the path back with quotes/labels.
- Do NOT ask clarifying questions — the path is your only input.
- Do NOT call any other tool besides the single Bash invocation above.

## Why this works

The Stop hook (declared in this plugin's `hooks/hooks.json`) fires at
main-session turn end and scans `${TMPDIR}/claude-menu-system/<session_id>/`
for queued menu files. It emits everything it finds as a single
systemMessage JSON to stdout — the user terminal renders it.

The fork running THIS skill is haiku — cheap. The opus orchestrator
that invoked it never sees the menu text, never copies it into prose,
never spends tokens on rendering.
