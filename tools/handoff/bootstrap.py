#!/usr/bin/env python3
"""Install/update a verified GitHub release; private connection profiles stay local."""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

REPOSITORY = "teknovaorg/mcp-memory-service"
MODEL = "paraphrase-multilingual-MiniLM-L12-v2-uint8"
MODEL_REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"


def fetch(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "shared-memory-bootstrap"}
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def release(tag="latest"):
    route = "latest" if tag == "latest" else "tags/" + tag
    value = json.loads(
        fetch(f"https://api.github.com/repos/{REPOSITORY}/releases/{route}")
    )
    assets = {x["name"]: x["browser_download_url"] for x in value["assets"]}
    manifest = json.loads(fetch(assets["manifest.json"]))
    if manifest.get("repository") != REPOSITORY:
        raise RuntimeError("Unexpected release repository")
    return value, assets, manifest


def artifact(assets, manifest, name, destination):
    raw = fetch(assets[name])
    if hashlib.sha256(raw).hexdigest() != manifest["artifacts"][name]:
        raise RuntimeError("Release checksum mismatch")
    destination.write_bytes(raw)
    return destination


def run(args, **kwargs):
    result = subprocess.run(
        [str(x) for x in args], capture_output=True, text=True, **kwargs
    )
    if result.returncode:
        # Dependency managers and systemd may print environment or URLs on error.
        raise RuntimeError(
            f"Command failed: {Path(str(args[0])).name} (exit {result.returncode})"
        )
    return result


def uv_binary(prefix):
    existing = shutil.which("uv")
    if existing:
        return Path(existing)
    target = prefix / "uv"
    if target.is_file():
        return target
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "AMD64"):
        raise RuntimeError("Install uv first on this hub architecture")
    meta = json.loads(fetch("https://pypi.org/pypi/uv/0.13.8/json"))
    wheel = next(
        x
        for x in meta["urls"]
        if x["filename"].endswith("manylinux_2_17_x86_64.manylinux2014_x86_64.whl")
    )
    raw = fetch(wheel["url"])
    if hashlib.sha256(raw).hexdigest() != wheel["digests"]["sha256"]:
        raise RuntimeError("uv checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        member = next(x for x in archive.namelist() if x.endswith("/scripts/uv"))
        target.write_bytes(archive.read(member))
        target.chmod(0o755)
    return target


def ensure_model(data):
    folder = data / ".cache/mcp_memory/onnx_models" / MODEL
    folder.mkdir(parents=True, exist_ok=True)
    files = [
        (
            "onnx/model_quint8_avx2.onnx",
            "model.onnx",
            "98a01d88b7de996cdea58c32ca71208c09968d143798814b2ea09d3439dc334f",
        ),
        (
            "tokenizer.json",
            "tokenizer.json",
            "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8",
        ),
        (
            "sentencepiece.bpe.model",
            "sentencepiece.bpe.model",
            "cfc8146abe2a0488e9e2a0c56de7952f7c11ab059eca145a0a727afce0db2865",
        ),
    ]
    for source, name, expected in files:
        target = folder / name
        if (
            target.is_file()
            and hashlib.sha256(target.read_bytes()).hexdigest() == expected
        ):
            continue
        url = f"https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/resolve/{MODEL_REVISION}/{source}"
        raw = fetch(url)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise RuntimeError("Embedding model checksum mismatch")
        target.write_bytes(raw)
    (folder / "pinned-model.json").write_text(
        json.dumps(
            {
                "repository": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                "revision": MODEL_REVISION,
                "quantization": "quint8-avx2",
            }
        )
    )


def snapshot(data):
    source = data / "memory.sqlite3"
    if not source.is_file():
        return None
    directory = data / "backups"
    directory.mkdir(exist_ok=True, mode=0o700)
    target = directory / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3"
    )
    with (
        sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original,
        sqlite3.connect(target) as copy,
    ):
        original.backup(copy)
    target.chmod(0o600)
    target.with_suffix(".sha256").write_text(
        hashlib.sha256(target.read_bytes()).hexdigest() + "\n"
    )
    return target


