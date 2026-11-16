"""Portable source readers and native integration, using synthetic fixtures."""

import json
from pathlib import Path
import sqlite3
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "client"))

from shared_memory.paths import in_project, locations, owner
from shared_memory.sources import claude_events, codex_event, jsonl_rows, sqlite_items
from shared_memory.state import sync_lock
from shared_memory.setup import toml_server


def test_native_locations_and_environment_overrides(tmp_path):
    for platform in ("win32", "darwin", "linux"):
        native = locations(
            env={
                "CODEX_HOME": str(tmp_path / "c"),
                "CLAUDE_CONFIG_DIR": str(tmp_path / "a"),
            },
            home=tmp_path,
            platform=platform,
        )
        assert native["codex"] == tmp_path / "c"
        assert native["claude"] == tmp_path / "a"
        assert native["claude_config"] == tmp_path / "a/.claude.json"
        assert native["base"] != native["codex"]


def test_windows_unc_and_longest_project_boundary():
    assert in_project(
        r"C:\Users\Person\Developer\ACME\src", ["c:/users/person/Developer/acme"]
    )
    assert in_project(r"\\server\share\repo\x", [r"\\SERVER\SHARE\repo"])
    assert not in_project("/work/acme-other", ["/work/acme"])
    assert (
        owner(
            "/work/acme/src",
            {"general": {"roots": ["/work"]}, "acme": {"roots": ["/work/acme"]}},
        )
        == "acme"
    )


def test_sqlite_updates_with_equal_ordinals_and_no_private_reasoning(tmp_path):
    path = tmp_path / "thread_history_1.sqlite"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE thread_items(thread_id,item_id,item_type,item_json,updated_at_ordinal,created_at_ms)"
        )
        for ident, kind in [
            ("a", "commandExecution"),
            ("b", "reasoning"),
            ("c", "agentMessage"),
        ]:
            db.execute(
                "INSERT INTO thread_items VALUES (?,?,?,?,?,?)",
                (
                    "t",
                    ident,
                    kind,
                    json.dumps(
                        {
                            "id": ident,
                            "type": kind,
                            "text": "visible",
                            "aggregatedOutput": "x" * 10000,
                        }
                    ),
                    1,
                    0,
                ),
            )
    first = sqlite_items(tmp_path, "t", limit=1)
    assert first[0]["item_id"] == "a"
    assert len(json.loads(first[0]["bounded_json"])["aggregatedOutput"]) == 4096
    assert [r["item_id"] for r in sqlite_items(tmp_path, "t", 1, after_id="a")] == ["c"]


def test_claude_tool_results_update_same_public_action():
    calls = {}
    start = claude_events(
        {
            "type": "assistant",
            "uuid": "a",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {
                        "type": "tool_use",
                        "id": "tool1",
                        "name": "Bash",
                        "input": {"command": "python check.py"},
                    },
                ]
            },
        },
        calls,
    )
    assert len(start) == 1
    result = claude_events(
        {
            "type": "user",
            "uuid": "b",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool1", "content": "done"}
                ]
            },
            "toolUseResult": {"exitCode": 0},
        },
        calls,
    )
    assert result[0]["event_id"] == start[0]["event_id"]
    assert result[0]["command"] == "python check.py"
    assert result[0]["outcome"] == "completed"
    assert result[0]["exit_code"] == 0
    assert codex_event({"type": "reasoning", "text": "private"}) is None


def test_redaction_expansion_cannot_exceed_the_command_contract():
    event = codex_event(
        {"id": "bounded", "type": "commandExecution", "command": "10.1.1.1 " * 1000}
    )
    assert len(event["command"]) <= 4096
    assert "10.1.1.1" not in event["command"]


def test_incomplete_jsonl_not_acknowledged_and_oversized_is_explicit(tmp_path):
    path = tmp_path / "session.jsonl"
    first = b'{"type":"user"}\n'
    path.write_bytes(first + b'{"type":')
    rows = list(jsonl_rows(path))
    assert len(rows) == 1 and rows[0][1] == len(first)
    path.write_bytes(first + b"x" * 200 + b"\n")
    assert (
        list(jsonl_rows(path, len(first), max_line=100))[0][0]["_gap"]
        == "oversized-jsonl-record"
    )


def test_hooks_and_daemon_cannot_race_cursors(tmp_path):
    with sync_lock(tmp_path / "state.sqlite") as one:
        assert one
        with sync_lock(tmp_path / "state.sqlite") as two:
            assert not two
    with sync_lock(tmp_path / "state.sqlite") as released:
        assert released


def test_codex_config_preserves_trust_and_other_tools():
    original = '[hooks.state]\ntrusted = "leave-as-is"\n[mcp_servers.context7]\ncommand="helper"\n[mcp_servers.agent-memory]\ncommand="old"\nenabled=true\n'
    result = toml_server(original, "python3", ["client.py", "mcp"])
    import tomllib

    parsed = tomllib.loads(result)
    assert parsed["hooks"]["state"]["trusted"] == "leave-as-is"
    assert parsed["mcp_servers"]["context7"]["command"] == "helper"
    assert not parsed["mcp_servers"]["agent-memory"]["enabled"]
    assert parsed["mcp_servers"]["shared-memory"]["enabled"]
    assert toml_server(result, "python3", ["client.py", "mcp"]) == result


