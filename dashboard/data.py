"""oss-scout v2 Dashboard 数据层：只读聚合所有状态文件与外部命令。

原则：
- 全部只读（sqlite 以 mode=ro 打开；绝无任何写路由）。
- 容错：任何单个数据源损坏/缺失 → 返回 None / 空值，页面显示"未知"，不崩溃。
- 外部命令（gh / modal）可注入 runner，供测试模拟失败与成功。
- 60 秒内存缓存外部命令结果，避免高频轮询打爆 API。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import yaml

# token 打码：ghp_ / gh_ / oc_sk_ / sk- 前缀 → 前 6 字符 + ***
SECRET_RE = re.compile(r"\b(?:ghp_|gh_o|gh_|oc_sk_|sk-)[A-Za-z0-9_\-]{3,}")
TERMINAL_JOB_STATES = {"succeeded", "failed", "inconclusive", "blocked"}
GH_CACHE_TTL = 60.0


def mask(text) -> str:
    """扫描文本中的 ghp_/gh_/oc_sk_/sk- 前缀 token，打码为前 6 字符+***。"""
    if text is None:
        return ""
    s = str(text)
    return SECRET_RE.sub(lambda m: m.group(0)[:6] + "***", s)


class GhError(Exception):
    """gh 命令失败（TLS 抖动/未登录/仓库不存在等）。页面显示"未知"。"""


def default_gh_runner(args: list[str]) -> str:
    p = subprocess.run(["gh", *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=45)
    if p.returncode != 0:
        raise GhError((p.stderr or p.stdout or "").strip()[:300])
    return p.stdout


def default_modal_runner(root: Path) -> str:
    """运行 scout_v2.budget.ModalCliBilling 读当月计量成本。

    输出一行 `METERED <float|None>`；CLI 缺失/超时/解析失败 → 异常（页面显示"未知"）。
    """
    code = (
        "from scout_v2.budget import ModalCliBilling\n"
        "import json\n"
        "print('METERED', json.dumps(ModalCliBilling().metered_usage_usd()))\n"
    )
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60, cwd=str(root),
                       env=None)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "").strip()[:300])
    return p.stdout


class DataSource:
    """全部页面数据读取入口。root 指向 oss-scout 项目根目录。"""

    def __init__(self, root: Path, gh_runner=None, modal_runner=None):
        self.root = Path(root)
        self.gh_runner = gh_runner or default_gh_runner
        self.modal_runner = modal_runner or (lambda r: default_modal_runner(r))
        self._cache: dict[str, tuple[float, object]] = {}

    # ---------- 缓存 ----------

    def _cached(self, key: str, ttl: float, fn):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (now, value)
        return value

    # ---------- 候选池 ----------

    def candidates(self) -> dict:
        path = self.root / "queue" / "candidates.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001 — 文件缺失/0x00 损坏都容错
            return {}

    def candidate(self, key: str):
        return self.candidates().get(key)

    # ---------- worklog ----------

    def worklog(self) -> list[dict]:
        """JSONL 审计账本，容错解析（坏行/非 JSON 行跳过）。"""
        path = self.root / "state" / "worklog.jsonl"
        out: list[dict] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return out
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    out.append(rec)
            except json.JSONDecodeError:
                continue
        return out

    # ---------- health ----------

    def health(self) -> tuple[dict | None, bool]:
        """返回 (health dict|None, 是否过期>7天)。"""
        path = self.root / "state" / "health.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None, True
        computed = str(data.get("computed_at", ""))
        try:
            age = dt.datetime.now() - dt.datetime.fromisoformat(computed)
            expired = age > dt.timedelta(days=7)
        except ValueError:
            expired = True
        return data, expired

    def health_date(self) -> str:
        data, _ = self.health()
        return str((data or {}).get("computed_at", "未知"))

    # ---------- sqlite jobs（只读）----------

    def _db_path(self) -> Path:
        return self.root / "state" / "scout_v2.sqlite3"

    def _open_ro(self):
        path = self._db_path()
        if not path.exists():
            return None
        try:
            return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10)
        except sqlite3.Error:
            return None

    def jobs(self) -> list[dict]:
        """jobs 表全量（payload/result 解析为 dict）。库缺失/损坏 → []。"""

        def _read():
            conn = self._open_ro()
            if conn is None:
                return []
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT job_id, idempotency_key, state, lease_until, payload,"
                    " result, updated_at FROM jobs ORDER BY updated_at DESC"
                ).fetchall()
            except sqlite3.Error:
                return []
            finally:
                conn.close()
            out = []
            for r in rows:
                payload, result = {}, None
                try:
                    payload = json.loads(r["payload"])
                except Exception:  # noqa: BLE001
                    payload = {"_raw": mask(r["payload"])}
                if r["result"]:
                    try:
                        result = json.loads(r["result"])
                    except Exception:  # noqa: BLE001
                        result = {"_raw": mask(r["result"])}
                out.append({"job_id": r["job_id"], "idempotency_key": r["idempotency_key"],
                            "state": r["state"], "lease_until": r["lease_until"],
                            "payload": payload or {}, "result": result,
                            "updated_at": r["updated_at"]})
            return out

        return self._cached("jobs", 10.0, _read)

    def job(self, job_id: str):
        for j in self.jobs():
            if j["job_id"] == job_id:
                return j
        return None

    def measured_cost_total(self) -> float | None:
        """jobs result 里 measured_cost_usd 求和；无任何记录 → None。"""
        total = 0.0
        found = False
        for j in self.jobs():
            res = j.get("result")
            if isinstance(res, dict) and isinstance(res.get("measured_cost_usd"), (int, float)):
                total += float(res["measured_cost_usd"])
                found = True
        return total if found else None

    # ---------- runs / logs ----------

    def runs_dir(self) -> Path:
        return self.root / "state" / "runs"

    def runs_files(self) -> list[dict]:
        d = self.runs_dir()
        if not d.is_dir():
            return []
        out = []
        for f in sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.is_file() and f.suffix in (".md", ".log", ".txt"):
                out.append({"name": f.name, "mtime": f.stat().st_mtime,
                            "size": f.stat().st_size})
        return out

    def last_scan_time(self) -> str | None:
        files = self.runs_files()
        if not files:
            return None
        mtime = max(f["mtime"] for f in files)
        return dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

    def runs_read(self, name: str) -> str:
        """读取 state/runs/ 下文件。强制白名单：仅文件名，禁止路径分隔符与 ..。"""
        if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
            raise ValueError("illegal log name")
        base = self.runs_dir().resolve()
        target = (base / name).resolve()
        if target.parent != base or not target.is_file():
            raise ValueError("illegal log name")
        return target.read_text(encoding="utf-8", errors="replace")

    # ---------- artifacts ----------

    def artifacts(self) -> list[dict]:
        d = self.root / "artifacts"
        if not d.is_dir():
            return []
        out = []
        for sub in sorted(d.iterdir(), reverse=True):
            if sub.is_dir():
                files = sorted(f.name for f in sub.iterdir() if f.is_file())
                out.append({"name": sub.name, "files": files})
        return out

    def artifact_file(self, artifact: str, filename: str) -> tuple[str, str] | None:
        """返回 (文件名, 内容)；校验都在 artifacts/<dir>/ 内，不跨目录。"""
        if (not artifact or not filename or "/" in artifact or "\\" in artifact
                or ".." in artifact or "/" in filename or "\\" in filename or ".." in filename):
            return None
        d = (self.root / "artifacts" / artifact).resolve()
        if d.parent != (self.root / "artifacts").resolve():
            return None
        f = (d / filename).resolve()
        if f.parent != d or not f.is_file():
            return None
        return f.name, f.read_text(encoding="utf-8", errors="replace")[:20000]

    # ---------- configs ----------

    def _read_yaml(self, rel: str) -> dict:
        try:
            return yaml.safe_load((self.root / rel).read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    def repositories_yaml(self) -> dict:
        return self._read_yaml("configs/repositories.yaml")

    def v2_yaml(self) -> dict:
        return self._read_yaml("configs/v2.yaml")

    def model_policy(self) -> dict:
        return self._read_yaml("configs/model_policy.yaml")

    def config_json(self) -> dict:
        try:
            return json.loads((self.root / "config.json").read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            return {}

    # ---------- gh（PR / Actions），60s 缓存，失败 → None ----------

    def _gh_json(self, args: list[str]):
        def _call():
            try:
                out = self.gh_runner(args)
                return json.loads(out)
            except Exception:  # noqa: BLE001 — TLS 抖动/超时/坏 JSON 都显示"未知"
                return None
        return self._cached("gh:" + " ".join(args), GH_CACHE_TTL, _call)

    def own_repos(self) -> list[str]:
        """从 worklog target/detail 与 artifacts pr_url.txt 提取自有仓库（xixi-box/*）。"""
        repos: set[str] = set()
        pat = re.compile(r"xixi-box/[A-Za-z0-9_.\-]+")
        for rec in self.worklog():
            blob = " ".join(str(rec.get(k, "")) for k in ("target", "result", "detail", "repo"))
            repos.update(m.rstrip(".") for m in pat.findall(blob))
        for art in self.artifacts():
            hit = self.artifact_file(art["name"], "pr_url.txt")
            if hit:
                m = re.search(r"github\.com/(xixi-box/[A-Za-z0-9_.\-]+)", hit[1])
                if m:
                    repos.add(m.group(1))
        return sorted(repos)

    def pr_list(self, repo: str):
        return self._gh_json([
            "pr", "list", "-R", repo,
            "--json", "number,title,state,createdAt,url,statusCheckRollup,comments",
            "--limit", "30",
        ])

    def actions_runs(self, repo: str):
        return self._gh_json([
            "run", "list", "-R", repo,
            "--json", "conclusion,createdAt,workflowName", "--limit", "10",
        ])

    def pr_counts(self) -> dict | None:
        """跨自有仓库汇总 open/merged/closed；任一仓库查询失败 → None（未知）。"""
        open_c = merged_c = closed_c = 0
        repos = self.own_repos()
        if not repos:
            return {"open": 0, "merged": 0, "closed": 0, "repos": []}
        for repo in repos:
            prs = self.pr_list(repo)
            if prs is None:
                return None
            for pr in prs:
                state = str(pr.get("state", "")).upper()
                if state == "OPEN":
                    open_c += 1
                elif state == "MERGED":
                    merged_c += 1
                elif state == "CLOSED":
                    closed_c += 1
        return {"open": open_c, "merged": merged_c, "closed": closed_c, "repos": repos}

    # ---------- Modal 额度 ----------

    def modal_usage(self) -> dict:
        """metered 未知时不显示 0，显示 None → 模板渲染"未知"。"""

        def _call():
            try:
                out = self.modal_runner(self.root)
                for line in out.splitlines():
                    if line.startswith("METERED"):
                        val = line.split(None, 1)[1].strip()
                        return None if val == "null" else float(val)
                return None
            except Exception:  # noqa: BLE001
                return None

        metered = self._cached("modal", GH_CACHE_TTL, _call)
        budget = (self.v2_yaml().get("budget") or {}).get("modal") or {}
        return {
            "metered": metered,
            "softcap": budget.get("monthly_soft_cap_usd"),
            "hard_limits": budget.get("platform_hard_limits") or {},
            "enabled": bool(budget.get("enabled")),
        }

    # ---------- OpenCode ----------

    def opencode_stats(self) -> dict:
        policy = self.model_policy()
        counts = {"401": 0, "402": 0, "429": 0}
        found = False
        for rec in self.worklog():
            action = str(rec.get("action", ""))
            if "model" not in action and "auth" not in action and "opencode" not in action:
                continue
            blob = mask(str(rec.get("result", "")) + " " + str(rec.get("detail", "")))
            for code in counts:
                if code in blob:
                    counts[code] += 1
                    found = True
        return {
            "primary_models": policy.get("primary_models") or [],
            "free_preferences": policy.get("free_preferences") or [],
            "allow_paid_api": policy.get("allow_paid_api"),
            "mode": policy.get("mode"),
            "errors": counts if found else None,
        }

    # ---------- 聚合视图 ----------

    def overview(self) -> dict:
        today = dt.date.today().isoformat()
        cands = self.candidates()
        status_counts: dict[str, int] = {}
        class_counts = {"A": 0, "B": 0, "C": 0, "未分级": 0}
        lane_counts: dict[str, int] = {}
        today_new = 0
        for c in cands.values():
            status_counts[c.get("status", "?")] = status_counts.get(c.get("status", "?"), 0) + 1
            cls = c.get("class") or "未分级"
            class_counts[cls] = class_counts.get(cls, 0) + 1
            lane_counts[c.get("lane", "?")] = lane_counts.get(c.get("lane", "?"), 0) + 1
            if str(c.get("discovered_at", "")).startswith(today):
                today_new += 1
        jobs = self.jobs()
        jobs_active = sum(1 for j in jobs if j["state"] not in TERMINAL_JOB_STATES)
        jobs_terminal = sum(1 for j in jobs if j["state"] in TERMINAL_JOB_STATES)
        recent = list(reversed(self.worklog()))[-10:]
        _, health_expired = self.health()
        return {
            "last_scan": self.last_scan_time(),
            "today_new": today_new,
            "total_candidates": len(cands),
            "status_counts": status_counts,
            "class_counts": class_counts,
            "lane_counts": lane_counts,
            "jobs_active": jobs_active,
            "jobs_terminal": jobs_terminal,
            "jobs_total": len(jobs),
            "pr_counts": self.pr_counts(),
            "recent_worklog": recent,
            "modal": self.modal_usage(),
            "health_expired": health_expired,
            "health_date": self.health_date(),
        }

    def merge_history(self) -> list[dict]:
        """worklog 里 action 或 result/detail 含 pr/merge 的行（PR 管理页合并历史）。"""
        out = []
        for rec in reversed(self.worklog()):
            action = str(rec.get("action", "")).lower()
            blob = mask(str(rec.get("result", "")) + " " + str(rec.get("detail", ""))).lower()
            if "pr" in action or "merge" in action or "merge" in blob or "pull" in action:
                out.append(rec)
        return out[:30]

    # ---------- 候选筛选 ----------

    @staticmethod
    def filter_candidates(cands: dict, q: str = "", lane: str = "", cls: str = "",
                          status: str = "", repo: str = "") -> list[dict]:
        items = list(cands.values())
        q = (q or "").lower()
        lane = (lane or "").lower()
        cls = (cls or "").upper()
        status = (status or "").lower()
        repo = (repo or "").lower()
        if q:
            items = [c for c in items
                     if q in str(c.get("title", "")).lower() or q in str(c.get("repo", "")).lower()]
        if lane:
            items = [c for c in items if str(c.get("lane", "")).lower() == lane]
        if cls:
            items = [c for c in items if str(c.get("class") or "").upper() == cls]
        if status:
            items = [c for c in items if str(c.get("status", "")).lower() == status]
        if repo:
            items = [c for c in items if str(c.get("repo", "")).lower() == repo]
        items.sort(key=lambda c: (str(c.get("discovered_at", "")), str(c.get("id", ""))),
                   reverse=True)
        return items

    @staticmethod
    def quote_key(key: str) -> str:
        return quote(key, safe="")


def bug_lane_jobs(ds: DataSource) -> list[dict]:
    """bug 车道任务：sqlite jobs payload 的 mode/execution_scope 含 bug/hunt。"""
    out = []
    for j in ds.jobs():
        payload = j.get("payload") or {}
        blob = json.dumps(payload, ensure_ascii=False).lower()
        if "bug" in blob or "hunt" in blob:
            out.append(j)
    return out
