---
description: Run the claude-menu-system self-test — renders a demo menu in every mode so you can verify the plugin is wired correctly. Use after install or after upgrading.
allowed-tools: Bash(python3:*), Skill
---

# /menu-test

Self-test for the claude-menu-system plugin. Renders a demo menu so
you can confirm the hook + skill + emit pipeline works end-to-end.

## Steps

1. Build the demo spec (writes a `menu` spec to a tempfile):

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/scripts/menu_test_specs.py" first-contact /tmp/cms-test-spec.json
   ```

2. Invoke the renderer skill to queue the menu. The skill returns the
   queue file path and ends — the menu does NOT appear immediately,
   it's queued for emission at this turn's end:

   ```
   Skill({skill: "claude-menu-system:render-menu", args: "/tmp/cms-test-spec.json"})
   ```

3. Tell the user (one line) that the menu will appear at the end of
   this turn. Do nothing else — the Stop hook handles emission.

If the menu appears in the terminal at the end of this turn, the
plugin is wired correctly. If it doesn't:

- Check `/tmp/claude-menu-system-debug.log` if `CLAUDE_MENU_DEBUG=1`
  is set
- Verify `claude-menu-system` is enabled: `jq '.enabledPlugins["claude-menu-system@emasoft-plugins"]' ~/.claude/settings.json`
- Run `/reload-plugins` after enabling
