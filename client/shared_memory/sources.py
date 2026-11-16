"""Normalize public events and read sources incrementally, never whole rollouts."""

import hashlib
import json
from pathlib import Path
import queue
import sqlite3
import subprocess
import threading
import time

from mcp_memory_service.handoff.redact import text as redact


def compact(value, limit=4096):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    cleaned = redact(value)
    return cleaned[:limit], max(
        0, len(cleaned.encode()) - len(cleaned[:limit].encode())
    )


def user_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        x.get("text", "")
        for x in content
        if isinstance(x, dict)
        and x.get("type") in ("text", "input_text", "output_text", "inputText")
    )


def instruction_envelope(value):
    return value.lstrip().startswith(
        ("# AGENTS.md instructions", "<environment_context>", "<INSTRUCTIONS>")
    )


def result_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return user_text(value) or "[Non-text result retained in original source]"
    if isinstance(value, dict):
        if isinstance(value.get("content"), list):
            return result_text(value["content"])
        return (
            "\n".join(
                f"{k}: {v}"
                for k, v in value.items()
                if k
                in (
                    "output",
                    "stdout",
                    "stderr",
                    "text",
                    "error",
                    "message",
                    "status",
                    "exit_code",
                    "exitCode",
                )
                and isinstance(v, (str, int, float, bool))
            )
            or "Structured result fields: " + ", ".join(value.keys())[:500]
        )
    return str(value or "")


def codex_event(item, timestamp="", revision=0):
    kind = item.get("type")
    ident = str(
        item.get("id")
        or hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
    )[:160]
    event = {
        "event_id": ident,
        "source_revision": int(revision),
        "occurred_at": str(timestamp),
        "source_ref": "codex-item:" + ident,
    }
    if kind == "userMessage":
        value = user_text(item.get("content", []))
        if instruction_envelope(value):
            return None
        if not value:
            value = (
                "[User supplied non-text content; original attachments are not copied]"
            )
        event.update(kind="user_message", summary=value)
    elif kind == "agentMessage":
        event.update(
            kind="assistant_message", summary=item.get("text", ""), outcome="completed"
        )
    elif kind == "commandExecution":
        output, omitted = compact(item.get("aggregatedOutput") or "")
        code = item.get("exitCode")
        status = item.get("status")
        event.update(
            kind="command",
            command=str(item.get("command", ""))[:4096],
            summary="Command execution",
            outcome="failed"
            if code not in (None, 0)
            else "completed"
            if code == 0
            else "interrupted"
            if status == "interrupted"
            else "running",
            excerpt=output,
            omitted_bytes=omitted,
            exit_code=code,
            duration_ms=item.get("durationMs"),
        )
    elif kind == "fileChange":
        changes = item.get("changes", [])
        event.update(
            kind="file_change",
            summary=f"File changes: {len(changes)}",
            files=[str(x.get("path", ""))[:512] for x in changes[:100]],
            outcome="completed" if item.get("status") == "completed" else "unknown",
        )
    elif kind == "mcpToolCall":
        excerpt, omitted = compact(
            result_text(item.get("error") or item.get("result") or "")
        )
        event.update(
            kind="mcp_call",
            summary=f"{item.get('server', '')}.{item.get('tool', '')}",
            excerpt=excerpt,
            omitted_bytes=omitted,
            outcome="failed"
            if item.get("error")
            else "completed"
            if item.get("status") == "completed"
            else "running",
        )
    elif kind == "webSearch":
        event.update(
            kind="web_search",
            summary=str(item.get("query") or item.get("action") or "Web search"),
            outcome="completed",
        )
    elif kind == "contextCompaction":
        event.update(
            kind="compaction",
            summary="Context was compacted; the complete original model context is not available",
            outcome="completed",
        )
    elif kind == "imageView":
        event.update(
            kind="artifact",
            summary="Image viewed; original image remains in source",
            files=[str(item.get("path", ""))[:512]],
        )
    else:
        return None
    event["summary"], omitted = compact(event.get("summary", ""), 8192)
    event["omitted_bytes"] = event.get("omitted_bytes", 0) + omitted
    if "command" in event:
        event["command"], omitted = compact(event["command"], 4096)
        event["omitted_bytes"] += omitted
    return event


