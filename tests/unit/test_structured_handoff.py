import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from mcp_memory_service.handoff.models import CONTRACTS
from mcp_memory_service.handoff.store import HandoffStore, RevisionConflict


@pytest.fixture
def store(tmp_path):
    return HandoffStore(tmp_path / "handoff-test.db")


def checkpoint(**kwargs):
    values = {
        "project": "alpha",
        "task_id": "task-1",
        "title": "A task",
        "objective": "Fix the export",
        "state": "Investigating",
    }
    values.update(kwargs)
    return CONTRACTS["task_checkpoint"][0].model_validate(values).model_dump()


def batch(**kwargs):
    values = {
        "project": "alpha",
        "session_id": "codex:one",
        "agent": "codex",
        "source": "fixture",
        "task_id": "task-1",
        "events": [
            {
                "event_id": "e1",
                "kind": "command",
                "command": "python check.py",
                "outcome": "running",
                "source_revision": 1,
            }
        ],
    }
    values.update(kwargs)
    return CONTRACTS["session_ingest"][0].model_validate(values).model_dump()


def test_explicit_checkpoint_cross_agent_and_revision_conflict(store):
    first = store.record("task_checkpoint", checkpoint(agent="codex"))
    assert first["record"]["revision"] == 1
    context = store.context("alpha", "task-1")
    assert context["tasks"][0]["state"] == "Investigating"
    assert not store.context("beta")["tasks"]
    second = store.record(
        "task_checkpoint", checkpoint(agent="claude", state="Fixed", base_revision=1)
    )
    assert second["record"]["revision"] == 2
    with pytest.raises(RevisionConflict):
        store.record(
            "task_checkpoint",
            checkpoint(agent="codex", state="Outdated", base_revision=1),
        )
    with store.connection() as db:
        old = db.execute(
            "SELECT payload_json FROM hf_record_versions WHERE project='alpha'"
        ).fetchone()
    assert json.loads(old[0])["state"] == "Investigating"


def test_idempotency_and_incremental_completed_command(store):
    first = batch()
    assert store.ingest(first)["changed_events"] == 1
    assert store.ingest(first)["changed_events"] == 0
    watermark = store.history("alpha")["next_after"]
    second = batch()
    second["events"][0].update(outcome="completed", exit_code=0, source_revision=2)
    assert store.ingest(second)["changed_events"] == 1
    changed = store.history("alpha", after=watermark)
    assert changed["events"][0]["exit_code"] == 0
    assert store.ingest(first)["changed_events"] == 0
    assert len(store.history("alpha")["events"]) == 1


def test_atomic_batch_cursor_and_ownership(store):
    store.ingest(batch(cursor="one"))
    with pytest.raises(ValueError):
        store.ingest(batch(cursor="two", agent="claude"))
    assert store.context("alpha")["sessions"][0]["cursor"] == "one"
    assert store.history("beta")["events"] == []


def test_concurrent_checkpoint_writers_have_one_winner(store):
    store.record("task_checkpoint", checkpoint())

    def update(agent):
        try:
            return store.record(
                "task_checkpoint", checkpoint(agent=agent, state=agent, base_revision=1)
            )["record"]["revision"]
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(update, ["codex", "claude"]))
    assert sorted(map(str, results)) == ["2", "conflict"]


def test_sources_never_promote_themselves_to_verified_state(store):
    store.ingest(batch(gaps=["Attachments unavailable"]))
    result = store.context("alpha")
    assert result["completeness"] == "partial-observations-only"
    assert result["tasks"] == []
    assert result["sessions"][0]["gaps"] == ["Attachments unavailable"]


def test_hidden_reasoning_and_large_payload_rejected():
    with pytest.raises(ValidationError):
        batch(
            events=[
                {"event_id": "hidden", "kind": "reasoning", "summary": "not allowed"}
            ]
        )
    with pytest.raises(ValidationError):
        batch(events=[{"event_id": "large", "kind": "command", "excerpt": "a" * 4097}])


