#!/usr/bin/env python3
"""Regression lock: every release must also push the `{name}--v{version}` tag.

WHY: since Claude Code 2.1.110 a VERSION-CONSTRAINED dependency
(`{"name": "claude-menu-system", "version": ">=0.1.5"}`) resolves ONLY against
tags named `{plugin-name}--v{version}`. A plain `vX.Y.Z` tag is ignored by that
resolver.

This repo shipped only plain `vX.Y.Z` tags, so every dependent plugin failed to
install with `no-matching-tag` and was DISABLED — invisibly, because an
already-installed dependent keeps working (claude-plugins-validation#163,
claude-menu-system#2).

These tests pin the two properties that failure needed:
  1. the dependency tag is derived from the manifest (not hardcoded), and
  2. it is included in the release push.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import publish


class TestDependencyTagName:
    def test_uses_the_double_hyphen_separator(self) -> None:
        """`--v`, not `-v`.

        A single hyphen does not match Claude Code's `{name}--v` prefix filter;
        several tags in the ecosystem got this wrong and silently resolved nothing.
        """
        tag = publish.dependency_tag_name(REPO, "0.2.0")
        assert tag == "claude-menu-system--v0.2.0"
        assert "--v" in tag

    def test_name_is_read_from_the_manifest(self, tmp_path: Path) -> None:
        """Derived from plugin.json so a rename cannot desync the tag."""
        (tmp_path / ".claude-plugin").mkdir()
        (tmp_path / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "renamed-plugin", "version": "9.9.9"}), encoding="utf-8"
        )
        assert publish.dependency_tag_name(tmp_path, "9.9.9") == "renamed-plugin--v9.9.9"

    def test_returns_none_when_the_manifest_is_unreadable(self, tmp_path: Path) -> None:
        """No manifest → no invented name (the caller warns and skips)."""
        assert publish.dependency_tag_name(tmp_path, "1.0.0") is None

    def test_matches_what_the_official_claude_plugin_tag_produces(self) -> None:
        """`claude plugin tag --dry-run` on this repo emits exactly this name.

        Pinned so the pipeline can never drift away from the official convention.
        """
        version = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
        assert publish.dependency_tag_name(REPO, version) == f"claude-menu-system--v{version}"


class TestReleasePushIncludesTheDependencyTag:
    def test_dry_run_pushes_both_tags_atomically(self, capsys) -> None:
        """The release push must carry BOTH refs.

        A release that pushes only `vX.Y.Z` is exactly the bug: it looks like a
        successful release while leaving every dependent unable to resolve it.
        """
        publish.stage_commit_and_push(REPO, "0.2.1", dry_run=True)
        out = capsys.readouterr().out
        assert "claude-menu-system--v0.2.1" in out
        push_line = [ln for ln in out.splitlines() if "Would push" in ln]
        assert push_line, out
        assert "v0.2.1" in push_line[0]
        assert "claude-menu-system--v0.2.1" in push_line[0]