def test_native_install_is_idempotent_and_does_not_delete_existing_hooks(
    tmp_path, monkeypatch
):
    from shared_memory import setup

    base = tmp_path / "client"
    base.mkdir()
    (base / "shared-memory.py").write_text("# launcher")
    cfg = {
        "codex_home": str(tmp_path / "codex"),
        "claude_home": str(tmp_path / "claude"),
        "projects": {"alpha": {"roots": [str(tmp_path)]}},
    }
    hooks = tmp_path / "codex/hooks.json"
    hooks.parent.mkdir()
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "existing"}]}]
                }
            }
        )
    )
    for _ in range(2):
        result = setup.install(cfg, base / "connections.json", daemon=False)
        assert result["hook_trust_changed"] is False
    stop = json.loads(hooks.read_text())["hooks"]["Stop"]
    assert len(stop) == 2 and stop[0]["hooks"][0]["command"] == "existing"


def test_failed_send_does_not_advance_source_cursor(tmp_path, monkeypatch):
    from shared_memory import sync
    from shared_memory.connection import LinkError

    registry = {
        "device_id": "fixture",
        "url": "http://127.0.0.1/handoff",
        "codex_home": str(tmp_path),
        "projects": {"alpha": {"token": "test", "roots": ["/work/alpha"]}},
    }
    thread = {
        "id": "thread",
        "cwd": "/work/alpha",
        "history_mode": "paginated",
        "title": "Task",
    }
    monkeypatch.setattr(sync, "sqlite_threads", lambda home: [thread])
    monkeypatch.setattr(
        sync,
        "sqlite_items",
        lambda home, thread, ordinal, limit, ident: (
            []
            if ordinal
            else [
                {
                    "item_id": "a",
                    "updated_at_ordinal": 1,
                    "created_at_ms": 0,
                    "source_bytes": 80,
                    "bounded_json": json.dumps(
                        {"id": "a", "type": "agentMessage", "text": "Visible result"}
                    ),
                }
            ]
        ),
    )

    class Client:
        fail = True

        def __init__(self, profile):
            self.profile = profile

        def call(self, name, args):
            if self.fail:
                raise LinkError("Unavailable")
            return {"changed_events": len(args["events"])}

    monkeypatch.setattr(sync, "Connection", Client)
    runner = sync.Sync(registry, tmp_path / "state.sqlite")
    with pytest.raises(LinkError):
        runner.codex("alpha")
    assert runner.state.get("codex-sqlite:alpha:thread") is None
    Client.fail = False
    runner.codex("alpha")
    assert runner.state.get("codex-sqlite:alpha:thread") == {
        "ordinal": 1,
        "item_id": "a",
    }
    prior = runner.report["events_changed"]
    runner.codex("alpha")
    assert runner.report["events_changed"] == prior


def test_legacy_non_utf8_is_decoded_without_changing_source(tmp_path):
    from shared_memory.legacy import import_legacy

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE memories(id,title,content,type,tags,context,created_at,updated_at)"
        )
        db.execute(
            "INSERT INTO memories VALUES (?,?,CAST(? AS TEXT),?,?,?,?,?)",
            ("one", "Title", "Пример".encode("cp1251"), "note", "", "", "2020", "2021"),
        )
    before = path.read_bytes()

    class Client:
        payload = None

        def call(self, name, args):
            if name == "knowledge_get":
                return {"record": None}
            self.payload = args
            return {"changed": True, "indexed": True}

    client = Client()
    result = import_legacy(client, path)
    assert (
        result["legacy_rows"] == 1 and result["non_utf8_fields_decoded_as_cp1251"] == 1
    )
    assert "Пример" in client.payload["statement"]
    assert client.payload["scope"] == "archive"
    assert path.read_bytes() == before


def test_document_assets_are_catalogued_without_copying_code_or_data(
    tmp_path, monkeypatch
):
    from shared_memory import __main__ as cli

    source = tmp_path / "script.sql"
    source.write_text("DO NOT COPY THIS BODY")
    captured = []

    class Importer:
        report = {"notes": 0}

        def __init__(self, *args):
            pass

        def note(self, client, path, kind, scope, source_identity=None):
            captured.append((path.read_text(), kind, source_identity))

    monkeypatch.setattr(cli, "Sync", Importer)
    monkeypatch.setattr(cli, "connection", lambda *args: None)
    result = cli.import_docs(
        {"projects": {"alpha": {}}}, tmp_path / "state", "alpha", source, "active"
    )
    assert result["source_file_references"] == 1
    assert captured[0][1] == "documentation-file" and captured[0][2] == source
    assert "DO NOT COPY" not in captured[0][0] and "SHA256:" in captured[0][0]


def test_mcp_requires_explicit_project_even_when_cwd_matches(
    tmp_path, monkeypatch, capsys
):
    from shared_memory import __main__ as cli
    import io

    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "session_context", "arguments": {}},
        },
    ]

    class Input:
        buffer = io.BytesIO(
            ("\n".join(json.dumps(x) for x in requests) + "\n").encode()
        )

    class Client:
        def rpc(self, request):
            return {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "tools": [
                        {
                            "name": "session_context",
                            "inputSchema": {"type": "object", "properties": {}},
                        }
                    ]
                },
            }

        def call(self, *args):
            raise AssertionError("An implicit project request reached the hub")

    monkeypatch.setattr(cli, "connection", lambda *args: Client())
    monkeypatch.setattr(cli.sys, "stdin", Input())
    monkeypatch.setattr(cli.os, "getcwd", lambda: "/work/alpha")
    cli.mcp({"projects": {"alpha": {"roots": ["/work/alpha"]}}}, "codex")
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert "project" in results[0]["result"]["tools"][0]["inputSchema"]["required"]
    assert results[1]["result"]["isError"]
