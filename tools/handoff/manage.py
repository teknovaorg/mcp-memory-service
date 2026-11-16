#!/usr/bin/env python3
"""Local hub administration. Private connection files are never release assets."""

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import uuid
import re


def provision(args):
    settings = Path(args.settings)
    inventory = json.loads(Path(args.inventory).read_text())
    credentials = settings / "connections.json"
    existing = (
        json.loads(credentials.read_text())
        if credentials.is_file()
        else {"schema": 1, "device_id": str(uuid.uuid4()), "projects": {}}
    )
    existing["url"] = args.url
    for name, profile in inventory["projects"].items():
        if not name or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for c in name
        ):
            raise RuntimeError("Invalid project identifier")
        old = existing["projects"].get(name, {})
        existing["projects"][name] = dict(
            profile, token=old.get("token") or secrets.token_urlsafe(40)
        )
    credentials.write_text(json.dumps(existing, indent=2) + "\n")
    credentials.chmod(0o600)
    projects = settings / "projects.json"
    temporary = projects.with_suffix(".next.json")
    temporary.write_text(
        json.dumps(
            {
                "projects": {
                    name: {
                        "token_sha256": hashlib.sha256(
                            value["token"].encode()
                        ).hexdigest(),
                        "redact_patterns": value.get("redact_patterns", []),
                    }
                    for name, value in existing["projects"].items()
                }
            }
        )
    )
    temporary.chmod(0o640)
    subprocess.run(["chown", "root:shared-memory", str(temporary)], check=True)
    temporary.replace(projects)
    print(
        json.dumps(
            {
                "provisioned": len(existing["projects"]),
                "private_registry": str(credentials),
            }
        )
    )


def verify_backup(args):
    original = Path(args.database)
    import tempfile

    with tempfile.TemporaryDirectory(prefix="memory-restore-check-") as directory:
        copy = Path(directory) / "restored.sqlite3"
        with (
            sqlite3.connect(original.as_uri() + "?mode=ro", uri=True) as source,
            sqlite3.connect(copy) as dest,
        ):
            source.backup(dest)
            integrity = dest.execute("PRAGMA integrity_check").fetchone()[0]
            tables = [
                x[0]
                for x in dest.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'hf_%' AND name NOT LIKE '%fts%'"
                )
            ]
            counts = {
                table: dest.execute('SELECT COUNT(*) FROM "' + table + '"').fetchone()[
                    0
                ]
                for table in tables
            }
        if integrity != "ok":
            raise RuntimeError("Backup integrity check failed")
        print(
            json.dumps(
                {"restored_copy_integrity": integrity, "structured_tables": counts}
            )
        )


def scrub_journal(args):
    """Migrate journal text after tightening private hostname redaction policy."""
    policies = json.loads(Path(args.policy).read_text())["projects"]
    patterns = {
        project: [re.compile(p) for p in cfg.get("redact_patterns", [])]
        for project, cfg in policies.items()
    }
    path = Path(args.database).resolve()
    with sqlite3.connect(path) as db:
        for project, rules in patterns.items():
            for row in db.execute(
                "SELECT statement FROM hf_knowledge WHERE project=?", (project,)
            ):
                if any(rule.search(row[0]) for rule in rules):
                    raise RuntimeError(
                        "Knowledge policy migration requires re-indexing; journal was not changed"
                    )
        backup = path.with_name(path.name + ".before-journal-policy")
        if backup.exists():
            raise RuntimeError(
                "Policy backup exists; inspect the previous migration first"
            )
        with sqlite3.connect(backup) as copy:
            db.backup(copy)
        backup.chmod(0o600)
        db.execute("PRAGMA secure_delete=ON")
        changed = 0
        for project, rules in patterns.items():
            for table, fields, clause in [
                ("hf_events", ["summary", "command", "excerpt", "files_json"], ""),
                ("hf_entries", ["title", "body"], " AND entity_type='hf_events'"),
            ]:
                for row in db.execute(
                    "SELECT rowid,"
                    + ",".join(fields)
                    + " FROM "
                    + table
                    + " WHERE project=?"
                    + clause,
                    (project,),
                ).fetchall():
                    values = []
                    for value in row[1:]:
                        for rule in rules:
                            value = rule.sub("[internal reference]", value)
                        values.append(value)
                    if list(row[1:]) != values:
                        db.execute(
                            "UPDATE "
                            + table
                            + " SET "
                            + ",".join(field + "=?" for field in fields)
                            + " WHERE rowid=?",
                            values + [row[0]],
                        )
                        changed += 1
        db.execute("INSERT INTO hf_entries_fts(hf_entries_fts) VALUES('rebuild')")
        db.commit()
        db.execute("VACUUM")
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    print(
        json.dumps(
            {
                "journal_policy_migrated": True,
                "changed_rows": changed,
                "backup_preserved": True,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("provision")
    p.add_argument("--inventory", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--settings", default="/etc/shared-memory")
    p = commands.add_parser("verify-backup")
    p.add_argument("database")
    p = commands.add_parser("scrub-journal")
    p.add_argument("database")
    p.add_argument("--policy", required=True)
    args = parser.parse_args()
    {
        "provision": provision,
        "verify-backup": verify_backup,
        "scrub-journal": scrub_journal,
    }[args.command](args)


if __name__ == "__main__":
    main()
