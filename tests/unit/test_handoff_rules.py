"""Standing rules: typed records, render order, generated files and digests."""

import pytest
from pydantic import ValidationError

from mcp_memory_service.handoff.models import CONTRACTS
from mcp_memory_service.handoff.rules import CLAUDE_RULES, CODEX_RULES, MARKER, render
from mcp_memory_service.handoff.store import HandoffStore


@pytest.fixture
def store(tmp_path):
    return HandoffStore(tmp_path / "rules-test.db")


def rule(**kwargs):
    values = {
        "project": "alpha",
        "rule_id": "style-1",
        "title": "Plain language",
        "text": "- Write comments in plain language.",
        "group": "Style",
        "order": 10,
    }
    values.update(kwargs)
    return CONTRACTS["rule_record"][0].model_validate(values).model_dump()


def test_rules_are_ordered_filtered_and_searchable(store):
    store.record("rule_record", rule(rule_id="b", group="Servers", order=20, text="- Read only."))
    store.record("rule_record", rule(rule_id="a", group="Servers", order=10, text="- Ask first."))
    store.record("rule_record", rule(rule_id="c", group="Style", order=5, audience="claude"))
    store.record("rule_record", rule(rule_id="old", status="retired", text="- Obsolete."))
    store.record("rule_record", rule(rule_id="d", directory="tools", text="- Tools rule."))
    assert [r["rule_id"] for r in store.rules("alpha")] == ["a", "b", "c", "d"]
    assert [r["rule_id"] for r in store.rules("alpha", audience="codex")] == ["a", "b", "d"]
    assert [r["rule_id"] for r in store.rules("alpha", directory="")] == ["a", "b", "c"]
    assert "old" in [r["rule_id"] for r in store.rules("alpha", include_retired=True)]
    assert not store.rules("beta")
    found = store.keyword_search("alpha", "obsolete rule comments")
    assert {r["record_id"] for r in found} <= {"old", "style-1", "a", "b", "c", "d"}
    assert any(r["record_type"] == "rules" for r in store.keyword_search("alpha", "ask first"))


def test_rule_revision_and_relative_paths(store):
    first = store.record("rule_record", rule())
    assert first["record"]["revision"] == 1 and first["changed"]
    again = store.record("rule_record", rule())
    assert not again["changed"]
    changed = store.record("rule_record", rule(text="- Updated.", base_revision=1))
    assert changed["record"]["revision"] == 2
    with pytest.raises(ValidationError):
        rule(directory="/etc")
    with pytest.raises(ValidationError):
        rule(paths=["../secrets/*"])
    assert rule(directory="tools\\sub\\")["directory"] == "tools/sub"


def test_render_layout_and_stable_digest(store):
    store.record("rule_record", rule(rule_id="intro", group="", order=0, text="Project rules."))
    store.record("rule_record", rule(rule_id="srv", group="Servers", order=1, text="- Read only."))
    store.record("rule_record", rule(rule_id="cl", group="Claude", order=1, audience="claude", text="- Claude only."))
    store.record("rule_record", rule(rule_id="cx", group="Codex", order=1, audience="codex", text="- Codex only."))
    store.record("rule_record", rule(rule_id="sub", directory="tools", group="Tools", text="- Sub rule."))
    store.record("rule_record", rule(rule_id="bash", placement="rules", load="paths", paths=["**/*.sh"], title="Bash", text="- set -euo pipefail"))
    store.record("rule_record", rule(rule_id="style", placement="rules", load="always", title="Style", text="# Style\n\n- Plain words."))
    store.record("rule_record", rule(rule_id="gone", status="retired", text="- Gone."))
    first = render(store.rules("alpha"), "alpha")
    files = first["files"]
    assert set(files) == {
        "AGENTS.md", "CLAUDE.md", "tools/AGENTS.md", "tools/CLAUDE.md",
        CLAUDE_RULES + "/bash.md", CODEX_RULES + "/bash.md",
        CLAUDE_RULES + "/style.md", CODEX_RULES + "/style.md", CODEX_RULES + "/index.md",
    }
    agents = files["AGENTS.md"]
    assert agents.startswith("<!-- " + MARKER) and first["digest"] in agents
    assert "Project rules." in agents and "## Servers" in agents and "## Codex" in agents
    assert "Claude only" not in agents and "Gone." not in agents and "Sub rule" not in agents
    assert files["CLAUDE.md"].splitlines()[2] == "@AGENTS.md" and "Claude only" in files["CLAUDE.md"]
    assert "Sub rule" in files["tools/AGENTS.md"] and "@AGENTS.md" in files["tools/CLAUDE.md"]
    bash = files[CLAUDE_RULES + "/bash.md"]
    assert bash.startswith('---\npaths:\n  - "**/*.sh"\n---\n') and "# Bash" in bash
    assert files[CLAUDE_RULES + "/style.md"].count("# Style") == 1
    assert "`bash.md`" in files[CODEX_RULES + "/index.md"]
    # Identical rule sets render byte-identical files; a change moves the digest.
    assert render(store.rules("alpha"), "alpha") == first
    store.record("rule_record", rule(rule_id="srv", group="Servers", order=1, text="- Read and ask.", base_revision=1))
    second = render(store.rules("alpha"), "alpha")
    assert second["digest"] != first["digest"] and "Read and ask" in second["files"]["AGENTS.md"]


def test_rules_query_contract_defaults():
    query = CONTRACTS["rules_list"][0].model_validate({"project": "alpha"}).model_dump()
    assert query["directory"] == "*" and query["render"] is True and query["audience"] == ""
    assert CONTRACTS["rules_list"][1] is True and CONTRACTS["rule_record"][1] is False
