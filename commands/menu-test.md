---
description: Run the claude-menu-system self-test — renders a demo menu so you can verify the plugin is wired correctly. Use after install or after upgrading.
allowed-tools: Bash(python3:*)
---

# /menu-test

Self-test for the claude-menu-system plugin. Renders a demo menu and
queues it so you can confirm the hook + emit pipeline works end-to-end.

## Steps

1. Build the demo spec then invoke the renderer directly via Bash (no
   skill — Option 1 path, runs in this turn at ~25ms):

   ```bash
   python3 "$CLAUDE_PLUGIN_ROOT/scripts/menu_test_specs.py" first-contact /tmp/cms-test-spec.json
   python3 "$CLAUDE_PLUGIN_ROOT/scripts/menu_write.py" /tmp/cms-test-spec.json
   ```

2. Tell the user (one line) that the menu will appear at the end of
   this turn. Do nothing else — the Stop hook handles emission.

If the menu appears cleanly bordered in the terminal at the end of
this turn, the plugin is wired correctly. If it doesn't:

- Verify `claude-menu-system` is enabled: `jq '.enabledPlugins["claude-menu-system@emasoft-plugins"]' ~/.claude/settings.json`
- Run `/reload-plugins` after enabling
- Check `/tmp/claude-menu-system-debug.log` after setting `CLAUDE_MENU_DEBUG=1` in your env
