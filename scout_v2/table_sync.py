"""Coalesced state-change notifications and independent Feishu table scheduling."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import weakref

TABLES = frozenset({"pr", "repos", "scan", "candidates"})
SOURCE_TABLES = {
    "queue/candidates.json": {"scan", "candidates"},
    "state/seen.json": {"scan"},
    "configs/repositories.yaml": {"repos"},
    "configs/lane_overrides.yaml": {"repos"},
    "state/pool_audit.json": {"repos"},
}
_feeds = weakref.WeakValueDictionary()
_feeds_lock = threading.Lock()


def _key(root):
    return os.path.normcase(os.path.abspath(root))


class TableChanges:
    def __init__(self, root: Path):
        self.root = root
        self.wake = threading.Event()
        self._lock = threading.Lock()
        self._dirty = set()
        self._stamps = {}
        self._versions = {}

    def mark(self, names):
        with self._lock:
            names = set(names) & TABLES
            self._dirty.update(names)
            for name in names:
                self._versions[name] = self._versions.get(name, 0) + 1
            self.wake.set()

    def version(self, name):
        with self._lock:
            return self._versions.get(name, 0)

    def take(self):
        with self._lock:
            names, self._dirty = self._dirty, set()
            self.wake.clear()
            return names

    def poll_files(self):
        # Other processes and editors do not share the in-process event feed.
        for name, tables in SOURCE_TABLES.items():
            try:
                stat = (self.root / name).stat()
                stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            except FileNotFoundError:
                stamp = None
            if name in self._stamps and self._stamps[name] != stamp:
                self.mark(tables)
            self._stamps[name] = stamp


def subscribe(root: Path) -> TableChanges:
    with _feeds_lock:
        key = _key(root)
        feed = _feeds.get(key)
        if feed is None:
            feed = TableChanges(Path(root))
            _feeds[key] = feed
        return feed


def notify(root: Path, names):
    with _feeds_lock:
        feed = _feeds.get(_key(root))
    if feed is not None:
        feed.mark(names)


def notify_path(path: Path):
    path = Path(path)
    tables = SOURCE_TABLES.get(path.parent.name + "/" + path.name)
    if tables:
        notify(path.parent.parent, tables)


class TableSyncSchedule:
    """One in-flight snapshot per table; failures retain dirty work with backoff."""
    def __init__(self, feed, executor, sync, *, full_interval=900, pr_interval=30):
        self.feed, self.executor, self.sync = feed, executor, sync
        self.full_interval, self.pr_interval = full_interval, pr_interval
        self.full_at = self.pr_at = self.activity_at = 0
        self.pending, self.reconcile = set(), set()
        self.inflight, self.retry_at, self.failures = {}, {}, {}

    def tick(self, now):
        self.feed.poll_files()
        self.pending.update(self.feed.take())
        if now >= self.full_at:
            self.pending.update(TABLES)
            self.reconcile.update(TABLES)
            self.full_at = now + self.full_interval
        if now >= self.pr_at:
            self.pending.add("pr")
            self.pr_at = now + self.pr_interval
        if now >= self.activity_at:
            # Detect Actions starts even before the controller's next collection.
            self.pending.update({"scan", "candidates"})
            self.activity_at = now + 10
        for name, (future, full) in list(self.inflight.items()):
            if not future.done():
                continue
            try:
                result = future.result()
                ok = bool(result) and not result.get("error") and not isinstance(result.get("skipped"), str)
            except Exception:
                ok = False
            del self.inflight[name]
            if ok:
                self.retry_at.pop(name, None)
                self.failures.pop(name, None)
            else:
                count = self.failures.get(name, 0) + 1
                self.failures[name] = count
                self.retry_at[name] = now + min(60, 5 * 2 ** min(count - 1, 4))
                self.pending.add(name)
                if full:
                    self.reconcile.add(name)
        for name in sorted(self.pending - self.inflight.keys()):
            if now < self.retry_at.get(name, 0):
                continue
            full = name in self.reconcile
            future = self.executor.submit(self.sync, name, full)
            self.inflight[name] = future, full
            self.pending.discard(name)
            self.reconcile.discard(name)
            future.add_done_callback(lambda _: self.feed.wake.set())
