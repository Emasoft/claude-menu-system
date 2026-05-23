# claude-menu-system

<!--BADGES-START-->
[![version](https://img.shields.io/badge/version-0.1.5-blue)](https://github.com/Emasoft/claude-menu-system/releases)
[![CI](https://github.com/Emasoft/claude-menu-system/actions/workflows/ci.yml/badge.svg)](https://github.com/Emasoft/claude-menu-system/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Emasoft/claude-menu-system?label=release)](https://github.com/Emasoft/claude-menu-system/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
<!--BADGES-END-->

The `AskQuestion` prompt is not good for a big number of choices.

Just run this command and you get a nice menu with as many entries as you need:

```bash
cat > /tmp/my-menu.json <<'EOF'
{
  "spec_version": 1,
  "mode":         "menu",
  "plugin":       "my-plugin",
  "slug":         "main",
  "header":       "What do you want to do?",
  "rows": [
    {"key": "1", "action_id": "scan",     "label": "Scan the codebase"},
    {"key": "2", "action_id": "validate", "label": "Validate the manifest"},
    {"key": "3", "action_id": "publish",  "label": "Publish to marketplace"},
    {"key": "0", "action_id": "cancel",   "label": "Cancel"}
  ],
  "footer": "Type a number to choose:"
}
EOF
python3 "$CLAUDE_PLUGIN_ROOT/scripts/menu_write.py" /tmp/my-menu.json
```

That's it. The menu appears in the user's terminal at the end of the
current turn (a bundled `Stop` hook does the emit). The user's reply
arrives in your next turn.

Add as many rows as you need — no 4-option cap, no truncation.

## Install

```bash
claude plugin install Emasoft/claude-menu-system@emasoft-plugins
/reload-plugins
```

Self-test:

```
/menu-test
```

## Calling from another plugin

If your plugin lives in a different marketplace, `$CLAUDE_PLUGIN_ROOT`
points at YOUR plugin — not this one. Resolve the cache path:

```bash
CMS_ROOT=$(ls -d ~/.claude/plugins/cache/emasoft-plugins/claude-menu-system/*/ | sort -V | tail -1)
python3 "$CMS_ROOT/scripts/menu_write.py" /tmp/my-menu.json
```

## Routing the user's reply

`menu_write.py` also writes a sibling `.actions.json` file mapping
rendered key → `action_id`. Read it in your next turn to route the
reply:

```bash
ACTIONS=$(cat /tmp/my-menu.actions.json)
# ACTIONS is JSON like: {"1":"scan","2":"validate","3":"publish","0":"cancel"}
```

## Other modes

`mode: menu` is the common case. Seven more modes ship for richer
output:

| Mode           | Purpose                                                          |
|----------------|------------------------------------------------------------------|
| `menu`         | Numbered table with action map (above)                           |
| `summary`      | Severity counts + verdict + report path                          |
| `breakdown`    | Per-category × per-severity matrix                               |
| `status_table` | Component / Status / Notes table (✓/✗/⚠/◐/○)                     |
| `panel`        | Single titled box with body text                                 |
| `multi_box`    | Stack of panels                                                  |
| `progress`     | Title + bar + counter (`12/24 (50%)`)                            |
| `confirm`      | Yes / No / Cancel prompt                                         |

See `examples/` for one canonical spec per mode — copy and adjust.

## Optional spec fields

These can appear in any spec on top of the per-mode required fields.

| Field          | Type             | Default | Meaning                                                                                   |
|----------------|------------------|---------|-------------------------------------------------------------------------------------------|
| `truncate_at`  | `int > 0` / null | absent  | Per-menu cap (chars) consumed by the emit hook. `null` disables per-menu shaping entirely (overflow fails loudly under the 9500-char queue cap). Absent = default heuristic shaping. |

Reserved keys (always preserved verbatim even with `renumber:true`):
`0`, `A`, `M`, `B`, `X` — the last three are CPV's navigation letters
(Main / Back / eXit) so menus that route through CPV's fixed-key contract
keep their nav glyphs stable across rerenders.

## License

MIT — see [LICENSE](LICENSE).