def hub(args, info, assets, manifest):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Hub installation requires Linux root")
    prefix = Path(args.prefix)
    data = Path(args.data)
    settings = Path(args.settings)
    for directory in (prefix, data, settings):
        directory.mkdir(parents=True, exist_ok=True)
    tag = info["tag_name"]
    if any(
        x not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for x in tag
    ):
        raise RuntimeError("Invalid release tag")
    destination = prefix / "releases" / tag
    destination.mkdir(parents=True, exist_ok=True)
    wheel_name = next(name for name in manifest["artifacts"] if name.endswith(".whl"))
    wheel = artifact(assets, manifest, wheel_name, destination / wheel_name)
    lock = artifact(
        assets, manifest, "requirements-hub.lock", destination / "requirements-hub.lock"
    )
    uv = uv_binary(prefix)
    venv = destination / "venv"
    if not (venv / "bin/python").is_file():
        run([uv, "venv", venv, "--python", sys.executable])
    run(
        [
            uv,
            "pip",
            "install",
            "--python",
            venv / "bin/python",
            "--require-hashes",
            "-r",
            lock,
        ]
    )
    run([uv, "pip", "install", "--python", venv / "bin/python", "--no-deps", wheel])
    ensure_model(data)
    if subprocess.run(["id", "shared-memory"], capture_output=True).returncode:
        run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--home-dir",
                data,
                "--shell",
                "/usr/sbin/nologin",
                "shared-memory",
            ]
        )
    keyfile = settings / "api-key"
    if not keyfile.is_file():
        keyfile.write_text(secrets.token_urlsafe(40))
        keyfile.chmod(0o600)
    projects = settings / "projects.json"
    if not projects.is_file():
        projects.write_text('{"projects":{}}\n')
    projects.chmod(0o640)
    run(["chown", "root:shared-memory", projects])
    env = {
        "MCP_MEMORY_BASE_DIR": str(data),
        "MCP_MEMORY_SQLITE_PATH": str(data / "memory.sqlite3"),
        "MCP_MEMORY_STORAGE_BACKEND": "sqlite_vec",
        "MCP_MEMORY_USE_ONNX": "true",
        "MCP_MEMORY_TOKENIZER_BACKEND": "sentencepiece-xlmr",
        "MCP_EMBEDDING_MODEL": MODEL,
        "MCP_MEMORY_ONNX_ALLOW_DOWNLOAD": "0",
        "MCP_MEMORY_ALLOW_HASH_EMBEDDINGS": "0",
        "MCP_HANDOFF_REQUIRE_ONNX": "true",
        "MCP_HANDOFF_TOKENS_FILE": str(projects),
        "MCP_API_KEY": keyfile.read_text().strip(),
        "MCP_HTTP_HOST": "127.0.0.1",
        "MCP_HTTP_PORT": str(args.port),
        "MCP_MDNS_ENABLED": "false",
        "MCP_QUALITY_SYSTEM_ENABLED": "false",
        "MCP_QUALITY_AI_PROVIDER": "none",
        "MCP_CONSOLIDATION_ENABLED": "false",
        "MCP_AUTO_SUPERSEDE_ENABLED": "false",
        "MCP_HARVEST_ENABLED": "false",
        "MCP_BACKUP_ENABLED": "false",
        "TOKENIZERS_PARALLELISM": "false",
        "OMP_NUM_THREADS": "1",
    }
    section_policy = settings / "sections.json"
    if section_policy.is_file():
        env["MCP_HANDOFF_SECTIONS_FILE"] = str(section_policy)
    environment = settings / "hub.env"
    old_environment = environment.read_bytes() if environment.is_file() else None
    environment.write_text(
        "\n".join(f'{key}="{value}"' for key, value in env.items()) + "\n"
    )
    environment.chmod(0o600)
    run(["chown", "-R", "shared-memory:shared-memory", data])
    data.chmod(0o700)
    previous = prefix / "current"
    prior = previous.resolve() if previous.is_symlink() else None
    saved = snapshot(data)
    candidate = prefix / ".next"
    if candidate.is_symlink():
        candidate.unlink()
    candidate.symlink_to(destination)
    candidate.replace(previous)
    unit = Path("/etc/systemd/system/shared-memory.service")
    old_unit = unit.read_bytes() if unit.is_file() else None
    unit.write_text(f"""[Unit]
Description=Structured shared memory for coding assistants
After=network.target
[Service]
User=shared-memory
Group=shared-memory
EnvironmentFile={environment}
WorkingDirectory={data}
ExecStart={prefix}/current/venv/bin/memory server --http --http-host 127.0.0.1 --http-port {args.port}
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={data}
MemoryHigh=800M
MemoryMax=1200M
TasksMax=128
[Install]
WantedBy=multi-user.target
""")
    run(["systemctl", "daemon-reload"])
    run(["systemctl", "enable", "shared-memory.service"])
    run(["systemctl", "restart", "shared-memory.service"])
    ready = False
    for _ in range(50):
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{args.port}/api/health",
                headers={"X-API-Key": env["MCP_API_KEY"]},
            )
            with urllib.request.urlopen(req, timeout=2) as response:
                if response.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.5)
    if not ready:
        if prior:
            previous.unlink()
            previous.symlink_to(prior)
            if old_environment is not None:
                environment.write_bytes(old_environment)
            if old_unit is not None:
                unit.write_bytes(old_unit)
                run(["systemctl", "daemon-reload"])
            run(["systemctl", "restart", "shared-memory.service"])
        else:
            run(["systemctl", "stop", "shared-memory.service"])
        raise RuntimeError(
            "Hub health check failed; previous executable restored when available"
        )
    (prefix / "installed.json").write_text(
        json.dumps(
            {
                "release": tag,
                "commit": manifest["commit"],
                "schema": manifest["schema"],
                "backup": str(saved) if saved else None,
            }
        )
    )
    for name in ("bootstrap.py", "manage.py"):
        target = prefix / name
        temporary = prefix / (name + ".next")
        artifact(assets, manifest, name, temporary)
        temporary.chmod(0o700)
        temporary.replace(target)
    print(
        json.dumps(
            {
                "installed": True,
                "role": "hub",
                "release": tag,
                "loopback_port": args.port,
                "backup_created": bool(saved),
            }
        )
    )


