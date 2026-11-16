"""Incremental export with per-source acknowledgements and explicit coverage."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .connection import Connection, LinkError
from .paths import expanded_roots, in_project, locations, owner
from .sources import (
    claude_events,
    codex_event,
    compact,
    instruction_envelope,
    jsonl_rows,
    sqlite_items,
    sqlite_threads,
    user_text,
)
from .state import State


def stamp(value):
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    return str(value or "")


def scoped_profile(registry, name):
    config = registry["projects"][name]
    return dict(
        config,
        project=name,
        url=registry["url"],
        codex_home=registry.get("codex_home"),
        claude_home=registry.get("claude_home"),
    )


def codex_import_sources(home):
    path = Path(home) / "external_agent_session_imports.json"
    if not path.is_file():
        return {}
    try:
        records = json.loads(path.read_text()).get("records", [])
    except (OSError, ValueError):
        return {}
    return {r["imported_thread_id"]: r for r in records if r.get("imported_thread_id")}


def ignored_codex_imports(home):
    records = codex_import_sources(home).values()
    # Prefer the original Claude source; don't import its converted Codex copy.
    return {
        r["imported_thread_id"]
        for r in records
        if r.get("imported_thread_id") and Path(r.get("source_path", "")).is_file()
    }


class Sync:
    def __init__(self, registry, state_path):
        self.registry = registry
        self.state = State(state_path)
        self.device = registry["device_id"]
        self.report = {
            "sessions": 0,
            "events_sent": 0,
            "events_changed": 0,
            "sources": 0,
            "notes": 0,
            "gaps": {},
            "errors": [],
        }

    def gap(self, name):
        self.report["gaps"][name] = self.report["gaps"].get(name, 0) + 1

    def send(self, connection, session, events, cursor, gaps=()):
        session = dict(session, sections=connection.profile.get("default_sections", []))
        payload = dict(
            session,
            events=events,
            cursor=str(cursor)[:512],
            coverage="partial",
            gaps=sorted(set(gaps))[:100],
        )
        if (
            len(json.dumps(payload, ensure_ascii=False).encode()) > 700 * 1024
            and len(events) > 1
        ):
            middle = len(events) // 2
            self.send(connection, session, events[:middle], cursor, gaps)
            self.send(connection, session, events[middle:], cursor, gaps)
            return
        result = connection.call("session_ingest", payload)
        self.report["events_sent"] += len(events)
        self.report["events_changed"] += result["changed_events"]

    def codex(self, name):
        profile = scoped_profile(self.registry, name)
        native = locations(profile)
        connection = Connection(profile)
        imported = ignored_codex_imports(native["codex"])
        for thread in sqlite_threads(native["codex"]):
            if (
                thread["id"] in imported
                or owner(thread["cwd"], self.registry["projects"]) != name
            ):
                continue
            self.report["sources"] += 1
            if thread.get("history_mode") != "paginated":
                self.legacy_codex(connection, thread)
                continue
            source = f"codex-sqlite:{name}:{thread['id']}"
            position = self.state.get(source, {"ordinal": 0, "item_id": ""})
            session = {
                "session_id": f"codex:{self.device}:{thread['id']}",
                "agent": "codex",
                "source": "codex-paginated-history",
                "title": compact(thread.get("title", ""), 512)[0],
            }
            exported = False
            while True:
                rows = sqlite_items(
                    native["codex"],
                    thread["id"],
                    position["ordinal"],
                    32,
                    position["item_id"],
                )
                if not rows:
                    break
                events = []
                for row in rows:
                    try:
                        item = json.loads(row["bounded_json"])
                    except ValueError:
                        self.gap("invalid-codex-item")
                        continue
                    event = codex_event(
                        item, stamp(row.get("created_at_ms")), row["updated_at_ordinal"]
                    )
                    if event:
                        event["omitted_bytes"] += max(
                            0, row["source_bytes"] - len(row["bounded_json"].encode())
                        )
                        events.append(event)
                last = rows[-1]
                next_position = {
                    "ordinal": last["updated_at_ordinal"],
                    "item_id": last["item_id"],
                }
                self.send(
                    connection,
                    session,
                    events,
                    json.dumps(next_position),
                    [
                        "Private reasoning/system instructions excluded",
                        "Large outputs bounded; original source retained",
                        "File changes do not synchronize the working tree",
                    ],
                )
                self.state.set(source, next_position)
                position = next_position
                exported = True
            if exported:
                self.report["sessions"] += 1

    def legacy_codex(self, connection, thread):
        path = Path(thread.get("rollout_path", ""))
        if not path.is_file():
            self.gap("missing-legacy-codex-rollout")
            return
        source = f"codex-jsonl:{connection.profile['project']}:{thread['id']}"
        position = self.state.get(source, {"offset": 0})
        original = codex_import_sources(locations(connection.profile)["codex"]).get(
            thread["id"], {}
        )
        converted_claude = (
            "/.claude/" in original.get("source_path", "").replace("\\", "/").lower()
        )
        session = {
            "session_id": f"codex:{self.device}:{thread['id']}",
            "agent": "claude" if converted_claude else "codex",
            "source": "codex-converted-claude-jsonl"
            if converted_claude
            else "codex-legacy-jsonl",
            "title": compact(thread.get("title", ""), 512)[0],
        }
        count = 0
        while True:
            rows = list(jsonl_rows(path, position["offset"], max_records=100))
            if not rows:
                break
            events = []
            gaps = [
                "Legacy history has visible messages only; original action records may be unavailable"
            ]
            if converted_claude:
                gaps.append(
                    "Claude transcript imported by Codex; original Claude file unavailable"
                )
            for row, offset in rows:
                if row.get("_gap"):
                    gaps.append(row["_gap"])
                    self.gap(row["_gap"])
                    continue
                payload = row.get("payload") or {}
                if (
                    row.get("type") == "response_item"
                    and payload.get("type") == "message"
                    and payload.get("role") in ("user", "assistant")
                ):
                    value = user_text(payload.get("content", []))
                    if (
                        not value
                        or instruction_envelope(value)
                        or payload.get("channel") == "analysis"
                    ):
                        continue
                    summary, omitted = compact(value, 8192)
                    events.append(
                        {
                            "event_id": f"offset:{offset}",
                            "source_revision": offset,
                            "kind": "user_message"
                            if payload["role"] == "user"
                            else "assistant_message",
                            "summary": summary,
                            "omitted_bytes": omitted,
                            "occurred_at": stamp(row.get("timestamp")),
                            "source_ref": "codex-rollout-offset:" + str(offset),
                        }
                    )
                elif row.get("type") == "compacted":
                    events.append(
                        {
                            "event_id": f"offset:{offset}",
                            "kind": "compaction",
                            "summary": "Legacy context compaction",
                            "occurred_at": stamp(row.get("timestamp")),
                        }
                    )
            # Bound outbound requests independently of the number of skipped rows.
            for start in range(0, len(events), 32):
                self.send(
                    connection,
                    session,
                    events[start : start + 32],
                    str(rows[-1][1]),
                    gaps,
                )
            if not events:
                self.send(connection, session, [], str(rows[-1][1]), gaps)
            position = {"offset": rows[-1][1]}
            self.state.set(source, position)
            count += len(events)
        if count:
            self.report["sessions"] += 1

    def claude(self, name):
        profile = scoped_profile(self.registry, name)
        native = locations(profile)
        roots = expanded_roots(profile)
        connection = Connection(profile)
        project_dir = native["claude"] / "projects"
        if not project_dir.is_dir():
            return
        for path in project_dir.rglob("*.jsonl"):
            if "memory" in path.parts:
                continue
            source = (
                "claude-jsonl:"
                + name
                + ":"
                + hashlib.sha256(str(path).encode()).hexdigest()
            )
            position = self.state.get(source, {"offset": 0, "cwd": ""})
            if position["cwd"] and not in_project(position["cwd"], roots):
                continue
            # Cache actual cwd, never decode hyphenated directory names as paths.
            if not position["cwd"]:
                meta_key = (
                    "claude-cwd:" + hashlib.sha256(str(path).encode()).hexdigest()
                )
                cached = self.state.get(meta_key)
                if cached:
                    position["cwd"] = cached
                else:
                    for row, _ in jsonl_rows(path, max_records=200):
                        cwd = row.get("cwd")
                        if cwd:
                            position["cwd"] = cwd
                            self.state.set(meta_key, cwd)
                            break
                if (
                    not position["cwd"]
                    or owner(position["cwd"], self.registry["projects"]) != name
                ):
                    continue
            if owner(position["cwd"], self.registry["projects"]) != name:
                continue
            self.report["sources"] += 1
            session = {
                "session_id": f"claude:{self.device}:{path.stem}"[:160],
                "agent": "claude",
                "source": "claude-jsonl",
                "title": "Claude session",
            }
            changed = False
            if path.stat().st_size < position["offset"]:
                position["offset"] = 0
                self.gap("claude-source-replaced-or-truncated")
            while True:
                rows = list(jsonl_rows(path, position["offset"], max_records=50))
                if not rows:
                    break
                events = []
                gaps = [
                    "Private thinking/system instructions excluded",
                    "Tool output excerpts bounded",
                ]
                for row, offset in rows:
                    if row.get("_gap"):
                        gaps.append(row["_gap"])
                        self.gap(row["_gap"])
                        continue
                    calls = position.setdefault("calls", {})
                    for event in claude_events(row, calls):
                        event["source_revision"] = offset
                        events.append(event)
                    if len(calls) > 200:
                        position["calls"] = dict(list(calls.items())[-200:])
                for start in range(0, len(events), 32):
                    self.send(
                        connection,
                        session,
                        events[start : start + 32],
                        str(rows[-1][1]),
                        gaps,
                    )
                if not events:
                    self.send(connection, session, [], str(rows[-1][1]), gaps)
                position["offset"] = rows[-1][1]
                self.state.set(source, position)
                changed = True
            if changed:
                self.report["sessions"] += 1

    def note(self, connection, path, source_type, scope="active", source_identity=None):
        raw = path.read_text(encoding="utf-8")
        content_fingerprint = hashlib.sha256(raw.encode()).hexdigest()
        fingerprint = hashlib.sha256(
            (
                content_fingerprint
                + scope
                + json.dumps(connection.profile.get("default_sections", []))
            ).encode()
        ).hexdigest()
        identity = Path(source_identity or path)
        canonical = (
            "claude-memory/" if source_type == "claude-memory" else "documentation/"
        ) + identity.name
        if source_type == "claude-memory":
            folder = identity.parent.parent.name
            for root in sorted(expanded_roots(connection.profile), key=len):
                encoded_root = re.sub(r"[^A-Za-z0-9]", "-", root)
                if folder.casefold().startswith(encoded_root.casefold()):
                    folder = folder[len(encoded_root) :].strip("-") or "root"
                    break
            canonical = "claude-memory/" + folder.casefold() + "/" + identity.name
        for root in expanded_roots(connection.profile):
            try:
                canonical = str(identity.relative_to(root)).replace("\\", "/")
                break
            except ValueError:
                pass
        key = (
            "note:"
            + connection.profile["project"]
            + ":"
            + hashlib.sha256(canonical.encode()).hexdigest()
        )
        previous = self.state.get(key, {})
        if previous.get("sha256") == fingerprint:
            return
        # Paragraph chunks are bounded, preserve heading/source identity, and
        # are imported reference material rather than accepted project rules.
        title = path.stem
        body = raw
        if raw.startswith("---\n"):
            pieces = raw.split("---", 2)
            if len(pieces) == 3:
                header, body = pieces[1:]
                found = re.search(r"^name:\s*(.+)$", header, re.M)
                if found:
                    title = found[1].strip()
        chunks = []
        heading = title
        buffer = []
        size = 0
        for line in body.splitlines():
            if line.startswith("#") and buffer:
                chunks.append((heading, "\n".join(buffer)))
                buffer = []
                size = 0
            if line.startswith("#"):
                heading = line.lstrip("# ")[:300]
            for piece in [
                line[i : i + 3000] for i in range(0, max(1, len(line)), 3000)
            ]:
                if size + len(piece) > 6000 and buffer:
                    chunks.append((heading, "\n".join(buffer)))
                    buffer = []
                    size = 0
                buffer.append(piece)
                size += len(piece) + 1
        if buffer:
            chunks.append((heading, "\n".join(buffer)))
        revisions = previous.get("revisions", {})
        needs_index = False
        occurrences = {}
        current = set()
        for index, (heading, statement) in enumerate(chunks):
            if not statement.strip():
                continue
            number = occurrences.get(heading, 0)
            occurrences[heading] = number + 1
            record_id = (
                "source:"
                + hashlib.sha256(
                    (canonical + ":" + heading + ":" + str(number)).encode()
                ).hexdigest()[:40]
            )
            current.add(record_id)
            args = {
                "record_id": record_id,
                "title": compact(title + " / " + heading, 512)[0],
                "statement": compact(statement, 8192)[0],
                "category": "project_note"
                if source_type == "claude-memory"
                else "document",
                "scope": scope,
                "status": "imported",
                "source": source_type + ":" + canonical,
                "source_revision": content_fingerprint,
                "agent": "importer",
                "observed_at": stamp(identity.stat().st_mtime * 1000),
                "base_revision": revisions.get(record_id, 0),
                "sections": connection.profile.get("default_sections", []),
            }
            try:
                result = connection.call("knowledge_record", args)
            except LinkError as exc:
                if exc.data.get("error") != "revision_conflict":
                    raise
                old = connection.call("knowledge_get", {"record_id": record_id})[
                    "record"
                ]
                if (
                    not old
                    or old["status"] != "imported"
                    or old["source"] != args["source"]
                ):
                    self.gap("import-needs-reviewed-record-merge")
                    continue
                if old["observed_at"] > args["observed_at"]:
                    self.gap("older-source-not-applied")
                    continue
                args["base_revision"] = old["revision"]
                result = connection.call("knowledge_record", args)
            revisions[record_id] = result["record"]["revision"]
            if result.get("indexed") is False:
                needs_index = True
                self.gap("semantic-index-retry-needed")
            # Each acknowledgement survives a failure halfway through the file.
            self.state.set(key, {"sha256": "", "revisions": revisions})
            self.report["notes"] += int(result.get("changed", False))
        for removed in set(revisions) - current:
            old = connection.call("knowledge_get", {"record_id": removed})["record"]
            if old and old["status"] == "imported":
                payload = {
                    k: v
                    for k, v in old.items()
                    if k not in ("revision", "updated_at", "content_hash", "digest")
                }
                payload.update(status="superseded", base_revision=old["revision"])
                connection.call("knowledge_record", payload)
        self.state.set(
            key, {"sha256": "" if needs_index else fingerprint, "revisions": revisions}
        )

    def claude_notes(self, name):
        profile = scoped_profile(self.registry, name)
        native = locations(profile)
        roots = expanded_roots(profile)
        connection = Connection(profile)
        for memory in (native["claude"] / "projects").glob("*/memory"):
            # Match Claude's directory encoding against explicitly configured roots.
            matches = memory.parent.name in profile.get(
                "memory_directories", []
            ) or any(
                memory.parent.name == re.sub(r"[^A-Za-z0-9]", "-", root)
                for root in roots
            )
            if not matches:
                continue
            for path in memory.glob("*.md"):
                if path.stat().st_size <= 2 * 1024 * 1024:
                    self.note(
                        connection,
                        path,
                        "claude-memory",
                        profile.get("knowledge_scope", "active"),
                    )
                else:
                    self.gap("oversized-memory-note")

    def run(self, names=None, notes=True):
        for name in names or self.registry["projects"]:
            for source in (
                [self.codex, self.claude, self.claude_notes]
                if notes
                else [self.codex, self.claude]
            ):
                try:
                    self.state.set(
                        "sync-progress",
                        {
                            "project": name,
                            "source": source.__name__,
                            "phase": "running",
                            "events_changed": self.report["events_changed"],
                        },
                    )
                    source(name)
                except Exception as exc:
                    # Keep independent sources running; never log payloads.
                    self.report["errors"].append(
                        {
                            "project": name,
                            "source": source.__name__,
                            "type": type(exc).__name__,
                            "details": exc.data if isinstance(exc, LinkError) else {},
                        }
                    )
        self.state.set("sync-progress", dict(self.report, phase="complete"))
        return self.report
