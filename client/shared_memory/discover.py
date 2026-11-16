"""Read only source metadata; produce an inventory for explicit provisioning."""

import json
import re

from .paths import locations
from .sources import jsonl_rows, sqlite_threads


def discover(config=None):
    native = locations(config)
    roots = {x["cwd"] for x in sqlite_threads(native["codex"]) if x.get("cwd")}
    claude_config = native["claude_config"]
    if claude_config.is_file():
        roots.update(json.loads(claude_config.read_text()).get("projects", {}))
    for path in (native["claude"] / "projects").rglob("*.jsonl"):
        for row, _ in jsonl_rows(path, max_records=200):
            if row.get("cwd"):
                roots.add(row["cwd"])
                break
    inventory = {}
    for root in sorted(roots):
        path = root.replace("\\", "/").rstrip("/")
        parts = path.split("/")
        if "Developer" in parts:
            pos = parts.index("Developer")
            tail = parts[pos + 1 :]
            if not tail:
                name = "workspace-general"
            elif tail[0].upper() == "PERSONAL" and len(tail) > 1:
                name, path = "personal-" + tail[1], "/".join(parts[: pos + 3])
            else:
                name, path = tail[0], "/".join(parts[: pos + 2])
        else:
            name = "workspace-" + (parts[-1] or "root")
        name = (
            re.sub(r"[^a-z0-9_.-]", "-", name.lower()).strip("-.")[:80] or "workspace"
        )
        entry = inventory.setdefault(
            name,
            {
                "roots": [],
                "knowledge_scope": "active"
                if name.startswith("personal-")
                else "archive",
                "default_sections": [],
            },
        )
        for alias in (path, root):
            if alias not in entry["roots"]:
                entry["roots"].append(alias)
    # A memory-only project may no longer have a checkout or transcript. Keep
    # its encoded directory as an explicit archive source, without guessing a cwd.
    for memory in (native["claude"] / "projects").glob("*/memory"):
        encoded = memory.parent.name
        matched = [
            name
            for name, profile in inventory.items()
            if any(
                encoded == re.sub(r"[^A-Za-z0-9]", "-", root)
                for root in profile["roots"]
            )
        ]
        if not matched:
            import hashlib

            name = "archive-claude-" + hashlib.sha256(encoded.encode()).hexdigest()[:12]
            inventory[name] = {
                "roots": [],
                "knowledge_scope": "archive",
                "default_sections": [],
                "memory_directories": [encoded],
            }
    return {"projects": inventory}
