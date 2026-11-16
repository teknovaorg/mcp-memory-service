"""Local acknowledgement cursors, isolated from the assistants' own databases."""

import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager


@contextmanager
def sync_lock(path):
    """Serialize hooks and background passes across processes on all three OSes."""
    path = Path(path).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if __import__("os").name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS sources (source TEXT PRIMARY KEY,value TEXT NOT NULL)"
            )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def connect(self):
        return sqlite3.connect(self.path, timeout=15)

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM sources WHERE source=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT INTO sources VALUES (?,?) ON CONFLICT(source) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
