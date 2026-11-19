#!/usr/bin/env python3
"""Assemble fork release artifacts with a SHA-256 manifest.

Builds the wheel and the portable client, copies the installer and helper
tools, and writes manifest.json in the layout bootstrap.py verifies. Nothing
is uploaded; publish the directory contents as GitHub release assets after
the private publication scan.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ("bootstrap.py", "manage.py", "requirements-hub.lock", "verify_live.py", "verify_export.py")


def run(args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, help="Release tag, for example v12.10.0-teknova.9")
    parser.add_argument("--repository", default="teknovaorg/mcp-memory-service")
    parser.add_argument("--upstream", default="v12.10.0")
    parser.add_argument("--output", type=Path, required=True, help="Empty or new directory for the artifacts")
    parser.add_argument("--notes", type=Path, help="Release notes file copied as release-notes.md")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit("Output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise SystemExit("Commit or stash local changes before building a release")
    run(["uv", "build", "--wheel", "--out-dir", output], cwd=ROOT)
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit("Expected exactly one wheel")
    run([sys.executable, ROOT / "tools/handoff/build_client.py", output / "shared-memory.pyz"], cwd=ROOT)
    for name in TOOLS:
        shutil.copyfile(ROOT / "tools/handoff" / name, output / name)
    if args.notes:
        shutil.copyfile(args.notes, output / "release-notes.md")
    artifacts = {}
    for path in sorted(output.iterdir()):
        if path.name in ("manifest.json", "release-notes.md") or path.name.startswith("."):
            continue
        artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "repository": args.repository,
        "release": args.release,
        "commit": commit,
        "schema": 1,
        "upstream": args.upstream,
        "artifacts": artifacts,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "artifacts": sorted(artifacts)}, indent=2))


if __name__ == "__main__":
    main()
