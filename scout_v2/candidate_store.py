"""Record-level candidate storage with a one-time JSON import."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from collections.abc import MutableMapping
from contextlib import closing, contextmanager
from pathlib import Path

LEGACY_CANDIDATES = "queue/candidates.json"
CANDIDATE_DB = "state/candidates.sqlite3"
CANDIDATE_SOURCES = (CANDIDATE_DB, CANDIDATE_DB + "-wal", LEGACY_CANDIDATES)


def candidate_source(root: Path) -> Path:
    db = root / CANDIDATE_DB
    return db if db.is_file() else root / LEGACY_CANDIDATES


def candidate_store_exists(root: Path) -> bool:
    return candidate_source(root).is_file()


def _encode(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def read_candidates(root: Path, *, statuses=None, keys=None, fields=None, missing_ok=True) -> dict:
    """Return candidates in queue order, optionally projecting fields for a view."""
    path = candidate_source(root)
    statuses = None if statuses is None else tuple(statuses)
    keys = None if keys is None else tuple(keys)
    fields = None if fields is None else tuple(fields)
    if path.name != "candidates.sqlite3":
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if not missing_ok:
                raise
            return {}
        if keys is None and statuses is None and fields is None:
            return entries
        selected = None if keys is None else set(keys)
        return {key: entry if fields is None else {field: entry.get(field) for field in fields}
                for key, entry in entries.items()
                if (selected is None or key in selected)
                and (statuses is None or entry.get("status") in statuses)}
    # Keep the number of SQL parameters bounded on SQLite builds with the
    # original 999-variable limit. Large key sets use a single ordered scan.
    conditions, params = [], []
    payload = "payload"
    if fields is not None:
        payload = "json_object(" + ",".join("?,json(payload -> ?)" for _ in fields) + ")"
        params.extend(param for field in fields for param in (field, "$." + field))
    for column, values in (("status", statuses), ("key", keys)):
        if values is not None and len(params) + len(values) <= 900:
            if not values:
                return {}
            conditions.append(f"{column} IN ({','.join('?' for _ in values)})")
            params.extend(values)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    selected = None if keys is None else set(keys)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as conn:
        return {key: json.loads(record) for key, status, record in conn.execute(
                    f"SELECT key,status,{payload} FROM candidates" + where + " ORDER BY rowid", params)
                if (selected is None or key in selected)
                and (statuses is None or status in statuses)}


def get_candidate(root: Path, key: str) -> dict | None:
    return read_candidates(root, keys=(key,)).get(key)


class _CandidateRows(MutableMapping):
    """Load and track mutable records on demand within the writer transaction."""

    def __init__(self, conn):
        self._conn = conn
        self._keys = dict(conn.execute("SELECT key,status FROM candidates ORDER BY rowid"))
        self._entries = {}
        self._before = {}
        self._deleted = set()

    def __contains__(self, key):
        return key in self._keys

    def __len__(self):
        return len(self._keys)

    def __iter__(self):
        return iter(self._keys)

    def __getitem__(self, key):
        if key not in self._keys:
            raise KeyError(key)
        if key not in self._entries:
            payload, = self._conn.execute("SELECT payload FROM candidates WHERE key=?", (key,)).fetchone()
            self._before[key] = payload
            self._entries[key] = json.loads(payload)
        return self._entries[key]

    def __setitem__(self, key, value):
        if key in self._keys and key not in self._entries:
            self[key]  # Identify an unchanged replacement without a write.
        self._keys[key] = value.get("status")
        self._entries[key] = value

    def __delitem__(self, key):
        del self._keys[key]
        self._entries.pop(key, None)
        self._before.pop(key, None)
        self._deleted.add(key)

    def matching(self, statuses):
        for key, stored_status in self._keys.items():
            entry = self._entries.get(key)
            if (entry.get("status") if entry is not None else stored_status) in statuses:
                yield key, self[key]

    def save(self):
        changed = 0
        for key in self._deleted:
            changed += self._conn.execute("DELETE FROM candidates WHERE key=?", (key,)).rowcount
        for key, entry in self._entries.items():
            payload = _encode(entry)
            if payload == self._before.get(key):
                continue
            self._conn.execute(
                "INSERT INTO candidates(key,status,payload) VALUES (?,?,?) ON CONFLICT(key) "
                "DO UPDATE SET status=excluded.status,payload=excluded.payload",
                (key, entry.get("status"), payload),
            )
            changed += 1
        return changed


def candidate_items(queue, statuses):
    """Select matching mutable records without decoding inactive history."""
    if isinstance(queue, _CandidateRows):
        return queue.matching(statuses)
    return ((key, entry) for key, entry in queue.items() if entry.get("status") in statuses)


@contextmanager
def sqlite_candidate_transaction(root: Path):
    from .state_store import _write_transaction
    from .table_sync import notify_path

    path = root / CANDIDATE_DB
    with closing(sqlite3.connect(path, timeout=60, isolation_level=None)) as conn:
        with _write_transaction(conn, path, "candidate rows update"):
            queue = _CandidateRows(conn)
            yield queue
            changed = queue.save()
    if changed:
        notify_path(path)


def migrate_candidates(root: Path) -> int | None:
    """Publish the new database only after every legacy record is committed."""
    from .state_store import _write_transaction

    path = root / CANDIDATE_DB
    if path.is_file():
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.parent / "queue_lock.sqlite3"
    with closing(sqlite3.connect(lock_path, timeout=60, isolation_level=None)) as lock:
        with _write_transaction(lock, lock_path, "candidate storage migration"):
            if path.is_file():
                return None
            queue = read_candidates(root)
            fd, name = tempfile.mkstemp(prefix="candidates-import-", suffix=".sqlite3", dir=path.parent)
            os.close(fd)
            temporary = Path(name)
            try:
                with closing(sqlite3.connect(temporary)) as conn, conn:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("CREATE TABLE candidates (key TEXT PRIMARY KEY,status TEXT,payload TEXT NOT NULL)")
                    conn.execute("CREATE INDEX candidates_status ON candidates(status)")
                    conn.executemany("INSERT INTO candidates VALUES (?,?,?)",
                                     ((key, entry.get("status"), _encode(entry)) for key, entry in queue.items()))
                os.replace(temporary, path)
                if os.name == "posix":
                    directory = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
            return len(queue)


if __name__ == "__main__":
    import sys
    count = migrate_candidates(Path(sys.argv[1]))
    if count is not None:
        print(f"[entrypoint] migrated {count} candidate records to SQLite", flush=True)
