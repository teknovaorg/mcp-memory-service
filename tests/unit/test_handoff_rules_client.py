"""Generated rule files, per-path hook context and one-time Markdown import."""

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))

from shared_memory import __main__ as cli
from shared_memory.connection import LinkError
from shared_memory.rules import context_for, matches, parse_markdown, sync, write_files

AGENTS = """<!-- test:start -->
# Project rules

Intro paragraph that applies everywhere.

## Servers

- Read only on production.
  Prepare commands for the operator.
- Ask before deleting data.

| Scope | Allowed |
|---|---|
| Local | everything |

## Style

- Plain language.
<!-- test:end -->
"""


def test_markdown_import_splits_blocks_in_order():
    rules = parse_markdown(AGENTS, "agents")
    assert [r["rule_id"] for r in rules] == ["agents-intro-01", "agents-servers-02", "agents-servers-03", "agents-servers-04", "agents-style-05"]
    assert [r["group"] for r in rules] == ["", "Servers", "Servers", "Servers", "Style"]
    assert rules[1]["text"] == "- Read only on production.\n  Prepare commands for the operator.\n"
    assert rules[3]["text"].startswith("| Scope |") and rules[3]["title"] == "Scope"
    assert [r["order"] for r in rules] == [10, 20, 30, 40, 50]
    assert all(r["load"] == "always" and r["placement"] == "agents" for r in rules)
    assert "<!--" not in "".join(r["text"] for r in rules)


def test_markdown_import_rule_file_with_paths():
    text = '---\npaths:\n  - "**/*.sh"\n  - "scripts/**"\n---\n\n# Bash\n\n- set -euo pipefail\n'
    (rule,) = parse_markdown(text, "bash", placement="rules")
    assert rule["rule_id"] == "bash" and rule["title"] == "Bash"
    assert rule["load"] == "paths" and rule["paths"] == ["**/*.sh", "scripts/**"]
    assert rule["text"] == "# Bash\n\n- set -euo pipefail\n"


def test_path_matching_covers_nested_top_level_and_directories():
    assert matches("a/b/c.sh", ["**/*.sh"]) and matches("c.sh", ["**/*.sh"])
    assert matches("docs/x/y.md", ["docs/**"]) and matches("docs", ["docs/**"])
    assert not matches("notes/x.md", ["docs/**"]) and not matches("a.py", ["**/*.sh"])
    assert matches("GITLAB\\ci\\x.yml", ["**/*.yml"])


