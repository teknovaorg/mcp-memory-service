"""Render standing rules into the files coding assistants load natively.

Memory is the source of truth; generated files are a cache carrying a digest.
Layout relative to a project root:

- AGENTS.md: rules for every assistant plus Codex-only rules, root directory.
- CLAUDE.md: imports AGENTS.md and adds Claude-only rules.
- <directory>/AGENTS.md and <directory>/CLAUDE.md for rules bound to a directory.
- .claude/rules/<rule_id>.md and .codex/rules/<rule_id>.md for rules placed as
  separate files, with a paths front matter when they load per path.
- .codex/rules/index.md listing the rule files for an assistant without
  automatic path matching.
"""

import hashlib
import json
import posixpath

MARKER = "shared-memory rules"
CLAUDE_RULES = ".claude/rules"
CODEX_RULES = ".codex/rules"


def digest(rules):
    identity = [(r["rule_id"], r["revision"], r["status"]) for r in rules]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]


def header(project, fingerprint, count):
    # No timestamp: identical rule sets must render byte-identical files.
    return (
        f"<!-- {MARKER}: project {project}, {count} rules, digest {fingerprint}. "
        "Generated from shared memory: change rules with rule_record, then run "
        "rules sync. Manual edits are overwritten. -->\n"
    )


def _audience(rule, agent):
    return rule["audience"] in ("all", agent)


def _agents_body(rules):
    # Groups keep the position of their first rule; rules stay in order inside a group.
    groups = {}
    for rule in sorted(rules, key=lambda r: (r["order"], r["rule_id"])):
        groups.setdefault(rule["group"], []).append(rule)
    lines = []
    for group, members in groups.items():
        if group:
            lines.append("")
            lines.append("## " + group)
        for rule in members:
            lines.append("")
            lines.append(rule["text"].rstrip())
    return "\n".join(lines).strip("\n") + "\n"


def _rule_file(rule):
    front = ""
    if rule["load"] == "paths" and rule["paths"]:
        front = "---\npaths:\n" + "".join(
            "  - " + json.dumps(path, ensure_ascii=False) + "\n" for path in rule["paths"]
        ) + "---\n\n"
    body = rule["text"].rstrip() + "\n"
    if not body.lstrip().startswith("#"):
        body = "# " + rule["title"].strip() + "\n\n" + body
    return front, body


def _index(rules):
    rows = ["| Rule file | Loads | Paths |", "|---|---|---|"]
    for rule in rules:
        paths = ", ".join("`" + p + "`" for p in rule["paths"])
        if not paths:
            paths = "every task" if rule["load"] == "always" else "on request"
        rows.append(f"| `{rule['rule_id']}.md` | {rule['load']} | {paths} |")
    return (
        "# Rule files\n\nThese files are generated from shared memory. "
        "Read the files matching the task before changing sources; the `paths` front matter "
        "is applied automatically by Claude Code and listed here for Codex.\n\n"
        + "\n".join(rows)
        + "\n"
    )


def render(rules, project):
    """Return {relative path: content} for every file the rule set implies."""
    active = [r for r in rules if r["status"] == "active"]
    fingerprint = digest(active)
    files = {}
    if not active:
        # A project without rules keeps its hand-written files untouched.
        return {"digest": fingerprint, "files": files}
    agents = [r for r in active if r["placement"] == "agents"]
    directories = sorted({r["directory"] for r in agents} | {""})
    for directory in directories:
        scoped = [r for r in agents if r["directory"] == directory]
        shared = [r for r in scoped if _audience(r, "codex")]
        claude_only = [r for r in scoped if r["audience"] == "claude"]
        prefix = posixpath.join(directory, "") if directory else ""
        if shared or not directory:
            files[prefix + "AGENTS.md"] = header(project, fingerprint, len(shared)) + "\n" + (
                _agents_body(shared) if shared else "No shared rules for this directory.\n"
            )
        claude = header(project, fingerprint, len(claude_only)) + "\n@AGENTS.md\n"
        if claude_only:
            claude += "\n" + _agents_body(claude_only)
        if shared or claude_only or not directory:
            files[prefix + "CLAUDE.md"] = claude
    separate = [r for r in active if r["placement"] == "rules"]
    for rule in separate:
        front, body = _rule_file(rule)
        content = front + header(project, fingerprint, 1) + "\n" + body
        name = rule["rule_id"] + ".md"
        if _audience(rule, "claude"):
            files[CLAUDE_RULES + "/" + name] = content
        if _audience(rule, "codex"):
            files[CODEX_RULES + "/" + name] = content
    codex_rules = [r for r in separate if _audience(r, "codex")]
    if codex_rules:
        files[CODEX_RULES + "/index.md"] = header(project, fingerprint, len(codex_rules)) + "\n" + _index(codex_rules)
    return {"digest": fingerprint, "files": files}