def test_redaction_occurs_before_storage_and_version_history(store):
    store.record(
        "task_checkpoint",
        checkpoint(
            state="password=example-value Bearer abcdef123456 request http://user:pass@example.org 10.1.2.3"
        ),
    )
    with store.connection() as db:
        raw = db.execute("SELECT state FROM hf_checkpoints").fetchone()[0]
    for secret in ["example-value", "abcdef123456", "user:pass", "10.1.2.3"]:
        assert secret not in raw
    assert "redacted" in raw


def test_verifications_and_decisions_are_scoped(store):
    verification = (
        CONTRACTS["verification_record"][0]
        .model_validate(
            {
                "project": "alpha",
                "record_id": "v1",
                "scope": "export",
                "outcome": "passed",
                "exit_code": 1,
                "evidence": "exit",
            }
        )
        .model_dump()
    )
    with pytest.raises(ValueError):
        store.record("verification_record", verification)
    verification.update(exit_code=0)
    store.record("verification_record", verification)
    invalid = checkpoint(project="beta", verification_ids=["v1"])
    with pytest.raises(ValueError):
        store.record("task_checkpoint", invalid)


def test_archive_is_opt_in_and_newer_versions_are_preserved(store):
    model = CONTRACTS["knowledge_record"][0]
    for scope in ["active", "archive"]:
        row = model.model_validate(
            {
                "project": "alpha",
                "record_id": scope,
                "title": "runner",
                "statement": "Old Jenkins" if scope == "archive" else "Current Runner",
                "scope": scope,
            }
        ).model_dump()
        store.record("knowledge_record", row)
    assert [r["record_id"] for r in store.keyword_search("alpha", "runner")] == [
        "active"
    ]
    assert len(store.keyword_search("alpha", "runner", True)) == 2
    assert store.keyword_search("beta", "runner", True) == []


@pytest.fixture
def api(tmp_path, monkeypatch, store):
    from mcp_memory_service.web.api import handoff

    cfg = tmp_path / "projects.json"
    cfg.write_text(
        json.dumps(
            {
                "projects": {
                    "alpha": {
                        "token_sha256": hashlib.sha256(
                            b"alpha-test-credential"
                        ).hexdigest()
                    },
                    "beta": {
                        "token_sha256": hashlib.sha256(
                            b"beta-test-credential"
                        ).hexdigest()
                    },
                }
            }
        )
    )
    monkeypatch.setenv("MCP_HANDOFF_TOKENS_FILE", str(cfg))

    class FakeService:
        async def call(self, name, args):
            data = CONTRACTS[name][0].model_validate(args).model_dump()
            if name == "session_context":
                data.pop("query")
                data.pop("max_chars")
                return store.context(**data)
            return store.record(name, data)

    async def fake_service(server):
        return FakeService()

    monkeypatch.setattr(handoff, "service", fake_service)
    monkeypatch.setattr(handoff, "_get_memory_server", lambda: None)
    app = FastAPI()
    app.include_router(handoff.router)
    return TestClient(app)


def rpc(client, name, args, token="alpha-test-credential"):
    return client.post(
        "/handoff/mcp",
        headers={"Authorization": "Bearer " + token},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )


