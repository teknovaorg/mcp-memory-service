#!/usr/bin/env python3
"""Scan tracked files and release archives using a private local deny policy.

Only paths and counts are reported. Match values and private patterns never
appear in diagnostics or in committed fixtures.
"""

import argparse
import json
import re
import subprocess
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--artifacts", nargs="*", default=[])
    args = parser.parse_args()
    config = json.loads(Path(args.policy).read_text())
    rules = {
        name: re.compile(pattern, re.IGNORECASE)
        for name, pattern in config["patterns"].items()
    }
    findings = []

    def inspect(name, raw):
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            return
        matches = {
            key: len(rule.findall(value))
            for key, rule in rules.items()
            if rule.search(value)
        }
        if matches:
            findings.append({"path": name, "categories": matches})

    files = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    for name in files:
        if name and Path(name).is_file():
            inspect(name, Path(name).read_bytes())
    for name in args.artifacts:
        path = Path(name)
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for entry in archive.infolist():
                    if not entry.is_dir():
                        inspect(path.name + ":" + entry.filename, archive.read(entry))
        else:
            inspect(path.name, path.read_bytes())
    print(json.dumps({"passed": not findings, "findings": findings}))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
