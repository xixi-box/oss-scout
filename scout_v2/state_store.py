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
        if pk_cols == ["candidate_id"]:
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
            if not cols2 or pk2 == ["candidate_id"]:
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
        now = now or dt.datetime.now()
        day = business_day(now, tz_name)
        ts = now_local_iso()
        cur = self._conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            existing = [r[0] for r in cur.execute(
                "SELECT candidate_id FROM claim_days WHERE day=?", (day,)).fetchall()]
            if candidate_id in existing:
                cur.execute("ROLLBACK")
                return False, f"already claimed on {day}: {candidate_id}"
            if max_per_day > 0 and len(existing) >= max_per_day:
                cur.execute("ROLLBACK")
                return False, (f"daily limit {max_per_day} reached on {day} "
                               f"({len(existing)} claimed)")
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
        """当日业务时区已用掉几个认领名额。

        额度判断必须读这张表而不是 worklog：表里有唯一约束，worklog 是
        追加日志，重复记录会让计数偏高、把额度误判成已用尽。
        """
        now = now or dt.datetime.now()
        day = business_day(now, tz_name)
        row = self._conn.execute(
            "SELECT COUNT(*) FROM claim_days WHERE day=?", (day,)).fetchone()
        return int(row[0] if row else 0)

    def release_claim(self, candidate_id: str,
                      tz_name: str = "Asia/Shanghai",
                      now: dt.datetime | None = None) -> bool:
        """释放**单条**候选的认领记录。返回是否真的删掉了一行。

        这是 abort 时该调的方法。2026-10-31 实录：之前 abort 走的是
        release_day() 无参，那是 `DELETE FROM claim_days`——把当天所有候选的
        占额记录一起抹掉。后果是任何一条 pipeline 中途失败（gh api 超时、路由
        失败、模型解析失败都算）都会把当日额度清零，下一轮又能开两条。这是
        一天开出 12 个 PR 的第二个原因（第一个是 ingest 绕过额度检查）。
        """
        now = now or dt.datetime.now()
        day = business_day(now, tz_name)
        cur = self._conn.execute(
            "DELETE FROM claim_days WHERE day=? AND candidate_id=?", (day, candidate_id))
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
