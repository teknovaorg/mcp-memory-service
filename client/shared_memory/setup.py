"""Idempotent native client configuration; never writes hook trust decisions."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import subprocess
import sys

from .paths import locations


GUIDANCE = """Use the shared-memory MCP for cross-session state. Select the current project; do not mix employers or projects. Start continuing work with session_context. For technical knowledge, select sections (web, database, operations, etc.); knowledge_search searches the section first, then keywords, then task context. Imported/historical records are references, not verified current settings or authorization. Record explicit task_checkpoint, decision_record, incident_record and verification_record as meaningful work progresses. Preserve stated rationale and evidence, not private reasoning. Native history files are sources; do not overwrite them or create duplicate MEMORY.md logs. Check the actual working tree before continuing changes from another host. Standing rules live in memory as rule_record and render into AGENTS.md, CLAUDE.md and rule files that carry a digest; change a rule through rule_record (read rules_list first), never by editing generated files."""


def backup(path, directory):
    if path.is_file():
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = directory / (
            path.name + "." + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        )
        shutil.copy2(path, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".next")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        temporary.chmod(0o600)
    except OSError:
        pass
    temporary.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def toml_server(original, python, arguments, replace_legacy=True):
    lines = original.splitlines()
    output = []
    skip = False
    in_old = False
    for line in lines:
        if line.lstrip().startswith("["):
            header = line.strip()
            skip = header == "[mcp_servers.shared-memory]" or header.startswith(
                "[mcp_servers.shared-memory."
            )
            in_old = header == "[mcp_servers.agent-memory]"
        if skip:
            continue
        if in_old and replace_legacy and line.strip().startswith("enabled"):
            continue
        output.append(line)
        if in_old and replace_legacy and line.strip() == "[mcp_servers.agent-memory]":
            output.append("enabled = false")
    while output and not output[-1].strip():
        output.pop()
    output += [
        "",
        "[mcp_servers.shared-memory]",
        "command = " + json.dumps(python),
        "args = " + json.dumps(arguments),
        "enabled = true",
        "",
    ]
    result = "\n".join(output)
    import tomllib

    tomllib.loads(result)
    return result


def command(arguments):
    if os.name == "nt":
        # An executable command works from both cmd and PowerShell hook runners.
        script = "& " + " ".join(
            "'" + arg.replace("'", "''") + "'" for arg in arguments
        )
        return subprocess.list2cmdline(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]
        )
    return shlex.join(arguments)


def install(config, registry_path, daemon=True, replace_legacy=True):
    registry_path = Path(registry_path).resolve()
    base = registry_path.parent
    launcher = base / "shared-memory.py"
    if not launcher.is_file():
        raise RuntimeError(
            "Install the verified client release with bootstrap.py first"
        )
    native = locations(config)
    backups = base / "config-backups"
    python = sys.executable
    common = [str(launcher), "--registry", str(registry_path)]
    codex = native["codex"] / "config.toml"
    codex.parent.mkdir(parents=True, exist_ok=True)
    backup(codex, backups)
    codex.write_text(
        toml_server(
            codex.read_text(encoding="utf-8") if codex.is_file() else "",
            python,
            common + ["mcp", "--agent", "codex"],
            replace_legacy,
        ),
        encoding="utf-8",
    )
    try:
        codex.chmod(0o600)
    except OSError:
        pass
    # The CLI's home file contains the user-scoped MCP map; retain every other key.
    claude_file = native["claude_config"]
    backup(claude_file, backups)
    value = read_json(claude_file)
    servers = value.setdefault("mcpServers", {})
    if replace_legacy:
        servers.pop("agent-memory", None)
    servers["shared-memory"] = {
        "type": "stdio",
        "command": python,
        "args": common + ["mcp", "--agent", "claude"],
    }
    write_json(claude_file, value)
    for agent, path in [
        ("codex", native["codex"] / "hooks.json"),
        ("claude", native["claude"] / "settings.json"),
    ]:
        backup(path, backups)
        settings = read_json(path)
        if agent == "claude":
            settings["autoMemoryEnabled"] = False
        hooks = settings.setdefault("hooks", {})
        hook_command = command([python] + common + ["hook", "--agent", agent])
        for event in ("SessionStart", "PreToolUse", "Stop", "PreCompact", "SessionEnd"):
            current = hooks.setdefault(event, [])
            current[:] = [
                group
                for group in current
                if not any(
                    "shared-memory.py" in h.get("command", "")
                    for h in group.get("hooks", [])
                )
            ]
            handler = {"type": "command", "command": hook_command, "timeout": 10}
            if agent == "codex" and event in ("SessionStart", "PreToolUse"):
                handler["additionalContextLimit"] = 4000
            group = {"hooks": [handler]}
            if event == "SessionStart":
                group["matcher"] = "startup|resume|clear|compact|fork"
            if event == "PreToolUse":
                # Path-bound rules are injected when a file is about to change.
                group["matcher"] = (
                    "Write|Edit|MultiEdit|NotebookEdit" if agent == "claude" else "Edit|Write|apply_patch"
                )
            current.append(group)
        write_json(path, settings)
    for path in (native["codex"] / "AGENTS.md", native["claude"] / "CLAUDE.md"):
        backup(path, backups)
        old = path.read_text(encoding="utf-8") if path.is_file() else ""
        start, end = "<!-- shared-memory:start -->", "<!-- shared-memory:end -->"
        if start in old and end in old:
            old = old[: old.index(start)] + old[old.index(end) + len(end) :]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            old.rstrip() + "\n\n" + start + "\n" + GUIDANCE + "\n" + end + "\n",
            encoding="utf-8",
        )
    result = {
        "configured": ["codex", "claude"],
        "backups": str(backups),
        "hook_trust_changed": False,
        "codex_hook_review_required": True,
        "restart_clients_to_load_new_mcp": True,
    }
    if daemon:
        result["sync_service"] = install_daemon(base, python, common)
    return result


def install_daemon(base, python, common):
    arguments = [python] + common + ["sync", "--watch", "--interval", "30"]
    if sys.platform == "darwin":
        path = Path.home() / "Library/LaunchAgents/com.teknova.shared-memory-sync.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        label = "com.teknova.shared-memory-sync"
        value = {
            "Label": label,
            "ProgramArguments": arguments,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 15,
            "StandardOutPath": str(base / "sync.log"),
            "StandardErrorPath": str(base / "sync-error.log"),
        }
        path.write_bytes(plistlib.dumps(value))
        path.chmod(0o600)
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], capture_output=True
        )
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
            capture_output=True,
        )
        return {"kind": "launchagent", "installed": result.returncode == 0}
    if sys.platform == "linux":
        system = os.geteuid() == 0
        directory = (
            Path("/etc/systemd/system")
            if system
            else Path.home() / ".config/systemd/user"
        )
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "shared-memory-sync.service"
        executable = " ".join(
            '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
            for arg in arguments
        )
        path.write_text(
            f"[Unit]\nDescription=Incremental coding-session memory exporter\nAfter=network-online.target\n[Service]\nExecStart={executable}\nRestart=on-failure\nRestartSec=15\nUMask=0077\n[Install]\nWantedBy={'multi-user.target' if system else 'default.target'}\n"
        )
        prefix = ["systemctl"] + ([] if system else ["--user"])
        first = subprocess.run(prefix + ["daemon-reload"], capture_output=True)
        result = (
            subprocess.run(prefix + ["enable", "--now", path.name], capture_output=True)
            if first.returncode == 0
            else first
        )
        if result.returncode == 0:
            result = subprocess.run(
                prefix + ["restart", path.name], capture_output=True
            )
        return {"kind": "systemd", "installed": result.returncode == 0}
    if os.name == "nt":
        task_command = subprocess.list2cmdline(arguments)
        result = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/TN",
                "SharedMemorySync",
                "/SC",
                "ONLOGON",
                "/TR",
                task_command,
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            subprocess.run(
                ["schtasks", "/Run", "/TN", "SharedMemorySync"], capture_output=True
            )
        return {
            "kind": "windows-task",
            "installed": result.returncode == 0,
            "manual_run_if_denied": task_command,
        }
    return {"kind": "unsupported", "installed": False}