def test_remote_project_credential_cannot_cross_boundary(api):
    assert rpc(api, "session_context", {"project": "beta"}).status_code == 403
    assert rpc(api, "session_context", {}, "invalid").status_code == 401
    assert (
        api.post(
            "/handoff/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        ).status_code
        == 401
    )
    written = rpc(
        api,
        "task_checkpoint",
        {"task_id": "t", "title": "one", "objective": "fix", "state": "done"},
    )
    assert written.json()["result"]["isError"] is False
    read = rpc(api, "session_context", {}, "beta-test-credential")
    assert json.loads(read.json()["result"]["content"][0]["text"])["tasks"] == []


def test_protocol_lists_only_structured_tools_without_scope_selector(api):
    result = api.post(
        "/handoff/mcp",
        headers={"Authorization": "Bearer alpha-test-credential"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    listing = result.json()["result"]["tools"]
    assert {r["name"] for r in listing} == set(CONTRACTS)
    assert all("project" not in r["inputSchema"]["properties"] for r in listing)
    assert rpc(api, "memory_delete", {}).json()["result"]["isError"]


def test_bad_request_does_not_echo_input_secrets(api):
    result = rpc(
        api,
        "task_checkpoint",
        {
            "task_id": "t",
            "title": "one",
            "objective": "fix",
            "state": "done",
            "unexpected": "secret-value",
        },
    )
    assert result.json()["result"]["isError"]
    assert "secret-value" not in result.text


def test_physical_sections_multiple_links_and_action_search(store):
    store.record(
        "task_checkpoint",
        checkpoint(
            topics=["web", "database"], state="Web runner exports the database snapshot"
        ),
    )
    with store.connection() as db:
        sections = {
            r[0]
            for r in db.execute(
                "SELECT section_id FROM hf_section_entries WHERE entity_type='hf_checkpoints' AND is_current=1"
            )
        }
    assert {"web", "database"} <= sections
    assert store.section_search("alpha", "runner exports", ["web"])
    store.ingest(
        batch(
            events=[
                {
                    "event_id": "error",
                    "kind": "command",
                    "command": "task-runner deploy.plan",
                    "excerpt": "missing inventory",
                    "outcome": "failed",
                    "exit_code": 2,
                }
            ]
        )
    )
    found = store.section_search("alpha", "missing inventory", ["operations"])
    assert found[0]["record_type"] == "event" and found[0]["exit_code"] == 2


@pytest.mark.asyncio
async def test_search_order_stops_when_section_or_keywords_succeed(tmp_path):
    from mcp_memory_service.handoff.service import HandoffService

    class Storage:
        db_path = tmp_path / "ordered.db"
        calls = 0

        async def retrieve(self, *args, **kwargs):
            self.calls += 1
            return []

    storage = Storage()
    service = HandoffService(storage, None)
    service.records.record(
        "task_checkpoint",
        checkpoint(topics=["web"], state="runner certificate failure"),
    )
    first = await service.staged_search("alpha", "runner certificate", topics=["web"])
    assert first["stage"] == "section" and len(first["trace"]) == 1
    second = await service.staged_search(
        "alpha", "runner certificate", topics=["operations"]
    )
    assert second["stage"] == "keywords" and len(second["trace"]) == 2
    assert storage.calls == 0
    empty = await service.staged_search(
        "alpha", "ocean submarine", topics=["operations"], context="maritime"
    )
    assert [x["stage"] for x in empty["trace"]] == ["section", "keywords", "context"]
    assert storage.calls == 1 and empty["records"] == []
    assert (await service.staged_search("beta", "runner certificate"))["records"] == []


def test_stale_semantic_fragments_are_never_current(store):
    store.record("task_checkpoint", checkpoint())
    store.set_vectors("alpha", "task-1", ["old"], "active", "hf_checkpoints", 1)
    assert store.knowledge_by_hashes("alpha", ["old"])
    store.record("task_checkpoint", checkpoint(state="new state", base_revision=1))
    assert store.knowledge_by_hashes("alpha", ["old"]) == []
    with pytest.raises(RevisionConflict):
        store.set_vectors("alpha", "task-1", ["late"], "active", "hf_checkpoints", 1)


@pytest.mark.asyncio
async def test_project_private_redaction_precedes_all_persistence(
    tmp_path, monkeypatch
):
    from mcp_memory_service.handoff.service import HandoffService

    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "projects": {
                    "alpha": {
                        "redact_patterns": [
                            r"(?i)\b(?:[a-z]+\.)*private-example\.test\b"
                        ]
                    }
                }
            }
        )
    )
    monkeypatch.setenv("MCP_HANDOFF_TOKENS_FILE", str(policy))

    class Storage:
        db_path = tmp_path / "private.db"

    service = HandoffService(Storage(), None)
    result = await service.call(
        "task_checkpoint", dict(checkpoint(state="Contact runner.private-example.test"))
    )
    assert "private-example.test" not in result["record"]["state"]
