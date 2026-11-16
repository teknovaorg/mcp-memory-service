"""Native locations and stable project identity across Windows, macOS and Linux."""

import ntpath
import os
from pathlib import Path
import posixpath
import re
import sys


def locations(profile=None, env=None, home=None, platform=None):
    profile, env = profile or {}, os.environ if env is None else env
    home, platform = (
        Path.home() if home is None else Path(home),
        platform or sys.platform,
    )
    if platform == "win32":
        base = (
            Path(env.get("LOCALAPPDATA", str(home / "AppData/Local"))) / "SharedMemory"
        )
    elif platform == "darwin":
        base = home / "Library/Application Support/SharedMemory"
    else:
        base = Path(env.get("XDG_CONFIG_HOME", str(home / ".config"))) / "shared-memory"
    result = {
        "base": base,
        "codex": Path(
            profile.get("codex_home") or env.get("CODEX_HOME") or home / ".codex"
        ).expanduser(),
        "claude": Path(
            profile.get("claude_home")
            or env.get("CLAUDE_CONFIG_DIR")
            or home / ".claude"
        ).expanduser(),
    }
    result["claude_config"] = (
        home / ".claude.json"
        if result["claude"] == home / ".claude"
        else result["claude"] / ".claude.json"
    )
    return result


def normalized(value):
    value = str(value)
    windows = bool(re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"))
    if windows:
        return ntpath.normpath(value).replace("\\", "/").rstrip("/").casefold()
    return posixpath.normpath(value.replace("\\", "/")).rstrip("/") or "/"


def in_project(cwd, roots):
    candidate = normalized(cwd)
    return any(
        candidate == normalized(root)
        or candidate.startswith(normalized(root).rstrip("/") + "/")
        for root in roots
    )


def expanded_roots(profile):
    return [
        str(Path(os.path.expandvars(root)).expanduser())
        for root in profile.get("roots", [])
    ]


def owner(cwd, projects):
    matches = [
        (len(normalized(root)), name)
        for name, value in projects.items()
        for root in expanded_roots(value)
        if in_project(cwd, [root])
    ]
    return max(matches)[1] if matches else None
