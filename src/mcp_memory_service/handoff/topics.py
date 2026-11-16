"""Generic section defaults with optional administrator-owned private definitions."""

import json
import os
import re
from functools import lru_cache
from pathlib import Path

TOPICS = {
    "web": ("Web", r"https?://|\bweb\b|\btls\b|browser"),
    "database": ("Database", r"\bsql\b|sqlite|database"),
    "operations": ("Operations", r"task-runner|\bdeploy(?:ment)?\b|automation"),
    "code": ("Code", r"\bpython(?:3)?\b|source code|unit test"),
    "memory": ("Agent memory", r"\bmcp\b|memory-service|shared memory"),
}


@lru_cache(maxsize=4)
def _load_policy(filename, modified_ns):
    value = json.loads(Path(filename).read_text(encoding="utf-8"))
    if value.get("schema") != 1 or not isinstance(value.get("sections"), dict):
        raise ValueError("Unsupported private section policy")
    sections = {}
    for ident, definition in value["sections"].items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9+_.-]{0,79}", ident):
            raise ValueError("Invalid section identifier in private policy")
        title, pattern = definition["name"], definition["pattern"]
        if (
            not isinstance(title, str)
            or len(title) > 160
            or not isinstance(pattern, str)
            or len(pattern) > 4096
        ):
            raise ValueError("Invalid private section definition")
        re.compile(pattern, re.IGNORECASE)
        sections[ident] = (title, pattern)
    aliases = value.get("aliases", {})
    if not isinstance(aliases, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or v not in sections
        for k, v in aliases.items()
    ):
        raise ValueError("Invalid private section aliases")
    return sections, {k.casefold(): v for k, v in aliases.items()}


def definitions():
    filename = os.environ.get("MCP_HANDOFF_SECTIONS_FILE")
    if not filename:
        return TOPICS, {}
    path = Path(filename)
    return _load_policy(str(path.resolve()), path.stat().st_mtime_ns)


def normalize(value):
    value = str(value).strip().casefold()
    return definitions()[1].get(value, value)


def classify(value, explicit=()):
    sections, _ = definitions()
    explicit = {normalize(x) for x in explicit}
    inferred = {
        name
        for name, (_, pattern) in sections.items()
        if re.search(pattern, value, re.IGNORECASE)
    }
    return sorted(explicit | inferred)


def label(value):
    return definitions()[0].get(value, (value, ""))[0]