class CodexRPC:
    """Read-only app-server operations; never start, resume or run a model turn."""

    def __init__(self, binary="codex", codex_home=None):
        import os

        env = os.environ.copy()
        if codex_home:
            env["CODEX_HOME"] = str(codex_home)
        self.process = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.messages = queue.Queue()
        self.request_id = 0

        def read():
            try:
                for line in self.process.stdout:
                    try:
                        self.messages.put(json.loads(line))
                    except ValueError:
                        continue
            finally:
                self.messages.put(None)

        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "shared-memory-link",
                    "title": "Shared Memory Link",
                    "version": "1",
                }
            },
        )
        self.process.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n')
        self.process.stdin.flush()

    def call(self, method, params):
        self.request_id += 1
        ident = self.request_id
        self.process.stdin.write(
            json.dumps(
                {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
            )
            + "\n"
        )
        self.process.stdin.flush()
        deadline = time.monotonic() + 30
        while True:
            message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if message is None:
                raise RuntimeError("Codex history process stopped")
            if message.get("id") == ident:
                if "error" in message:
                    raise RuntimeError("Codex history operation unavailable")
                return message["result"]

    def threads(self):
        for archived in (False, True):
            cursor = None
            while True:
                result = self.call(
                    "thread/list",
                    {
                        "limit": 100,
                        "cursor": cursor,
                        "useStateDbOnly": True,
                        "sourceKinds": [],
                        "modelProviders": [],
                        "archived": archived,
                    },
                )
                yield from result.get("data", [])
                cursor = result.get("nextCursor")
                if not cursor:
                    break

    def items(self, thread_id, cursor=None):
        result = self.call(
            "thread/items/list",
            {
                "threadId": thread_id,
                "limit": 50,
                "cursor": cursor,
                "sortDirection": "asc",
            },
        )
        return result

    def close(self):
        try:
            self.process.stdin.close()
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def database(home, prefix):
    candidates = list(Path(home).glob(prefix + "_*.sqlite"))
    return max(candidates, key=lambda p: int(p.stem.rsplit("_", 1)[-1]), default=None)


def sqlite_threads(codex_home):
    filename = database(codex_home, "state")
    if filename is None:
        # Older CLI installations only have rollouts. Read metadata, not blobs.
        result = []
        for directory in ("sessions", "archived_sessions"):
            for path in (Path(codex_home) / directory).rglob("*.jsonl"):
                for row, _ in jsonl_rows(path, max_records=5):
                    if row.get("type") == "session_meta":
                        meta = row.get("payload") or {}
                        if meta.get("id") and meta.get("cwd"):
                            result.append(
                                dict(
                                    meta,
                                    rollout_path=str(path),
                                    title="Legacy Codex session",
                                    history_mode="legacy",
                                )
                            )
                        break
        return result
    with sqlite3.connect(filename.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        columns = {x[1] for x in db.execute("PRAGMA table_info(threads)")}
        required = {"id", "cwd", "rollout_path"}
        if not required <= columns:
            raise RuntimeError(
                "Unsupported Codex metadata schema; app-server or JSONL fallback required"
            )
        optional = ",".join(
            field
            if field in columns
            else "'legacy' AS " + field
            if field == "history_mode"
            else "'' AS " + field
            for field in ("updated_at_ms", "title", "history_mode")
        )
        return [
            dict(x)
            for x in db.execute(
                "SELECT id,cwd,rollout_path," + optional + " FROM threads"
            )
        ]


def sqlite_items(codex_home, thread_id, after=0, limit=50, after_id=""):
    filename = database(codex_home, "thread_history")
    if filename is None:
        raise RuntimeError("Paginated Codex history database is unavailable")
    with sqlite3.connect(filename.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        required = {
            "thread_id",
            "item_id",
            "item_type",
            "item_json",
            "updated_at_ordinal",
        }
        if not required <= {
            x[1] for x in db.execute("PRAGMA table_info(thread_items)")
        }:
            raise RuntimeError("Unsupported Codex history schema")
        # Exclude private reasoning before loading payloads into the client.
        rows = db.execute(
            """SELECT item_id,updated_at_ordinal,created_at_ms,
            CASE WHEN item_type='commandExecution' THEN
              json_set(item_json,'$.aggregatedOutput',substr(COALESCE(json_extract(item_json,'$.aggregatedOutput'),''),1,4096))
            WHEN item_type='mcpToolCall' THEN
              json_set(json_remove(item_json,'$.arguments'),'$.result',substr(COALESCE((SELECT group_concat(json_extract(value,'$.text'),char(10)) FROM json_each(item_json,'$.result.content') WHERE json_extract(value,'$.type')='text'),json_extract(item_json,'$.result.output'),json_extract(item_json,'$.error.message'),'[Non-text or structured result retained at source]'),1,4096))
            WHEN item_type='userMessage' THEN
              json_set(item_json,'$.content',json(COALESCE((SELECT json_group_array(json_object('type','text','text',json_extract(value,'$.text'))) FROM json_each(item_json,'$.content') WHERE json_extract(value,'$.type') IN ('text','input_text','inputText')),'[]')))
            WHEN item_type='fileChange' THEN
              json_set(item_json,'$.changes',json(COALESCE((SELECT json_group_array(json_object('path',json_extract(value,'$.path'))) FROM json_each(item_json,'$.changes')),'[]')))
            ELSE item_json END AS bounded_json,
            LENGTH(item_json) AS source_bytes
            FROM thread_items WHERE thread_id=? AND (updated_at_ordinal>? OR (updated_at_ordinal=? AND item_id>?))
            AND item_type IN ('userMessage','agentMessage','commandExecution','fileChange','mcpToolCall','webSearch','contextCompaction','imageView')
            ORDER BY updated_at_ordinal,item_id LIMIT ?""",
            (thread_id, after, after, after_id, limit),
        ).fetchall()
    return [dict(x) for x in rows]


def jsonl_rows(path, offset=0, max_line=8 * 1024 * 1024, max_records=200):
    """Skip oversized records in bounded reads, preserving resumable byte offsets."""
    with Path(path).open("rb") as source:
        source.seek(offset)
        for _ in range(max_records):
            start = source.tell()
            raw = source.readline(max_line + 1)
            if not raw:
                break
            if len(raw) > max_line:
                while raw and not raw.endswith(b"\n"):
                    raw = source.readline(max_line)
                yield {"_gap": "oversized-jsonl-record"}, source.tell()
                continue
            if not raw.endswith(b"\n"):
                source.seek(start)
                break
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                row = {"_gap": "invalid-jsonl-record"}
            yield row, source.tell()


def claude_events(row, calls=None):
    calls = {} if calls is None else calls
    kind = row.get("type")
    message = row.get("message") or {}
    if kind not in ("user", "assistant") or not isinstance(message, dict):
        return []
    parts = message.get("content", [])
    if isinstance(parts, str):
        parts = [{"type": "text", "text": parts}]
    ident = str(
        row.get("uuid")
        or message.get("id")
        or hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    )[:100]
    output = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        subtype = part.get("type")
        event = {
            "event_id": f"{ident}:{index}",
            "occurred_at": str(row.get("timestamp") or ""),
            "source_ref": "claude-item:" + ident,
        }
        if subtype == "text":
            value = part.get("text", "")
            if not value or instruction_envelope(value):
                continue
            value, omitted = compact(value, 8192)
            event.update(
                kind="user_message" if kind == "user" else "assistant_message",
                summary=value,
                omitted_bytes=omitted,
            )
        elif subtype == "tool_use":
            name = part.get("name", "")
            inputs = part.get("input") or {}
            command = inputs.get("command", "") if isinstance(inputs, dict) else ""
            event.update(
                kind="command"
                if name in ("Bash", "bash")
                else "file_change"
                if name in ("Edit", "Write", "MultiEdit")
                else "mcp_call",
                summary="Tool: " + name,
                command=compact(command)[0],
                outcome="running",
            )
            if isinstance(inputs, dict) and inputs.get("file_path"):
                event["files"] = [str(inputs["file_path"])[:512]]
            if part.get("id"):
                event["event_id"] = "call:" + str(part["id"])
                calls[part["id"]] = {
                    k: v
                    for k, v in event.items()
                    if k in ("kind", "command", "summary", "files")
                }
        elif subtype == "tool_result":
            value, omitted = compact(result_text(part.get("content", "")))
            old = calls.get(part.get("tool_use_id"), {})
            event.update(
                kind="mcp_call",
                summary="Tool result: " + str(part.get("tool_use_id", "")),
                excerpt=value,
                omitted_bytes=omitted,
                outcome="failed" if part.get("is_error") else "completed",
            )
            event.update(old)
            if part.get("tool_use_id"):
                event["event_id"] = "call:" + str(part["tool_use_id"])
            result = row.get("toolUseResult") or {}
            if isinstance(result, dict):
                code = result.get("exitCode", result.get("exit_code"))
                if isinstance(code, int):
                    event["exit_code"] = code
                if code not in (None, 0) or result.get("interrupted"):
                    event["outcome"] = "failed" if code else "interrupted"
        else:
            # thinking/redacted_thinking are deliberately not portable history.
            continue
        output.append(event)
    return output
