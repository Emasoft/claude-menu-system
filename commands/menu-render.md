---
name: menu-render
description: Render a menu from a JSON spec file (ad-hoc — useful for testing your own plugin's menu specs without integrating yet).
argument-hint: <path-to-spec.json>
allowed-tools: Bash(python3:*)
---

# /menu-render <spec-path>

Render a menu from a JSON spec at `$ARGUMENTS`. The menu appears at
the end of this turn via the Stop hook.

## Steps

1. Invoke the renderer directly via Bash (no skill — Option 1 path,
   runs in this turn at ~25ms):

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/scripts/menu_write.py" "$ARGUMENTS"
   ```

2. Reply with one line confirming the menu was queued. Do NOT echo the
   spec contents or describe the menu — the Stop hook will surface it
   to the user terminal at turn end.

If the spec is malformed, the script exits non-zero with the error on
stderr. Surface the error verbatim and stop.