def test_generated_files_keep_guidance_block_back_up_and_prune(tmp_path):
    root = tmp_path / "project"
    (root / ".claude/rules").mkdir(parents=True)
    (root / ".codex/rules").mkdir(parents=True)
    (root / "AGENTS.md").write_text("hand written\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("old\n\n<!-- shared-memory:start -->\nguidance\n<!-- shared-memory:end -->\n", encoding="utf-8")
    (root / ".claude/rules/old.md").write_text("<!-- shared-memory rules: digest 1 -->\nstale\n", encoding="utf-8")
    (root / ".claude/rules/mine.md").write_text("personal\n", encoding="utf-8")
    (root / ".codex/rules/bash.md").symlink_to("../../.claude/rules/bash.md")
    files = {
        "AGENTS.md": "<!-- shared-memory rules: digest 2 -->\n\nnew\n",
        "CLAUDE.md": "<!-- shared-memory rules: digest 2 -->\n\n@AGENTS.md\n",
        ".claude/rules/bash.md": "<!-- shared-memory rules: digest 2 -->\n# Bash\n",
        ".codex/rules/bash.md": "<!-- shared-memory rules: digest 2 -->\n# Bash\n",
    }
    report = write_files(root, files, tmp_path / "backups")
    assert set(report["written"]) == set(files) and report["backed_up"] == ["AGENTS.md", "CLAUDE.md"]
    assert (tmp_path / "backups/AGENTS.md").read_text() == "hand written\n"
    claude = (root / "CLAUDE.md").read_text()
    assert claude.startswith("<!-- shared-memory rules") and claude.rstrip().endswith("<!-- shared-memory:end -->")
    assert report["removed"] == [".claude/rules/old.md"] and (root / ".claude/rules/mine.md").is_file()
    assert not (root / ".codex/rules/bash.md").is_symlink() and (root / ".codex/rules/bash.md").read_text().endswith("# Bash\n")
    again = write_files(root, dict(files, **{"CLAUDE.md": files["CLAUDE.md"]}), tmp_path / "backups")
    assert again["written"] == [] and again["unchanged"] == len(files)
    with pytest.raises(LinkError):
        write_files(root, {"../escape.md": "x"}, tmp_path / "backups")
    # Explicit destinations serve user-level instruction files outside the root.
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    mapped = write_files(root, {"AGENTS.md": "<!-- shared-memory rules: digest 3 -->\n\nglobal\n"}, tmp_path / "backups", {"AGENTS.md": str(home / ".codex/AGENTS.md")})
    assert mapped["written"] == ["AGENTS.md"] and (home / ".codex/AGENTS.md").read_text().endswith("global\n")
    assert (root / "AGENTS.md").read_text().endswith("new\n")


class FakeLink:
    def __init__(self, listing):
        self.listing = listing
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.listing


def test_sync_writes_cache_and_hook_injects_matching_rules_once(tmp_path, monkeypatch, capsys):
    root = tmp_path / "work/acme"
    root.mkdir(parents=True)
    base = tmp_path / "base"
    listing = {
        "digest": "abc123",
        "count": 2,
        "rules": [
            {"rule_id": "bash", "title": "Bash", "text": "- set -euo pipefail", "load": "paths", "paths": ["**/*.sh"], "placement": "rules", "directory": "", "audience": "all", "revision": 1, "status": "active"},
            {"rule_id": "codex-only", "title": "Codex", "text": "- codex rule", "load": "paths", "paths": ["**/*.sh"], "placement": "rules", "directory": "", "audience": "codex", "revision": 1, "status": "active"},
        ],
        "files": {"AGENTS.md": "<!-- shared-memory rules: digest abc123 -->\n\nrules\n"},
    }
    profile = {"project": "acme", "roots": [str(root)], "url": "https://memory.test/handoff", "token": "t"}
    report = sync(FakeLink(listing), profile, base)
    assert report["digest"] == "abc123" and report["written"] == ["AGENTS.md"]
    assert (root / "AGENTS.md").read_text().endswith("rules\n")
    event = {"hook_event_name": "PreToolUse", "session_id": "s1", "cwd": str(root), "tool_name": "Write", "tool_input": {"file_path": str(root / "scripts/run.sh")}}
    text = context_for(base, "acme", "claude", event)
    assert "set -euo pipefail" in text and "codex rule" not in text and "scripts/run.sh" in text
    assert context_for(base, "acme", "claude", event) == ""
    patch = {"hook_event_name": "PreToolUse", "session_id": "s2", "cwd": str(root), "tool_name": "apply_patch", "tool_input": {"patch": "*** Begin Patch\n*** Update File: scripts/run.sh\n*** End Patch"}}
    assert "codex rule" in context_for(base, "acme", "codex", patch)
    assert context_for(base, "acme", "claude", dict(event, session_id="s3", tool_input={"file_path": str(root / "a.py")})) == ""
    config = {"schema": 1, "device_id": "d", "url": "https://memory.test/handoff", "projects": {"acme": {"token": "t", "roots": [str(root)]}}}
    registry = base / "connections.json"
    registry.write_text(json.dumps(config))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(dict(event, session_id="s4"))))
    cli.hook(config, SimpleNamespace(agent="claude", registry=str(registry)))
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "set -euo pipefail" in output["hookSpecificOutput"]["additionalContext"]
