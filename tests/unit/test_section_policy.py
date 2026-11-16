import json

import pytest

from mcp_memory_service.handoff.models import CONTRACTS
from mcp_memory_service.handoff.store import HandoffStore
from mcp_memory_service.handoff.topics import classify, definitions, label, normalize


def test_private_sections_drive_classification_and_database_labels(
    tmp_path, monkeypatch
):
    path = tmp_path / "sections.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "sections": {
                    "custom-area": {"name": "Private example", "pattern": "demo-tool"}
                },
                "aliases": {"short alias": "custom-area"},
            }
        )
    )
    monkeypatch.setenv("MCP_HANDOFF_SECTIONS_FILE", str(path))
    assert classify("demo-tool apply") == ["custom-area"]
    assert normalize("SHORT ALIAS") == "custom-area"
    assert label("custom-area") == "Private example"
    store = HandoffStore(tmp_path / "memory.db")
    data = (
        CONTRACTS["session_ingest"][0]
        .model_validate(
            {
                "project": "example",
                "session_id": "session",
                "agent": "fixture",
                "source": "fixture",
                "events": [
                    {
                        "event_id": "call",
                        "kind": "command",
                        "command": "demo-tool apply",
                    }
                ],
            }
        )
        .model_dump()
    )
    store.ingest(data)
    sections = store.topic_list("example")["sections"]
    assert sections == [{"id": "custom-area", "name": "Private example", "records": 1}]
    assert (
        store.section_search("example", "apply", ["custom-area"])[0]["event_id"]
        == "call"
    )


def test_invalid_or_missing_private_policy_does_not_silently_use_defaults(
    tmp_path, monkeypatch
):
    path = tmp_path / "sections.json"
    monkeypatch.setenv("MCP_HANDOFF_SECTIONS_FILE", str(path))
    with pytest.raises(FileNotFoundError):
        definitions()
    path.write_text('{"schema":999,"sections":{}}')
    with pytest.raises(ValueError, match="Unsupported private section policy"):
        definitions()


def test_generic_discovery_keeps_unreviewed_projects_in_archive(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))
    from shared_memory import discover

    monkeypatch.setattr(
        discover,
        "locations",
        lambda config: {
            "codex": tmp_path / "history",
            "claude": tmp_path / "notes",
            "claude_config": tmp_path / "settings.json",
        },
    )
    monkeypatch.setattr(
        discover,
        "sqlite_threads",
        lambda home: [{"cwd": "/work/Developer/ExampleWorkspace/module"}],
    )
    result = discover.discover()["projects"]["exampleworkspace"]
    assert result["roots"] == [
        "/work/Developer/ExampleWorkspace",
        "/work/Developer/ExampleWorkspace/module",
    ]
    assert result["knowledge_scope"] == "archive"
    assert result["default_sections"] == []
