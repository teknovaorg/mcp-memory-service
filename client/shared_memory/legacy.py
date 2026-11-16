"""Explicit one-time historical import; never treats a legacy row as current truth."""

import hashlib
from pathlib import Path
import sqlite3

from .connection import LinkError
from .sources import compact


def import_legacy(client, database):
    database = Path(database).resolve()
    rows = changed = fragments = repaired = 0

    def decode(raw):
        nonlocal repaired
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            repaired += 1
            return raw.decode("cp1251", errors="replace")

    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.text_factory = decode
        db.row_factory = sqlite3.Row
        columns = {r[1] for r in db.execute("PRAGMA table_info(memories)")}
        if not {"id", "content", "title", "updated_at"} <= columns:
            raise LinkError("Unsupported legacy schema")
        for row in db.execute(
            "SELECT id,title,content,type,tags,context,created_at,updated_at FROM memories ORDER BY id"
        ):
            rows += 1
            text = (
                "Historical source. Verify against current configuration.\n"
                + "\n".join(
                    f"{key}: {row[key]}"
                    for key in ("type", "tags", "context", "created_at", "updated_at")
                    if row[key]
                )
                + "\n"
                + (row["content"] or "")
            )
            for index, offset in enumerate(range(0, max(1, len(text)), 6000)):
                source = f"legacy-db:{database.name}:{row['id']}:{index}"
                record_id = "legacy:" + hashlib.sha256(source.encode()).hexdigest()[:40]
                previous = client.call("knowledge_get", {"record_id": record_id})[
                    "record"
                ]
                if previous and previous["status"] != "imported":
                    continue
                result = client.call(
                    "knowledge_record",
                    {
                        "record_id": record_id,
                        "title": compact(row["title"] or "Historical note", 512)[0],
                        "statement": compact(text[offset : offset + 6000], 8192)[0],
                        "scope": "archive",
                        "status": "imported",
                        "source": source,
                        "source_revision": hashlib.sha256(text.encode()).hexdigest(),
                        "observed_at": str(row["updated_at"]),
                        "agent": "legacy-importer",
                        "base_revision": previous["revision"] if previous else 0,
                    },
                )
                fragments += 1
                changed += int(result["changed"])
                if result.get("indexed") is False:
                    raise LinkError(
                        "Historical record saved but semantic index needs retry"
                    )
    return {
        "legacy_rows": rows,
        "fragments": fragments,
        "changed": changed,
        "scope": "archive",
        "non_utf8_fields_decoded_as_cp1251": repaired,
    }
