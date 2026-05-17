---
description: Render a menu from a JSON spec file (ad-hoc — useful for testing your own plugin's menu specs without integrating yet).
argument-hint: <path-to-spec.json>
allowed-tools: Skill
---

# /menu-render <spec-path>

Render a menu from a JSON spec at `$ARGUMENTS`. The menu appears at
the end of this turn via the Stop hook.

## Steps

1. Invoke the renderer:

   ```
   Skill({skill: "claude-menu-system:render-menu", args: "$ARGUMENTS"})
   ```

2. Reply with one line confirming the menu was queued. Do NOT echo the
   spec contents or describe the menu — the Stop hook will surface it
   to the user terminal at turn end.

If the spec is malformed, the skill returns a non-zero exit code with
the error on stderr. Surface the error verbatim and stop.
