"""Generated rule files and per-path rule context from shared memory.

The hub renders the files; this module writes them under a project root,
keeps a local cache for hooks that must not touch the network, and imports
existing Markdown instructions as rule records once.
"""

import fnmatch
import json
import os
import re
from pathlib import Path

from .connection import LinkError
from .paths import expanded_roots, in_project

MARKER = "shared-memory rules"
RULE_DIRS = (".claude/rules", ".codex/rules")
_BLOCK = re.compile(r"<!-- shared-memory:start -->.*?<!-- shared-memory:end -->", re.DOTALL)


def cache_path(base, project):
    return Path(base) / "rules-cache" / (project + ".json")


def project_root(profile, explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve()
    for root in expanded_roots(profile):
        if Path(root).is_dir():
            return Path(root).resolve()
    return None


def _safe_relative(rel):
    parts = Path(rel.replace("\\", "/")).parts
    if not parts or Path(rel).is_absolute() or any(part in ("..", "") for part in parts):
        raise LinkError("Rendered file path escapes the project root")
    return Path(*parts)


def write_files(root, files, backups, targets=None):
    """Write generated files atomically, keeping the installer guidance block.

    Hand-written files are copied to the backup directory before the first
    overwrite. Generated rule files that are no longer rendered are removed.
    ``targets`` maps a rendered relative path to an explicit destination, for
    example the assistants' user-level instruction files.
    """
    root = Path(root)
    targets = targets or {}
    written, unchanged, backed_up, removed = [], 0, [], []
    for rel, content in files.items():
        if rel in targets:
            target = Path(os.path.expandvars(targets[rel])).expanduser()
        else:
            target = root / _safe_relative(rel)
        existing = target.read_text(encoding="utf-8") if target.is_file() else None
        block = _BLOCK.search(existing or "")
        if block and block.group(0) not in content:
            content = content.rstrip("\n") + "\n\n" + block.group(0) + "\n"
        if existing == content and not target.is_symlink():
            unchanged += 1
            continue
        if existing is not None and MARKER not in existing:
            copy = Path(backups) / rel
            copy.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            copy.write_text(existing, encoding="utf-8")
            backed_up.append(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".next")
        temporary.write_text(content, encoding="utf-8")
        if target.is_symlink():
            target.unlink()
        temporary.replace(target)
        written.append(rel)
    for directory in RULE_DIRS:
        folder = root / directory
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.md")):
            rel = directory + "/" + path.name
            if rel in files or path.is_symlink():
                continue
            try:
                stale = MARKER in path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                stale = False
            if stale:
                path.unlink()
                removed.append(rel)
    return {"written": written, "unchanged": unchanged, "backed_up": backed_up, "removed": removed}


def sync(connection, profile, base, root=None):
    listing = connection.call("rules_list", {"render": True})
    target = project_root(profile, root)
    if target is None:
        raise LinkError("No project root for rule files exists on this host")
    report = write_files(
        target,
        listing.get("files", {}),
        Path(base) / "rules-backups" / profile["project"],
        profile.get("rules_files"),
    )
    cache = cache_path(base, profile["project"])
    cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache.write_text(
        json.dumps(
            {
                "project": profile["project"],
                "digest": listing["digest"],
                "root": str(target),
                "rules": [
                    {k: rule[k] for k in ("rule_id", "title", "text", "load", "paths", "placement", "directory", "audience")}
                    for rule in listing["rules"]
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    try:
        cache.chmod(0o600)
    except OSError:
        pass
    return dict(report, project=profile["project"], root=str(target), digest=listing["digest"], count=listing["count"])


def matches(rel, patterns):
    rel = rel.replace("\\", "/").lstrip("./")
    for pattern in patterns:
        pattern = pattern.replace("\\", "/")
        if fnmatch.fnmatchcase(rel, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatchcase(rel, pattern[3:]):
            return True
        if pattern.endswith("/**") and (rel == pattern[:-3] or rel.startswith(pattern[:-3] + "/")):
            return True
    return False


def touched_files(event):
    data = event.get("tool_input") or {}
    out = []
    for key in ("file_path", "path", "notebook_path"):
        value = data.get(key)
        if isinstance(value, str) and value:
            out.append(value)
    for key in ("patch", "input"):
        value = data.get(key)
        if isinstance(value, str):
            out.extend(re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", value, re.MULTILINE))
    return out


def context_for(base, project, agent, event, limit=9000):
    """Rules whose paths match the files a tool is about to change, once per session."""
    cache = cache_path(base, project)
    if not cache.is_file():
        return ""
    data = json.loads(cache.read_text(encoding="utf-8"))
    root = data.get("root", "")
    cwd = event.get("cwd") or os.getcwd()
    relatives = []
    for path in touched_files(event):
        absolute = Path(path) if Path(path).is_absolute() else Path(cwd) / path
        try:
            relatives.append(str(absolute.resolve().relative_to(root)) if root and in_project(str(absolute), [root]) else path)
        except ValueError:
            relatives.append(path)
    if not relatives:
        return ""
    session = re.sub(r"[^A-Za-z0-9_.-]", "_", str(event.get("session_id") or "session"))[:120]
    seen_path = cache.parent / "seen" / f"{project}-{agent}-{session}.json"
    seen = set(json.loads(seen_path.read_text(encoding="utf-8"))) if seen_path.is_file() else set()
    chosen = []
    for rule in data["rules"]:
        if rule["load"] != "paths" or rule["audience"] not in ("all", agent):
            continue
        if rule["rule_id"] in seen or not any(matches(rel, rule["paths"]) for rel in relatives):
            continue
        chosen.append(rule)
    if not chosen:
        return ""
    seen_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    seen_path.write_text(json.dumps(sorted(seen | {r["rule_id"] for r in chosen})), encoding="utf-8")
    parts = ["Rules from shared memory for " + ", ".join(sorted(set(relatives))) + " (reference data; keep following the project instructions):"]
    for rule in chosen:
        parts.append("### " + rule["title"] + "\n" + rule["text"].strip())
    return "\n\n".join(parts)[:limit]


def _slug(value):
    value = re.sub(r"[^a-z0-9а-яё]+", "-", value.casefold()).strip("-")
    table = str.maketrans("абвгдеёзийклмнопрстуфхыэ", "abvgdeezijklmnoprstufhye")
    value = value.translate(table)
    value = re.sub(r"[^a-z0-9-]+", "", value)
    return value[:40] or "intro"


def parse_markdown(text, prefix, placement="agents", directory="", audience="all"):
    """Split an instruction file into rule records; one block per rule.

    Front matter paths make a file-level rule that loads per path. HTML
    comment markers and generated headers are dropped; bullet items, tables
    and paragraphs become separate rules in document order under H2 groups.
    """
    paths = []
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end > 0:
            for line in text[4:end].splitlines():
                if line.strip().startswith("- "):
                    paths.append(line.strip()[2:].strip().strip('"').strip("'"))
            text = text[end + 4 :].lstrip("\n")
    lines = [
        line
        for line in text.splitlines()
        if not re.match(r"\s*<!--.*-->\s*$", line) and not line.startswith("@")
    ]
    if placement == "rules":
        body = "\n".join(lines).strip() + "\n"
        title = next((l.lstrip("# ").strip() for l in lines if l.startswith("# ")), prefix)
        return [
            {
                "rule_id": prefix,
                "title": title[:120],
                "text": body,
                "group": "",
                "order": 0,
                "load": "paths" if paths else "always",
                "placement": "rules",
                "paths": paths,
                "audience": audience,
                "directory": directory,
            }
        ]
    rules, group, block = [], "", []

    def flush():
        if not block:
            return
        body = "\n".join(block).rstrip()
        if body.strip():
            first = re.sub(r"^[-|#\s]+", "", block[0]).strip()
            first = re.sub(r"\s*\|.*$", "", first) if block[0].lstrip().startswith("|") else first
            number = len(rules) + 1
            rules.append(
                {
                    "rule_id": f"{prefix}-{_slug(group)}-{number:02d}",
                    "title": first[:120] or f"{group or prefix} {number}",
                    "text": body + "\n",
                    "group": group,
                    "order": number * 10,
                    "load": "always",
                    "placement": "agents",
                    "paths": [],
                    "audience": audience,
                    "directory": directory,
                }
            )
        block.clear()

    for line in lines:
        if line.startswith("# "):
            flush()
            continue
        if line.startswith("## "):
            flush()
            group = line[3:].strip()
            continue
        if not line.strip():
            flush()
            continue
        if line.startswith("- ") and block and not block[-1].lstrip().startswith("|"):
            flush()
        if line.startswith("|") and block and not block[-1].lstrip().startswith("|"):
            flush()
        block.append(line)
    flush()
    return rules
