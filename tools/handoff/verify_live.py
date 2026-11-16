#!/usr/bin/env python3
"""Synthetic end-to-end check against a separately provisioned test project."""

import argparse
import asyncio
import json
import sys


async def check(args):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def call(session, tool, payload):
        result = await session.call_tool(tool, dict(payload, project=args.project))
        if result.isError:
            raise RuntimeError("Structured MCP operation failed: " + tool)
        return json.loads(result.content[0].text)

    async def client(agent):
        return StdioServerParameters(
            command=sys.executable,
            args=[args.launcher, "--registry", args.registry, "mcp", "--agent", agent],
        )

    async with stdio_client(await client("codex")) as streams:
        async with ClientSession(*streams) as codex:
            await codex.initialize()
            listing = await codex.list_tools()
            assert "section_list" in {x.name for x in listing.tools}
            current = await call(codex, "session_context", {"task_id": "handoff-smoke"})
            existing = next(
                (t for t in current["tasks"] if t["task_id"] == "handoff-smoke"), None
            )
            written = await call(
                codex,
                "task_checkpoint",
                {
                    "task_id": "handoff-smoke",
                    "title": "Cross-client smoke check",
                    "objective": "Verify shared task continuation",
                    "state": "Codex prepared a task for another client",
                    "next_steps": [
                        "Read the checkpoint through an independent Claude MCP connection"
                    ],
                    "sections": ["memory", "web"],
                    "base_revision": existing["revision"] if existing else 0,
                },
            )
            assert written["indexed"]
            text = (
                ("Справочная таблица результатов проверок. " * 160)
                + "\nРабочие исполнители сборок доверяют корпоративному удостоверяющему центру. Для защиты от подмены соединения устанавливают корневой сертификат в хранилище доверия контейнера; проверку TLS не отключают."
            )
            old = await call(
                codex, "knowledge_get", {"record_id": "long-tail-semantic"}
            )
            fact = await call(
                codex,
                "knowledge_record",
                {
                    "record_id": "long-tail-semantic",
                    "title": "Справочные проверки",
                    "statement": text,
                    "sections": ["web"],
                    "status": "verified",
                    "base_revision": old["record"]["revision"] if old["record"] else 0,
                },
            )
            assert fact["indexed"]
            result = await call(
                codex,
                "knowledge_search",
                {
                    "query": "How do build executors prevent man-in-the-middle attacks?",
                    "context": "trust chain and certificate authority",
                    "sections": ["web"],
                },
            )
            assert result["stage"] == "context", result["trace"]
            assert "long-tail-semantic" in {
                r.get("record_id") for r in result["records"]
            }
    async with stdio_client(await client("claude")) as streams:
        async with ClientSession(*streams) as claude:
            await claude.initialize()
            read = await call(claude, "session_context", {"task_id": "handoff-smoke"})
            task = read["tasks"][0]
            assert task["agent"] == "codex"
            changed = await call(
                claude,
                "task_checkpoint",
                {
                    key: value
                    for key, value in dict(
                        task,
                        agent="claude",
                        state="Independent Claude MCP connection received the task",
                        base_revision=task["revision"],
                    ).items()
                    if key not in ("project", "revision", "updated_at")
                },
            )
            assert changed["record"]["revision"] > task["revision"]
    return {
        "stdio_mcp_clients": 2,
        "cross_client_checkpoint": "passed",
        "genuine_model_long_tail_retrieval": "passed",
        "semantic_stage": result["stage"],
        "index_fragments": fact.get("index_fragments"),
        "project": args.project,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--launcher", required=True)
    parser.add_argument("--project", default="integration-checks")
    print(json.dumps(asyncio.run(check(parser.parse_args()))))
