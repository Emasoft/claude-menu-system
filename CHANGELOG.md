# Changelog

All notable changes to **claude-menu-system** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.2] — 2026-05-17

### Fixed

- **Critical UX bug** — emitted `systemMessage` now prepends a `\n` so
  the menu box starts on its own line beneath Claude Code's `Stop says:`
  banner. v0.1.1 emitted the box's first row inline with the banner,
  shifting every subsequent row right and breaking visual alignment.
  Regression-pinned by `test_systemMessage_always_starts_with_newline`.
- **render-menu skill body** — rewritten to NOT rely on the
  `$ARGUMENTS` placeholder (slash-command syntax, not skill syntax).
  The haiku fork now receives explicit instructions to take the user
  message as the path and invoke the Bash tool with it. v0.1.1's
  body left the fork describing what it would do rather than actually
  executing the script.

## [0.1.1] — 2026-05-17

### Added

- `/menu-test` slash command — self-test that renders a demo menu so
  users can verify the plugin is wired correctly after install.
- `/menu-render <spec-path>` slash command — ad-hoc rendering from any
  JSON spec file on disk; useful for plugin authors testing their specs.
- `scripts/menu_test_specs.py` — helper script that produces canonical
  demo specs for all 8 modes. Used by `/menu-test` and re-used to seed
  the `examples/` gallery.
- `examples/` directory with 8 canonical JSON specs (one per render
  mode) — plugin authors can copy-paste these as starting points.

### Notes

- All changes additive — no behavior change to v0.1.0 hooks / skill /
  renderer. Consumer plugins built against v0.1.0 work unchanged on v0.1.1.

## [0.1.0] — 2026-05-17

Initial release. First-class, reusable menu rendering for Claude Code
plugins via the systemMessage-from-Stop-hook pattern empirically
proven against `Emasoft/token-reporter-plugin`.

### Added

- `render-menu` skill (`context: fork`, `model: haiku`,
  `agent: general-purpose`) — other plugins invoke via
  `Skill({skill: "claude-menu-system:render-menu", args: "<spec>"})`.
- `Stop` / `StopFailure` hooks that emit the queued menu as
  `systemMessage` at main-session turn end.
- `SubagentStop` hook that no-ops (systemMessage from subagent-stop
  routes to AI context, not the user terminal — documented).
- 8 render modes: `menu`, `summary`, `breakdown`, `status_table`,
  `panel`, `multi_box`, `progress`, `confirm`.
- 5 box styles: `heavy` (default for menus), `rounded` (default for
  panels), `light`, `double`, `ascii`. Auto-downgrade to `ascii` when
  the terminal can't render unicode box-drawing.
- Semantic ANSI palette (border / header / value / success / warning /
  error / info / muted + severity tiers). Auto-strip when `NO_COLOR=1`,
  `CLAUDE_MENU_COLOR=0`, or `TERM=dumb`.
- Display-width-aware padding via `unicodedata.east_asian_width` —
  correct alignment for emoji + CJK + box-drawing glyphs.
- ANSI-safe text wrapping that preserves color state across continuation
  lines (ported from token-reporter `_wrap_ansi`).
- Auto-terminal-width detection (`COLUMNS` env → `shutil.get_terminal_size`
  → 80 default, capped at `CLAUDE_MENU_MAX_WIDTH` default 120).
- 10K hook-output cap with tiered truncation (SMALL untouched / BIG
  row-dropped / OVERFLOW title-stubbed) and line-safe truncation
  (cut only at `\n` boundaries, never mid-ANSI-escape).
- Session-isolated queue dir
  (`${TMPDIR}/claude-menu-system/<session_id>/`).
- `flock`-protected concurrent emit (multiple Stop hooks don't race).
- TTL-based stale cleanup (default 60s, configurable via
  `CLAUDE_MENU_TTL_SEC`).
- 188 tests (≥95% line coverage on every module), all running in
  under 1 second via `pytest-xdist`.

### Notes

- Runtime dependency: stdlib only (no third-party deps).
- Plugin-scoped hooks — picked up by `/reload-plugins`, no full session
  restart needed.
- Consumer plugins must hard-require `claude-menu-system` to be enabled.
  Pre-flight check pattern in the README.
