"""Durable inbox, daily start ledger, job state and serialized JSON queue writes.

Queue JSON remains compatible with readers; queue_transaction owns runtime mutations.
ClaimLedger retains every started repair for its Asia/Shanghai business day.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo
from contextlib import contextmanager

JOB_STATES = ("queued", "leased", "running", "collecting",
              "succeeded", "failed", "inconclusive", "blocked")
STAGED_CANDIDATE_LIMIT = 10
STAGED_FULL_REASON = "候选列表已满"


def staged_candidate_count(queue: dict) -> int:
    return sum(entry.get("status") == "staged" and not entry.get("direct_start_reserved_at")
               for entry in queue.values())


def direct_start_available(root: Path, queue: dict, tz_name: str, cap: int) -> bool:
    """Call under queue_transaction: one immediate handoff shares the execution slot.

    A nonblocking probe also respects a pipeline still screening or reconciling,
    before its remote execution has been recorded in the candidate JSON.
    """
    if any(e.get("status") in ("staged", "running") and (
            e.get("status") == "running" or e.get("dispatch_started_at")
            or e.get("direct_start_reserved_at")) for e in queue.values()):
        return False
    conn = sqlite3.connect(str(root / "state/dispatcher.sqlite3"), timeout=0, isolation_level=None)
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower():
                return False
            raise
    finally:
        conn.close()
    ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
    try:
        day = business_now(tz_name).date().isoformat()
        if ledger.pending_publication() is not None or ledger.quota_used(day) >= cap:
            return False
        # Reuse exact dispatch eligibility, including retry backoff and refills.
        from .workers.auto_pipeline import _discovered_today, _order_candidates
        today = [(key, entry) for key, entry in queue.items()
                 if _discovered_today(entry) and not entry.get("blockers")
                 and (entry.get("ai_verdict") or {}).get("usable") is True]
        refill = day if ledger.has_publication_release(day) else ""
        return not _order_candidates(today, refill_day=refill)
    finally:
        ledger.close()


def rotate_staged_queue(root: Path, queue: dict, *, keep=(), protected=(), replacement: str = "") -> list[str]:
    """Under queue_transaction, retain the configured number of newest waiters."""
    from .console_models import runtime_limit

    limit = runtime_limit(root, "candidate_pool_size", STAGED_CANDIDATE_LIMIT)
    protected = {key.lower() for key in (*keep, *protected)}
    db = root / "state/scout_v2.sqlite3"
    if db.exists():
        import re
        with sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            try:
                for repo, head in conn.execute("SELECT repo,head FROM publications WHERE day IS NULL"):
                    match = re.search(r":fix/issue-(\d+)-", head)
                    if match:
                        protected.add(f"{repo}#{match[1]}".lower())
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise
    waiting = [(key, entry) for key, entry in queue.items()
               if entry.get("status") == "staged" and key.lower() not in protected
               and not entry.get("dispatch_started_at") and not entry.get("direct_start_reserved_at")]
    waiting.sort(key=lambda item: candidate_fifo_key(*item))
    excess = max(0, staged_candidate_count(queue) - limit)
    now = business_now().isoformat(timespec="microseconds")
    removed = []
    for key, entry in waiting[:excess]:
        entry.update(status="queue_evicted", updated_at=now, evicted_at=now,
                     evicted_by=replacement, abort_reason=f"暂存满 {limit} 条，按入队时间清除最早候选")
        removed.append(key)
    return removed


def candidate_fifo_key(key: str, entry: dict) -> tuple[float, str]:
    stamp = entry.get("staged_at") or entry.get("discovered_at")
    try:
        then = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if then.tzinfo is None:
            then = then.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return then.timestamp(), key
    except (AttributeError, TypeError, ValueError):
        return 0.0, key


def publication_quota_remaining(root: Path, tz_name: str, cap: int) -> int:
    if cap <= 0:
        return 10 ** 6
    ledger = ClaimLedger(root / "state/scout_v2.sqlite3")
    try:
        return max(0, cap - ledger.quota_used(business_now(tz_name).date().isoformat()))
    finally:
        ledger.close()


def business_day(ts: dt.datetime, tz_name: str) -> str:
    """把时间戳换算到业务时区的日期（YYYY-MM-DD）。

    worklog 里的 ts 是裸本地时间（naive），按既有时区语义处理，不做 UTC 换算偷改日期。
    """
    if ts.tzinfo is None:
        return ts.date().isoformat()
    return ts.astimezone(ZoneInfo(tz_name)).date().isoformat()


def now_local_iso() -> str:
    return business_now().isoformat(timespec="seconds")


def business_now(tz_name: str = "Asia/Shanghai") -> dt.datetime:
    return dt.datetime.now(ZoneInfo(tz_name))


@contextmanager
def queue_transaction(root: Path, *, path: Path | None = None, backup: bool = True):
    """Serialize queue read/modify/write across threads and processes.

    A separate SQLite lock avoids nesting the quota database's transactions.
    Network calls must happen outside this short transaction.
    """
    root = Path(root)
    (root / "state").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(root / "state/queue_lock.sqlite3"), timeout=60,
                           isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        path = path or root / "queue/candidates.json"
        queue = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        original = json.dumps(queue, ensure_ascii=False, sort_keys=True)
        yield queue
        if original != json.dumps(queue, ensure_ascii=False, sort_keys=True):
            atomic_json_write(path, queue, backup=backup)
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


class IssueInbox:
    """Durable ingestion; HTTP acknowledges storage, never an AI network call."""

    def __init__(self, root: Path):
        self.root = Path(root)
        (self.root / "state").mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.root / "state/issue_inbox.sqlite3"),
                                    timeout=30, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS inbox ("
                          "key TEXT PRIMARY KEY, payload TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,"
                          "ready REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,"
                          "attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')")
        if "done_version" not in {r[1] for r in self.conn.execute("PRAGMA table_info(inbox)")}:
            try:
                self.conn.execute("ALTER TABLE inbox ADD COLUMN done_version INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                if "done_version" not in {r[1] for r in self.conn.execute("PRAGMA table_info(inbox)")}:
                    raise


    def close(self):
        self.conn.close()

    def put(self, issues: list[dict]) -> int:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for issue in issues:
                key = f"{issue['repo']}#{issue['number']}"
                payload = json.dumps(issue, ensure_ascii=False)
                self.conn.execute("INSERT INTO inbox(key,payload) VALUES (?,?) "
                                  "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
                                  "version=inbox.version+1,ready=0,error='' WHERE inbox.payload<>excluded.payload",
                                  (key, payload))
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return len(issues)

    def take(self):
        import time
        now = time.time()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT key,payload,version,attempts FROM inbox "
                                    "WHERE done_version<version AND ready<=? AND lease_until<=? ORDER BY rowid LIMIT 1",
                                    (now, now)).fetchone()
            if row:
                self.conn.execute("UPDATE inbox SET lease_until=? WHERE key=?", (now + 900, row[0]))
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return row

    def finish(self, key, version, error="", attempts=0):
        import time
        if error:
            self.conn.execute("UPDATE inbox SET ready=?,lease_until=0,attempts=attempts+1,error=? "
                              "WHERE key=? AND version=?", (time.time() + min(3600, 30 * 2 ** min(attempts, 7)),
                                                            error[:300], key, version))
            self.conn.execute("UPDATE inbox SET lease_until=0 WHERE key=? AND version<>?", (key, version))
        else:
            # Keep a tombstone: overlapping Worker windows must not refetch full
            # GitHub context for every already-processed payload every minute.
            self.conn.execute("UPDATE inbox SET done_version=MAX(done_version,?),lease_until=0,attempts=0,error='' WHERE key=?",
                              (version, key))


def atomic_json_write(path: Path, data, verify: bool = True, backup: bool = True) -> None:
    """临时文件 + fsync + os.replace + 回读校验 + 上一版滚动 .bak。

    discover.py 原版没有 fsync/校验/.bak——0x00 损坏事件后统一走这里。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=1)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    bak = path.with_suffix(path.suffix + ".bak")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        if verify:
            json.loads(Path(tmp_name).read_text(encoding="utf-8"))  # 破损直接抛，不落地
        if backup and path.exists():
            # 先备份旧版再替换——.bak 必须是"上一版"，否则零价值
            try:
                bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass
        os.replace(tmp_name, path)
        if verify:
            # 10-22 0x00 事故：NTFS 预分配 + 中断可在 replace 后留下填零文件，
            # 落地后必须回读校验最终文件本身
            json.loads(path.read_text(encoding="utf-8"))
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


