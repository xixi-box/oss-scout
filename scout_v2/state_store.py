"""状态存储：统一写路径、worklog 账本、SQLite 认领锁与 Job 存储（计划 §13）。

设计裁定（docs/p0_audit.md §3）：
- 候选真值仍是 queue/candidates.json（discover.py 兼容）；SQLite 只管 Job/租约/认领锁，
  认领**事实**以 worklog 为准，SQLite 锁只做并发闸。
- atomic_json_write 在 discover.py 的临时文件+replace 基础上补两道（10-22 0x00 损坏教训）：
  fsync 后回读 json 解析校验；保留上一版滚动 .bak。
- 每日认领上限：事务内先查 SQLite 当日记录，再查 worklog 当日 claim/awaiting_assignment，
  两者任一命中即拒绝（跨窗口/跨进程统一计数）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

JOB_STATES = ("queued", "leased", "running", "collecting",
              "succeeded", "failed", "inconclusive", "blocked")


def business_day(ts: dt.datetime, tz_name: str) -> str:
    """把时间戳换算到业务时区的日期（YYYY-MM-DD）。

    worklog 里的 ts 是裸本地时间（naive），按既有时区语义处理，不做 UTC 换算偷改日期。
    """
    if ts.tzinfo is None:
        return ts.date().isoformat()
    return ts.astimezone(ZoneInfo(tz_name)).date().isoformat()


def now_local_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


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
        now = now or dt.datetime.now()
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
    """SQLite 每日认领锁：跨进程并发下保证"每业务日至多 1 个新认领"。

    用法：with ledger.lock_day(tz_name, candidate_id) as ok: ...
    SQLite 连接必须打开在同一文件上（BEGIN IMMEDIATE 提供写序列化）。
    """

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS claim_days ("
            " day TEXT PRIMARY KEY, candidate_id TEXT NOT NULL,"
            " action TEXT NOT NULL, ts TEXT NOT NULL)")

    def close(self) -> None:
        self._conn.close()

    def try_claim(self, tz_name: str, candidate_id: str, action: str = "claim",
                  now: dt.datetime | None = None) -> tuple[bool, str]:
        """尝试占用当日认领名额。返回 (ok, reason)。

        BEGIN IMMEDIATE 串行化后：SQLite 已有当日记录 → 拒绝；
        否则插入成功 → 占额成功。与 worklog 的双闸由 repo_policy.PolicyEngine 组合完成。
        """
        now = now or dt.datetime.now()
        day = business_day(now, tz_name)
        ts = now_local_iso()
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            row = cur.execute("SELECT candidate_id FROM claim_days WHERE day=?",
                              (day,)).fetchone()
            if row is not None:
                cur.execute("ROLLBACK")
                return False, f"day {day} already claimed by {row[0]}"
            cur.execute("INSERT INTO claim_days(day, candidate_id, action, ts) VALUES (?,?,?,?)",
                        (day, candidate_id, action, ts))
            cur.execute("COMMIT")
            return True, "claimed"
        except BaseException:
            cur.execute("ROLLBACK")
            raise

    def release_day(self, day: str | None = None) -> None:
        """仅测试/运维回滚用：删除某日占额记录。"""
        if day is None:
            self._conn.execute("DELETE FROM claim_days")
        else:
            self._conn.execute("DELETE FROM claim_days WHERE day=?", (day,))


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