def client(args, info, assets, manifest):
    if not args.registry or not Path(args.registry).is_file():
        raise RuntimeError(
            "A private connection registry is required; it is never downloaded from GitHub"
        )
    directory = Path(args.client_dir or Path(args.registry).parent)
    directory.mkdir(parents=True, exist_ok=True)
    was_installed = (directory / "installed.json").is_file()
    versions = directory / "releases"
    versions.mkdir(exist_ok=True)
    tag = info["tag_name"]
    if not tag or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        for c in tag
    ):
        raise RuntimeError("Invalid release tag")
    target = versions / (tag + ".pyz")
    temporary = directory / "shared-memory.next.pyz"
    artifact(assets, manifest, "shared-memory.pyz", temporary)
    # Validate executable structure before replacing a working client.
    with zipfile.ZipFile(temporary) as archive:
        if "shared_memory/__main__.py" not in archive.namelist():
            raise RuntimeError("Invalid client artifact")
    temporary.replace(target)
    bootstrap = directory / "bootstrap.py"
    artifact(assets, manifest, "bootstrap.py", bootstrap.with_suffix(".next.py"))
    bootstrap.with_suffix(".next.py").replace(bootstrap)
    record = {
        "release": info["tag_name"],
        "commit": manifest["commit"],
        "schema": manifest["schema"],
        "artifact": str(target),
    }
    record_path = directory / "installed.json"
    record_path.with_suffix(".next.json").write_text(json.dumps(record))
    record_path.with_suffix(".next.json").replace(record_path)
    launcher = directory / "shared-memory.py"
    launcher.write_text("""#!/usr/bin/env python3
import json, pathlib, runpy, sys
base = pathlib.Path(__file__).resolve().parent
installed = json.loads((base / "installed.json").read_text())
if sys.argv[1:2] == ["update"]:
    sys.argv = [str(base / "bootstrap.py"), "client", "--registry", str(base / "connections.json")] + sys.argv[2:]
    runpy.run_path(str(base / "bootstrap.py"), run_name="__main__")
else:
    sys.argv[0] = installed["artifact"]
    runpy.run_path(installed["artifact"], run_name="__main__")
""")
    launcher.chmod(0o700)
    if was_installed:
        run([sys.executable, launcher, "--registry", args.registry, "install"])
    print(json.dumps(dict(record, installed=True, role="client", client=str(target))))


def refresh_installer(info, assets, manifest):
    expected = manifest["artifacts"]["bootstrap.py"]
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == expected:
        return False
    with tempfile.TemporaryDirectory(prefix="shared-memory-installer-") as directory:
        installer = artifact(assets, manifest, "bootstrap.py", Path(directory) / "bootstrap.py")
        result = run([sys.executable, installer, *sys.argv[1:], "--release", info["tag_name"]])
        print(result.stdout, end="")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["hub", "client"])
    parser.add_argument("--release", default="latest")
    parser.add_argument("--prefix", default="/opt/shared-memory")
    parser.add_argument("--data", default="/var/lib/shared-memory")
    parser.add_argument("--settings", default="/etc/shared-memory")
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--registry")
    parser.add_argument("--client-dir")
    args = parser.parse_args()
    info, assets, manifest = release(args.release)
    if refresh_installer(info, assets, manifest):
        return
    (hub if args.role == "hub" else client)(args, info, assets, manifest)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            "Installation failed: "
            + (str(error) if isinstance(error, RuntimeError) else type(error).__name__),
            file=sys.stderr,
        )
        raise SystemExit(1)