class Worklog:
    """append-only 审计账本（state/worklog.jsonl）。只追加与读取，永不改写历史行。"""

    CLAIM_ACTIONS = {"claim", "awaiting_assignment", "claimed"}

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # 历史账本容错：坏行跳过但保留在文件里
        return out

    def claimed_today(self, tz_name: str, now: dt.datetime | None = None,
                      skip_targets: set[str] | None = None) -> list[dict]:
        """当日（业务时区）已有 claim / awaiting_assignment 动作的记录。

        skip_targets：不计入配额的目标（如内部测试仓库）。P5 闭环演练会在自有
        fixture 上反复认领，那些不该占用真实仓库每天 1 个的额度——否则演练几次
        就把当天真实机会挤掉了（2026-10-28 实测：fixture 演练 6 次，真实 issue
        被日闸拦下）。
        """
        now = now or business_now(tz_name)
        today = business_day(now, tz_name)
        skip = skip_targets or set()
        hits = []
        for rec in self.read_all():
            ts_raw = rec.get("ts")
            if not ts_raw:
                continue
            if rec.get("target") in skip:
                continue
            try:
                ts = dt.datetime.fromisoformat(ts_raw)
            except ValueError:
                continue
            if business_day(ts, tz_name) == today and rec.get("action") in self.CLAIM_ACTIONS:
                hits.append(rec)
        return hits


