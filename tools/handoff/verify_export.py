#!/usr/bin/env python3
"""Compare acknowledged local public-event IDs with a read-only hub inventory."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys


def audit(args):
    registry_path = Path(args.registry).resolve()
    registry = json.loads(registry_path.read_text())
    artifact = json.loads((registry_path.parent / "installed.json").read_text())[
        "artifact"
    ]
    sys.path.insert(0, artifact)
    from shared_memory.paths import locations, owner
    from shared_memory.sources import (
        sqlite_threads,
        sqlite_items,
        jsonl_rows,
        codex_event,
        claude_events,
        user_text,
        instruction_envelope,
    )
    from shared_memory.sync import ignored_codex_imports

    native = locations(registry)
    with sqlite3.connect(registry_path.parent / "client-state.sqlite") as db:
        state = {
            row[0]: json.loads(row[1])
            for row in db.execute("SELECT source,value FROM sources")
        }
    expected = {}
    missing_ack = 0
    ignored = ignored_codex_imports(native["codex"])
    for thread in sqlite_threads(native["codex"]):
        if thread["id"] in ignored:
            continue
        project = owner(thread["cwd"], registry["projects"])
        if not project:
            continue
        session = f"codex:{registry['device_id']}:{thread['id']}"
        ids = set()
        expected[(project, session)] = ids
        paginated = thread.get("history_mode") == "paginated"
        key = f"codex-{'sqlite' if paginated else 'jsonl'}:{project}:{thread['id']}"
        position = state.get(key)
        if not position:
            missing_ack += 1
            continue
        if paginated:
            ordinal = 0
            ident = ""
            upper = (position["ordinal"], position["item_id"])
            while True:
                rows = sqlite_items(native["codex"], thread["id"], ordinal, 512, ident)
                if not rows:
                    break
                stop = False
                for row in rows:
                    point = (row["updated_at_ordinal"], row["item_id"])
                    if point > upper:
                        stop = True
                        break
                    event = codex_event(json.loads(row["bounded_json"]))
                    if event:
                        ids.add(event["event_id"])
                    ordinal, ident = point
                if stop:
                    break
        else:
            offset = 0
            while offset < position["offset"]:
                rows = list(jsonl_rows(thread["rollout_path"], offset, max_records=500))
                if not rows:
                    break
                for row, end in rows:
                    if end > position["offset"]:
                        break
                    payload = row.get("payload") or {}
                    if (
                        row.get("type") == "response_item"
                        and payload.get("type") == "message"
                        and payload.get("role") in ("user", "assistant")
                    ):
                        text = user_text(payload.get("content", []))
                        if (
                            text
                            and not instruction_envelope(text)
                            and payload.get("channel") != "analysis"
                        ):
                            ids.add(f"offset:{end}")
                    elif row.get("type") == "compacted":
                        ids.add(f"offset:{end}")
                offset = rows[-1][1]
    for path in (native["claude"] / "projects").rglob("*.jsonl"):
        cwd = next(
            (r["cwd"] for r, end in jsonl_rows(path, max_records=200) if r.get("cwd")),
            None,
        )
        project = owner(cwd, registry["projects"]) if cwd else None
        if not project:
            continue
        key = (
            "claude-jsonl:"
            + project
            + ":"
            + hashlib.sha256(str(path).encode()).hexdigest()
        )
        position = state.get(key)
        session = f"claude:{registry['device_id']}:{path.stem}"[:160]
        ids = set()
        expected[(project, session)] = ids
        if not position:
            missing_ack += 1
            continue
        offset = 0
        calls = {}
        while offset < position["offset"]:
            rows = list(jsonl_rows(path, offset, max_records=500))
            if not rows:
                break
            for row, end in rows:
                if end > position["offset"]:
                    break
                ids.update(event["event_id"] for event in claude_events(row, calls))
                if len(calls) > 200:
                    calls = dict(list(calls.items())[-200:])
            offset = rows[-1][1]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.@-]*", args.ssh_host):
        raise ValueError("Invalid SSH alias")
    script = """import sqlite3,json
with sqlite3.connect('file:/var/lib/shared-memory/memory.sqlite3?mode=ro',uri=True) as db:
 print(json.dumps(db.execute('SELECT project,session_id,event_id FROM hf_events').fetchall()))
"""
    actual = {}
    for project, session, ident in json.loads(
        subprocess.check_output(
            ["ssh", args.ssh_host, "python3 -c " + shlex.quote(script)], text=True
        )
    ):
        actual.setdefault((project, session), set()).add(ident)
    missing = sum(len(ids - actual.get(key, set())) for key, ids in expected.items())
    extra = sum(len(actual.get(key, set()) - ids) for key, ids in expected.items())
    return {
        "sources_checked": len(expected),
        "expected_acknowledged_unique_events": sum(map(len, expected.values())),
        "missing_events": missing,
        "sources_without_acknowledgement": missing_ack,
        "additional_live_events_after_snapshot": extra,
        "passed": missing == 0 and missing_ack == 0,
        "private_reasoning_included": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    result = audit(args)
    if args.output:
        path = Path(args.output)
        path.write_text(json.dumps(result, indent=2) + "\n")
        path.chmod(0o600)
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)
