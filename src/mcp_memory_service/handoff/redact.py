"""Minimize sensitive data before persistence, not only when displaying it."""

import ipaddress
import re
import json
import os
from pathlib import Path

_KEYS = r"password|passwd|pwd|secret|token|api[_-]?key|authorization|cookie|private[_-]?key|client[_-]?secret"


def text(value: str) -> str:
    value = re.sub(
        r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----.*?-----END (?:[A-Z ]+)?PRIVATE KEY-----",
        "[private key removed]",
        value,
        flags=re.S,
    )
    value = re.sub(
        r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [redacted]", value
    )
    value = re.sub(
        r"(?i)([\"']?(?:"
        + _KEYS
        + r")[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1[redacted]",
        value,
    )
    value = re.sub(
        r"(?i)((?:--?|/)(?:password|passwd|token|secret|api-key)\s+)(?:\"[^\"]*\"|'[^']*'|[^\s;]+)",
        r"\1[redacted]",
        value,
    )
    value = re.sub(
        r"(?i)((?:AWS_SECRET_ACCESS_KEY|[A-Z_]+_(?:TOKEN|PASSWORD|SECRET|API_KEY))\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}]+)",
        r"\1[redacted]",
        value,
    )
    value = re.sub(
        r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{15,}|github_pat_[A-Za-z0-9_]{15,}|glpat-[A-Za-z0-9_-]{12,})\b",
        "[redacted]",
        value,
    )
    value = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[redacted]@", value)
    value = re.sub(r"(?i)([?&](?:" + _KEYS + r")=)[^&\s]+", r"\1[redacted]", value)

    def ip(match):
        try:
            return (
                "[private address]"
                if ipaddress.ip_address(match[0]).is_private
                else match[0]
            )
        except ValueError:
            return match[0]

    value = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", ip, value)
    value = re.sub(
        r"(?i)\b(?:[a-z0-9_-]+\.)+(?:local|internal|corp|lan)\b",
        "[internal host]",
        value,
    )
    return value.replace("\x00", "")


def clean(value):
    if isinstance(value, str):
        return text(value)
    if isinstance(value, list):
        return [clean(x) for x in value]
    if isinstance(value, dict):
        return {
            k: "[redacted]" if re.fullmatch(_KEYS, k, re.I) else clean(v)
            for k, v in value.items()
        }
    return value


def project_clean(value, project):
    """Apply private administrator-owned patterns, never patterns from MCP input."""
    filename = os.environ.get("MCP_HANDOFF_TOKENS_FILE")
    if not filename:
        return value
    config = json.loads(Path(filename).read_text())["projects"].get(project, {})
    patterns = [re.compile(p) for p in config.get("redact_patterns", [])]

    def visit(item):
        if isinstance(item, str):
            for pattern in patterns:
                item = pattern.sub("[internal reference]", item)
            return item
        if isinstance(item, list):
            return [visit(x) for x in item]
        if isinstance(item, dict):
            return {k: visit(v) for k, v in item.items()}
        return item

    return visit(value)