class ClaimLedger:
    """SQLite 认领账本：记录当日已认领的候选，支持每日多条上限。

    2026-10-30（用户裁定）：从"每日至多 1 个"改成按配置的每日上限。
    表从 `day` 单主键改成 `(day, candidate_id)` 复合主键，因此同一天可以
    记录多条；上限由调用方传入的 max_per_day 决定。
    旧库自动迁移：检测到旧的单主键表就重建，已有记录按新结构搬过去。

    SQLite 连接必须打开在同一文件上（BEGIN IMMEDIATE 提供写序列化）。
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        """把旧的 day 单主键表升级成 (day, candidate_id) 复合主键。

        并发安全的写法：绝不 DROP 任何表。
        2026-10-30 实测事故：早期实现先 `DROP TABLE IF EXISTS claim_days_old`
        再可能 `ALTER TABLE ... RENAME`，多个进程同时 __init__ 时会互相看到
        对方正在迁移的中间态——结果 4 个进程的 INSERT 都返回成功，但表被
        重建过，最终只剩 1 行。已成功的进程以为自己占了名额，实际记录已被
        后续迁移覆盖。迁移只做一次，且用可重入的幂等判断。
        """
        cols = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(claim_days)").fetchall()]
        if not cols:
            # 表不存在：建。IF NOT EXISTS 在 DDL 层是原子的，并发安全。
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS claim_days ("
                " day TEXT NOT NULL, candidate_id TEXT NOT NULL,"
                " action TEXT NOT NULL, ts TEXT NOT NULL,"
                " PRIMARY KEY (day, candidate_id))")
            return
        pk_cols = [r[1] for r in self._conn.execute(
            "PRAGMA table_info(claim_days)").fetchall() if r[5]]
        if pk_cols == ["day", "candidate_id"]:
            return          # 已是新结构

        # 旧结构：在一个事务里搬数据。BEGIN IMMEDIATE 拿写锁，
        # 其他进程会等它提交，不会看到中间态。
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            # 二次确认：拿到锁之后别的进程可能已经迁移完了
            cols2 = [r[1] for r in self._conn.execute(
                "PRAGMA table_info(claim_days)").fetchall()]
            pk2 = [r[1] for r in self._conn.execute(
                "PRAGMA table_info(claim_days)").fetchall() if r[5]]
            if not cols2 or pk2 == ["day", "candidate_id"]:
                self._conn.execute("COMMIT")
                return
            rows = self._conn.execute(
                "SELECT day, candidate_id, action, ts FROM claim_days").fetchall()
            self._conn.execute("ALTER TABLE claim_days RENAME TO claim_days_new")
            self._conn.execute(
                "CREATE TABLE claim_days ("
                " day TEXT NOT NULL, candidate_id TEXT NOT NULL,"
                " action TEXT NOT NULL, ts TEXT NOT NULL,"
                " PRIMARY KEY (day, candidate_id))")
            self._conn.executemany(
                "INSERT OR IGNORE INTO claim_days(day, candidate_id, action, ts)"
                " VALUES (?,?,?,?)", rows)
            self._conn.execute("DROP TABLE IF EXISTS claim_days_new")
            self._conn.execute("COMMIT")
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def close(self) -> None:
        self._conn.close()

    def _publication_table(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS publications (job_id TEXT PRIMARY KEY, "
            "repo TEXT NOT NULL, head TEXT NOT NULL, runner_repo TEXT NOT NULL, "
            "remote_id TEXT, expected_url TEXT, day TEXT, url TEXT UNIQUE)"
        )

        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS publication_status (url TEXT PRIMARY KEY, "
            "state TEXT NOT NULL, closed_day TEXT)")

    def published_today(self, tz_name: str) -> int:
        self._publication_table()
        day = business_now(tz_name).date().isoformat()
        return self._conn.execute("SELECT COUNT(*) FROM publications WHERE day=?",
                                  (day,)).fetchone()[0]

    def quota_used(self, day: str) -> int:
        self._publication_table()
        return self._conn.execute(
            "SELECT COUNT(*) FROM publications p WHERE p.day=? AND NOT EXISTS ("
            "SELECT 1 FROM publication_status s WHERE s.url=p.url AND s.state='closed' "
            "AND s.closed_day=p.day)", (day,)).fetchone()[0]

    def has_publication_release(self, day: str, *, min_published: int = 1) -> bool:
        self._publication_table()
        return self._conn.execute(
            "SELECT 1 FROM publications p JOIN publication_status s USING(url) "
            "WHERE p.day=? AND s.state='closed' AND s.closed_day=p.day "
            "AND (SELECT COUNT(*) FROM publications WHERE day=p.day)>=? LIMIT 1",
            (day, min_published)).fetchone() is not None

    def publications_to_check(self, day: str) -> list[tuple[str, str]]:
        self._publication_table()
        return self._conn.execute(
            "SELECT p.repo,p.url FROM publications p LEFT JOIN publication_status s USING(url) "
            "WHERE p.day=? AND p.url IS NOT NULL AND COALESCE(s.state,'open')!='merged'",
            (day,)).fetchall()

    def record_publication_status(self, url: str, state: str, closed_day: str | None) -> bool:
        return bool(self._conn.execute(
            "INSERT INTO publication_status(url,state,closed_day) VALUES (?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET state=excluded.state,closed_day=excluded.closed_day "
            "WHERE publication_status.state!=excluded.state "
            "OR publication_status.closed_day IS NOT excluded.closed_day",
            (url, state, closed_day)).rowcount)

    def pending_publication(self) -> tuple | None:
        self._publication_table()
        return self._conn.execute(
            "SELECT job_id, repo, head, runner_repo, remote_id, expected_url "
            "FROM publications WHERE day IS NULL"
        ).fetchone()

    def reserve_publication(self, tz_name: str, cap: int, job_id: str, repo: str,
                            head: str, runner_repo: str) -> bool:
        # Serialize the final check with the status poller as well as dispatchers.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            allowed = (not self.pending_publication()
                       and self.quota_used(business_now(tz_name).date().isoformat()) < cap)
            if allowed:
                self._conn.execute(
                    "INSERT INTO publications(job_id,repo,head,runner_repo) VALUES (?,?,?,?)",
                    (job_id, repo, head, runner_repo))
            self._conn.execute("COMMIT")
            return allowed
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def set_publication_run(self, job_id: str, run_id: str) -> None:
        self._conn.execute("UPDATE publications SET remote_id=? WHERE job_id=?", (run_id, job_id))

    def expect_publication(self, job_id: str, url: str) -> None:
        self._conn.execute("UPDATE publications SET expected_url=? WHERE job_id=?", (url, job_id))

    def finish_publication(self, job_id: str, *, day: str = "", url: str = "") -> None:
        if day and url:
            self._conn.execute("UPDATE OR IGNORE publications SET day=?,url=? WHERE job_id=?",
                               (day, url, job_id))
        self._conn.execute("DELETE FROM publications WHERE job_id=? AND day IS NULL", (job_id,))

    def reconcile_legacy_starts(self, worklog: Path, tz_name: str) -> None:
        """One-time migration: failures refunded by the old code still count today."""
        import hashlib
        day = business_now(tz_name).date().isoformat()
        self._conn.execute("CREATE TABLE IF NOT EXISTS quota_migrations (name TEXT PRIMARY KEY)")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            name = "daily-starts-2026-11-06"
            if self._conn.execute("SELECT 1 FROM quota_migrations WHERE name=?", (name,)).fetchone():
                self._conn.execute("COMMIT")
                return
            covered = set()
            for index, line in enumerate(worklog.read_text(encoding="utf-8").splitlines() if worklog.exists() else []):
                try:
                    rec = json.loads(line)
                    ts = dt.datetime.fromisoformat(rec.get("ts", "").replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    continue
                if rec.get("action") != "claim" or business_day(ts, tz_name) != day:
                    continue
                cid = str(rec.get("target") or "")
                if not cid:
                    continue
                if cid not in covered:
                    key = cid
                    covered.add(cid)
                else:
                    key = "legacy-start:" + hashlib.sha256(f"{index}:{line}".encode()).hexdigest()
                self._conn.execute("INSERT OR IGNORE INTO claim_days(day,candidate_id,action,ts) VALUES (?,?,?,?)",
                                   (day, key, "claim", rec["ts"]))
            self._conn.execute("INSERT INTO quota_migrations VALUES (?)", (name,))
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def claimed_ids(self, day: str) -> list[str]:
        return [r[0] for r in self._conn.execute(
            "SELECT candidate_id FROM claim_days WHERE day=?", (day,)).fetchall()]

    def try_claim(self, tz_name: str, candidate_id: str, action: str = "claim",
                  now: dt.datetime | None = None,
                  max_per_day: int = 0) -> tuple[bool, str]:
        """尝试占用一个当日认领名额。返回 (ok, reason)。

        max_per_day <= 0 表示不限条数。
        BEGIN IMMEDIATE 串行化后：同一条候选已认领过 → 拒绝；当日已达上限 →
        拒绝；否则插入成功。
        """
        now = now or business_now(tz_name)
        day = business_day(now, tz_name)
        ts = now_local_iso()
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            existing = [r[0] for r in cur.execute(
                "SELECT candidate_id FROM claim_days WHERE day=?", (day,)).fetchall()]
            if candidate_id in existing:
                # 同日闸覆盖所有记录（含 abort）：同一条候选今天失败过就不再
                # 重试，防死循环保险丝之一（_todays_claim_count 是其二）。
                cur.execute("ROLLBACK")
                return False, f"already claimed on {day}: {candidate_id}"
            active = [r[0] for r in cur.execute(
                "SELECT candidate_id FROM claim_days WHERE day=? AND action='claim'",
                (day,)).fetchall()]
            if max_per_day > 0 and len(active) >= max_per_day:
                cur.execute("ROLLBACK")
                return False, (f"daily limit {max_per_day} reached on {day} "
                               f"({len(active)} claimed)")
            cur.execute("INSERT INTO claim_days(day, candidate_id, action, ts) VALUES (?,?,?,?)",
                        (day, candidate_id, action, ts))
            cur.execute("COMMIT")
            return True, "claimed"
        except sqlite3.IntegrityError as e:
            # 复合主键是最后一道保险：读-判-写之间存在时间窗，即使上面的
            # SELECT 没看到已有记录，INSERT 也会被唯一约束拦住。
            # 2026-10-30 实测：多进程并发时约 8/20 轮出现两个进程都拿到
            # "claimed"（BEGIN IMMEDIATE 未能完全封住 WAL 下的读取）。
            # 把约束冲突当成"已被认领"，两个赢家变成一真一假。
            try:
                cur.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            return False, f"already claimed on {day}: {candidate_id} ({e})"
        except BaseException:
            cur.execute("ROLLBACK")
            raise

    def count_today(self, tz_name: str, now: dt.datetime | None = None) -> int:
        """当日业务时区已用掉几个认领名额（只数 action='claim'）。

        额度判断必须读这张表而不是 worklog：表里有唯一约束，worklog 是
        追加日志，重复记录会让计数偏高、把额度误判成已用尽。
        abort 的记录不占额度（失败的认领要让下一条候选顶上），但留在表里
        维持 try_claim 的同日闸——见 release_claim。
        """
        now = now or business_now(tz_name)
        day = business_day(now, tz_name)
        row = self._conn.execute(
            "SELECT COUNT(*) FROM claim_days WHERE day=? AND action='claim'",
            (day,)).fetchone()
        return int(row[0] if row else 0)

    def release_claim(self, candidate_id: str,
                      tz_name: str = "Asia/Shanghai",
                      now: dt.datetime | None = None) -> bool:
        """abort 时退坑：记录**保留**但标记为 abort。返回是否有记录被标记。

        语义拆成两半（2026-11-06 实录）：
          - 占额：abort 的记录**不再占当日额度**——失败的认领要让下一条候选
            顶上（2026-10-31 裁定），额度数的是开出的 PR，count_today 只数
            action='claim'。
          - 防重：记录**必须留在表里**——之前这里是 DELETE，把 already-claimed
            闸一起删了。2026-11-06 队列恢复（误标 stage_expired 的候选被重置回
            new）之后，同一条候选当天被再次认领，RuVector#1292 和
            OpenViking#6823 各开出两个重复 PR。保留记录后 try_claim 的同日闸
            自然堵住。
        """
        now = now or business_now(tz_name)
        day = business_day(now, tz_name)
        cur = self._conn.execute(
            "UPDATE claim_days SET action='abort' "
            "WHERE day=? AND candidate_id=? AND action='claim'", (day, candidate_id))
        self._conn.commit()
        return cur.rowcount > 0

    def release_day(self, day: str | None = None) -> None:
        """运维/测试用的整日回滚：删掉某一天的全部占额记录。

        只用人工排障，不要在 pipeline 流程里调用——单个候选 abort 请用
        release_claim()。day=None 表示清空整张表，那是"重置全部额度"的
        破坏性操作，代码路径里不该出现。
        """
        if day is None:
            self._conn.execute("DELETE FROM claim_days")
        else:
            self._conn.execute("DELETE FROM claim_days WHERE day=?", (day,))
        self._conn.commit()


class BlockerRegistry:
    """上游写失败（403 等）blocker 登记：本轮/当日对同一对象不再重试同一写。"""

    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS blockers ("
            " key TEXT PRIMARY KEY, reason TEXT NOT NULL, ts TEXT NOT NULL)")

    def register(self, key: str, reason: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO blockers(key, reason, ts) VALUES (?,?,?)",
            (key, reason, now_local_iso()))

    def is_blocked(self, key: str) -> bool:
        row = self._conn.execute("SELECT reason FROM blockers WHERE key=?", (key,)).fetchone()
        return row is not None

    def reason(self, key: str) -> str | None:
        row = self._conn.execute("SELECT reason FROM blockers WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        self._conn.close()


class JobStore:
    """Job 状态持久化：queued → leased → running → collecting → 终态。幂等写入。"""

    TERMINAL = {"succeeded", "failed", "inconclusive", "blocked"}

    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            " job_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,"
            " state TEXT NOT NULL, lease_until TEXT, payload TEXT NOT NULL,"
            " result TEXT, updated_at TEXT NOT NULL)")

    def submit(self, job: dict) -> tuple[str, bool]:
        """按 idempotency_key 幂等入库。返回 (job_id, created)。已存在则原样返回。"""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO jobs(job_id, idempotency_key, state, lease_until,"
            " payload, result, updated_at) VALUES (?,?,?,NULL,?,NULL,?)",
            (job["job_id"], job["idempotency_key"], "queued",
             json.dumps(job, ensure_ascii=False), now_local_iso()))
        if cur.rowcount == 0:
            row = self._conn.execute(
                "SELECT job_id FROM jobs WHERE idempotency_key=?",
                (job["idempotency_key"],)).fetchone()
            return row[0], False
        return job["job_id"], True

    def get(self, job_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT job_id, idempotency_key, state, lease_until, payload, result, updated_at"
            " FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return {"job_id": row[0], "idempotency_key": row[1], "state": row[2],
                "lease_until": row[3], "payload": json.loads(row[4]),
                "result": json.loads(row[5]) if row[5] else None, "updated_at": row[6]}

    def find_by_idempotency(self, key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT job_id FROM jobs WHERE idempotency_key=?", (key,)).fetchone()
        return self.get(row[0]) if row else None

    def transition(self, job_id: str, to_state: str, lease_seconds: int | None = None) -> bool:
        """状态机推进；终态幂等（重复写同一终态返回 True，改终态返回 False）。"""
        if to_state not in JOB_STATES:
            raise ValueError(f"unknown state {to_state!r}")
        row = self._conn.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return False
        cur_state = row[0]
        if cur_state in self.TERMINAL:
            return cur_state == to_state
        lease_until = None
        if to_state in ("leased", "running") and lease_seconds:
            lease_until = (dt.datetime.now()
                           + dt.timedelta(seconds=lease_seconds)).isoformat(timespec="seconds")
        self._conn.execute(
            "UPDATE jobs SET state=?, lease_until=?, updated_at=? WHERE job_id=?",
            (to_state, lease_until, now_local_iso(), job_id))
        return True

    def expired_leases(self, now: dt.datetime | None = None) -> list[str]:
        """租约过期的 job_id 列表，交由回收任务重新排队（不自动重提交 GitHub 写）。"""
        now = now or dt.datetime.now()
        rows = self._conn.execute(
            "SELECT job_id, lease_until, state FROM jobs").fetchall()
        out = []
        for job_id, lease_until, state in rows:
            if state in ("leased", "running") and lease_until:
                if dt.datetime.fromisoformat(lease_until) < now:
                    out.append(job_id)
        return out

    def close(self) -> None:
        self._conn.close()
