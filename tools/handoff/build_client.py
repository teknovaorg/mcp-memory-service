#!/usr/bin/env python3
"""Build a portable Python zipapp containing no models or third-party packages."""

from pathlib import Path
import shutil
import sys
import tempfile
import zipapp

root = Path(__file__).resolve().parents[2]
destination = Path(
    sys.argv[1] if len(sys.argv) > 1 else root / "dist/shared-memory.pyz"
)
destination.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="shared-memory-build-") as directory:
    stage = Path(directory)
    shutil.copytree(
        root / "client/shared_memory",
        stage / "shared_memory",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    package = stage / "mcp_memory_service/handoff"
    package.mkdir(parents=True)
    (package.parent / "__init__.py").touch()
    (package / "__init__.py").touch()
    shutil.copyfile(
        root / "src/mcp_memory_service/handoff/redact.py", package / "redact.py"
    )
    zipapp.create_archive(
        stage,
        destination,
        main="shared_memory.__main__:entry",
        interpreter="/usr/bin/env python3",
        compressed=True,
    )
print(destination)
