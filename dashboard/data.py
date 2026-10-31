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

import tenacity
from urllib.parse import quote

import yaml

# token 打码：ghp_ / gh_ / oc_sk_ / sk- 前缀 → 前 6 字符 + ***
SECRET_RE = re.compile(r"\b(?:ghp_|gh_o|gh_|oc_sk_|sk-)[A-Za-z0-9_\-]{3,}")
TERMINAL_JOB_STATES = {"succeeded", "failed", "inconclusive", "blocked"}
GH_CACHE_TTL = 300.0   # gh 结果缓存 5 分钟：PR/run 数据低频变化，且降低抖动面


def mask(text) -> str:
    """扫描文本中的 ghp_/gh_/oc_sk_/sk- 前缀 token，打码为前 6 字符+***。"""
    if text is None:
        return ""
    s = str(text)
    return SECRET_RE.sub(lambda m: m.group(0)[:6] + "***", s)


class GhError(Exception):
    """gh 命令失败（TLS 抖动/未登录/仓库不存在等）。页面显示"未知"。"""


@tenacity.retry(
    stop=tenacity.stop_after_attempt(2),
    wait=tenacity.wait_fixed(4),
    retry=tenacity.retry_if_exception_type((subprocess.TimeoutExpired, GhError)),
    reraise=True,
)
def _gh_once(args: list[str]) -> str:
    try:
        p = subprocess.run(["gh", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
    except subprocess.TimeoutExpired:
        raise
    if p.returncode != 0:
        raise GhError((p.stderr or p.stdout or "").strip()[:300])
    return p.stdout


def default_gh_runner(args: list[str]) -> str:
    """gh 调用，tenacity 带退避重试（本机 TLS 抖动常态）。"""
    return _gh_once(args)


def default_modal_runner(root: Path) -> str:
    """运行 scout_v2.budget.ModalCliBilling 读当月计量成本。

    输出一行 `METERED <float|None>`；CLI 缺失/超时/解析失败 → 异常（页面显示"未知"）。
    """
    code = (
        "from scout_v2.budget import ModalCliBilling\n"
        "import json\n"
        "print('METERED', json.dumps(ModalCliBilling().metered_usage_usd()))\n"
    )
    # cwd 必须能 import scout_v2：优先包所在目录（容器 /app），回退数据根
    pkg_root = Path(__file__).resolve().parents[1]
    run_cwd = pkg_root if (pkg_root / "scout_v2").is_dir() else Path(root)
    env = dict(__import__("os").environ)
    env["PYTHONPATH"] = str(pkg_root) + (
        (__import__("os").pathsep + env["PYTHONPATH"]) if env.get("PYTHONPATH") else "")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60, cwd=str(run_cwd),
                       env=env)
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
        self._refreshing: set[str] = set()

    # ---------- 缓存 ----------

    FAILURE_TTL = 30.0   # 失败短缓存：gh 抖动时页面秒开，30 秒内自动重试恢复

    def _cached(self, key: str, ttl: float, fn):
        """stale-while-revalidate：未过期回缓存；过期先回旧值 + 后台线程刷新；
        无旧值才同步计算。失败（None）短缓存 30s——gh 抖动不再阻塞每个请求。"""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit:
            age = now - hit[0]
            if hit[1] is None:
                if age < self.FAILURE_TTL:
                    return None
            elif age < ttl:
                return hit[1]
            # 过期：立即回旧值（含旧失败），后台刷新
            if key not in self._refreshing:
                self._refreshing.add(key)
                import threading
                def _bg():
                    try:
                        v = fn()
                        self._cache[key] = (time.monotonic(), v)
                    finally:
                        self._refreshing.discard(key)
                threading.Thread(target=_bg, daemon=True).start()
            return hit[1]
        value = fn()
        self._cache[key] = (now, value)   # None 也记（30s 失败缓存）
        return value
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
        """返回 (health dict|None, 是否过期>7天)。

        文件不存在 → (None, False)：页面显示"尚未计算"，不误报为"已过期"。
        （原先返回 True 会让全新部署一上来就显示"过期>7天"的误导提示）
        """
        path = self.root / "state" / "health.json"
        if not path.exists():
            return None, False
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
                return json.loads(self.gh_runner(args))
            except Exception:  # noqa: BLE001 — TLS 抖动/超时/坏 JSON 都显示"未知"
                return None
        key = "gh:" + " ".join(args)
        ttl = dashboard_settings(self)["gh_cache_ttl"]
        # 失败（None）只缓存 30s：TLS 抖动恢复后能立刻出数据，不被长 TTL 卡住
        hit = self._cache.get(key)
        if hit and hit[1] is None:
            ttl = min(ttl, 30)
        return self._cached(key, ttl, _call)

    def own_repos(self) -> list[str]:
        """自有仓库列表：配置声明 + 实际调用 gh 查询的仓库。

        来源（并集）：
        1. configs/dashboard.yaml: pr_repos（显式声明，适合新部署的空数据目录）
        2. repositories.yaml 里 internal_test_repo / 属于当前 gh 用户的仓库
        3. worklog / artifacts 历史痕迹（兼容老部署）
        """
        repos: set[str] = set()
        # 1) 显式配置
        try:
            cfg = dashboard_settings(self)
            for r in (cfg.get("pr_repos") or []):
                if isinstance(r, str) and "/" in r:
                    repos.add(r)
        except Exception:  # noqa: BLE001
            pass
        # 2) 当前 gh 用户的仓库（有 admin 权限者，避免查询他人仓库）
        try:
            out = self.gh_runner(["api", "user", "--jq", ".login"])
            me = (out or "").strip()
            if me:
                cfg_repos = (self.repositories_yaml().get("repositories") or {})
                for repo_name in cfg_repos:
                    if repo_name.startswith(me + "/"):
                        repos.add(repo_name)
        except Exception:  # noqa: BLE001
            pass
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


    def my_external_prs(self, limit: int = 50):
        """我（gh 当前用户）在全 GitHub 的作者 PR——含贡献给第三方仓库的。

        用 search API：author:<me> is:pr。返回 None 表示查询失败（页面显示未知）。
        """
        try:
            me = (self.gh_runner(["api", "user", "--jq", ".login"]) or "").strip()
        except Exception:  # noqa: BLE001
            return None
        if not me:
            return None
        self._me_login = me
        # 用 gh search prs（高层命令）而不是 gh api search/issues：
        # 后者在部分 gh 版本会把 search 当子命令解析，返回帮助文本而非 JSON
        items = self._gh_json([
            "search", "prs", "--author", me,
            "--limit", str(limit), "--sort", "updated", "--order", "desc",
            "--json", ("number,title,state,createdAt,updatedAt,url,repository,"
                       "commentsCount,isDraft"),
        ])
        data = items if isinstance(items, list) else None
        if data is None:
            return None
        out = []
        for it in data:
            repo_obj = it.get("repository") or {}
            repo = (repo_obj.get("nameWithOwner") if isinstance(repo_obj, dict)
                    else str(repo_obj)) or ""
            out.append({
                "repo": repo,
                "number": it.get("number"),
                "title": it.get("title", ""),
                "state": str(it.get("state", "")).upper(),
                "url": it.get("url", ""),
                "createdAt": it.get("createdAt", ""),
                "updatedAt": it.get("updatedAt", ""),
                "comments": it.get("commentsCount", 0),
                "isDraft": bool(it.get("isDraft")),
                "external": not repo.startswith((me + "/")),
            })
        return out

    def me_login(self) -> str:
        try:
            return (self.gh_runner(["api", "user", "--jq", ".login"]) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def pr_list(self, repo: str):
        """PR 列表（含完整状态/评论/review/diff 统计/正文）。

        字段取舍：gh pr list 的 comments 只有作者+时间，正文需逐条评论；
        这里一次请求拿到 comments.author/login 与 reviews，正文由 pr_detail 按需取
        （避免一次拉 30 个 PR 的全部评论正文打爆 API）。
        """
        return self._gh_json([
            "pr", "list", "-R", repo, "--state", "all",
            "--json", ("number,title,state,isDraft,createdAt,updatedAt,closedAt,mergedAt,"
                       "url,author,statusCheckRollup,comments,reviews,"
                       "additions,deletions,changedFiles,baseRefName,headRefName,"
                       "mergeable,mergeStateStatus,reviewDecision,body,labels"),
            "--limit", "50",
        ])

    def pr_detail(self, repo: str, number: int):
        """单个 PR 详情：完整描述 + 全部评论正文 + review 意见 + 文件清单。"""
        detail = self._gh_json([
            "pr", "view", str(number), "-R", repo,
            "--json", ("number,title,body,state,isDraft,author,createdAt,mergedAt,closedAt,"
                       "url,additions,deletions,changedFiles,mergeable,mergeStateStatus,"
                       "reviewDecision,statusCheckRollup,labels,baseRefName,headRefName"),
        ])
        if detail is None:
            return None
        comments = self._gh_json([
            "api", f"repos/{repo}/issues/{number}/comments?per_page=50",
        ]) or []
        reviews = self._gh_json([
            "pr", "view", str(number), "-R", repo, "--json", "reviews",
        ]) or {}
        files = self._gh_json([
            "pr", "view", str(number), "-R", repo, "--json", "files",
        ]) or {}
        detail["_comments"] = [
            {"author": (c.get("user") or {}).get("login", ""),
             "association": c.get("author_association", ""),
             "created": c.get("created_at", ""),
             "body": c.get("body") or ""}
            for c in comments
        ]
        detail["_reviews"] = [
            {"author": (r.get("author") or {}).get("login", ""),
             "state": r.get("state", ""), "body": r.get("body") or "",
             "submitted": r.get("submittedAt", "")}
            for r in (reviews.get("reviews") or [])
        ]
        detail["_files"] = [
            {"path": f.get("path", ""), "additions": f.get("additions", 0),
             "deletions": f.get("deletions", 0)}
            for f in (files.get("files") or [])
        ]
        return detail

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
        hard = budget.get("platform_hard_limits") or {}
        return {
            "metered": metered,
            # 本地 25 软阈值已按用户要求删除；上限直接用控制台 usage limit
            "softcap": hard.get("usage_limit_usd"),
            "hard_limits": hard,
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
            "health_missing": not (self.root / "state" / "health.json").exists(),
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


# ---------- Cloudflare Worker 回传通道（2026-10-25） ----------

def opencode_state(ds):
    p = ds.root / "state" / "dashboard_opencode_state.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"ok": None, "model": "", "checked_at": ""}


def cloudflare_cfg(ds) -> dict:
    p = ds.root / "configs" / "cloudflare.yaml"
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def ingest_cloudflare_issues(ds, items: list[dict]) -> tuple[int, list[str]]:
    """Cloudflare Worker 回传的原始 issue → 客观否决 + AI 二元判定 → 入队。

    返回 (入队条数, 新入队的 id 列表)。后者让调用方可以**立即派发**修复，
    而不用等下一轮轮询——Worker 每分钟送来新 issue，让 AI 刚判定可用的候选
    再排 60 秒队是白等。

    规则：只入队已知监控仓库；有 blockers 或 AI 判 NO 的记录进 seen 不入队；
    去重与 discover.py 共用 state/seen.json。写路径走 atomic_json_write。
    """
    from scout_v2.state_store import atomic_json_write
    added: list[str] = []      # 本次新入队的候选 id，供调用方立即派发
    # 不 import discover.py：它在模块顶层硬读 config.json，容器里没有该文件会崩。
    # 只复用它的评分实现（纯函数 + 正则），通过 _discover_score() 取得。
    discover = _discover_score()

    q = ds._cached("candidates", 5.0, _read_json(ds.root / "queue" / "candidates.json", {}))
    seen_path = ds.root / "state" / "seen.json"
    seen = _read_json(seen_path, {})()
    # lane 判定走策略层（repositories.yaml + lane_overrides 合并，PolicyEngine 同源），
    # 而非 config.json 发现名单——否则名单外仓库（如内部测试仓）会被静默丢弃
    from scout_v2.repo_policy import PolicyEngine
    eng, _eng_err = PolicyEngine.load(ds.root)
    lane_of: dict[str, str] = {}
    if eng is not None:
        for repo_name in ((eng.repos_cfg.get("repositories") or {})):
            lane_of[repo_name] = eng.repo_lane(repo_name)

    now_iso = dt.datetime.now().isoformat(timespec="seconds")
    # AI 评分（用户裁定 2026-10-25）：对每条候选调 Go API，与规则评分并列存储；
    # 最终 A/B/C 判定仍由规则引擎决定（Playbook：AI 推测不作事实）
    from scout_v2.workers.issue_scorer import load_scorer_config
    scorer_cfg = load_scorer_config(ds.root)
    scorer = None
    if scorer_cfg.get("api_key"):
        from scout_v2.workers.issue_scorer import GoClient
        scorer = GoClient(key=scorer_cfg["api_key"], model=scorer_cfg.get("model", "deepseek-v4.1-flash"))
    queued = 0
    for it in items:
        repo = str(it.get("repo", ""))
        number = int(it.get("number") or 0)
        key = f"{repo}#{number}"
        if not repo or not number or key in seen or key in q:
            continue
        lane = lane_of.get(repo)
        if lane is None:
            seen[key] = {"first_seen": now_iso, "status": "unknown_repo"}
            continue
        title = str(it.get("title", ""))
        body = str(it.get("body_excerpt", ""))
        labels = [str(l) for l in (it.get("labels") or [])]

        # 客观否决（不需要 AI 判断的事实）：标题像提问、纯文档、计费抱怨。
        # 这类连调 AI 都省了。其余交给 AI 判断"能不能自动处理"。
        blockers: list[str] = []
        if discover.RE_BILLING.search(title):
            blockers.append("billing_complaint")
        if any(discover.RE_FEATURE_LABEL.search(lb) for lb in labels):
            blockers.append("feature_label")
        if discover.RE_BAD_TITLE.search(title):
            blockers.append("question_or_feature_title")
        if discover.RE_DOCS.search(title):
            blockers.append("docs_only")
        if len(body) < 200:
            blockers.append("thin_body")

        # AI 细筛：唯一的合格判定。usable=None 表示调用失败（与"判定不可用"区分）。
        verdict = {"usable": None, "reason": "no scorer configured", "model": None}
        if scorer is not None:
            try:
                verdict = scorer.score_issue({
                    "repo": repo, "number": number, "title": title,
                    "labels": labels, "reactions": int(it.get("reactions") or 0),
                    "comments": int(it.get("comments") or 0), "body_excerpt": body})
            except Exception as ex:  # noqa: BLE001 — 评分失败不阻塞入队，留给人工看
                verdict = {"usable": None, "reason": f"scorer error: {ex}"[:200],
                           "model": None}

        if blockers or verdict.get("usable") is not True:
            seen[key] = {"first_seen": now_iso,
                         "status": "blocked" if blockers else "rejected"}
            # 记录 AI 的理由，便于事后核对"它为什么被拒"
            if verdict.get("reason"):
                seen[key]["why"] = verdict["reason"][:200]
            continue

        entry = {"id": key, "repo": repo, "number": number, "title": title,
                 "url": it.get("url") or f"https://github.com/{repo}/issues/{number}",
                 "lane": lane, "blockers": blockers,
                 # 顶层也放一份 labels：PR 门禁要按标签判断这个仓库允不允许未受邀 PR。
                 # 只在 signals 里放会让 entry["labels"] 永远为空（2026-10-30 实测
                 # 0/210 个候选有顶层 labels），门禁于是对 requires_invite:false 的
                 # 仓库无条件放行、对 true 的永远拒绝——两头都错。
                 "labels": labels,
                 "ai_verdict": verdict,
                 "created_at": it.get("created_at", ""),
                 "discovered_at": now_iso, "status": "new", "lease": None,
                 "source": "cloudflare-worker",
                 # 8000 而非 2000：AI 要判断"能否自动修"，需要看完整的复现步骤和预期行为
                 "body_excerpt": body[:8000]}
        q[key] = entry
        seen[key] = {"first_seen": now_iso, "status": "queued"}
        queued += 1
        added.append(key)
    if queued:
        atomic_json_write(ds.root / "queue" / "candidates.json", q)
        atomic_json_write(seen_path, seen)
    return queued, added


def _read_json(path: Path, default):
    def _fn():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return default
    return _fn



def _discover_score():
    """返回带 score()/RE_REPRO 的对象：优先用项目根 discover.py（本地开发），
    容器内无 config.json 时用内置等价实现（与 discover.py 评分规则一致）。"""
    import sys as _sys
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1]
    if (root / "config.json").exists():
        if str(root) not in _sys.path:
            _sys.path.insert(0, str(root))
        try:
            import importlib
            return importlib.import_module("discover")
        except Exception:  # noqa: BLE001
            pass
    return _BuiltinScorer()


def cloudflare_pull(ds) -> dict:
    """从 CF KV 拉取 pending 批次 → ingest → 删除已处理键。返回摘要。

    用 configs/cloudflare.yaml 的 api_token/account_id/kv_namespace_id；
    任一缺失 → {"error": "not configured"}。网络失败 → {"error": ...}。
    """
    cfg = cloudflare_cfg(ds)
    token, account = cfg.get("api_token") or "", cfg.get("account_id") or ""
    ns = cfg.get("kv_namespace_id") or ""
    if not (token and account and ns):
        return {"error": "not configured"}
    base = f"https://api.cloudflare.com/client/v4/accounts/{account}/storage/kv/namespaces/{ns}"
    headers = {"Authorization": f"Bearer {token}"}
    import urllib.request

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_fixed(4),
        reraise=True,
    )
    def _req(path: str, **kw):
        if not path.startswith("https://"):
            raise ValueError(f"blocked non-https url: {path[:60]}")
        req = urllib.request.Request(path, headers=headers, **kw)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())

    try:
        keys = [k["name"] for k in
                (_req(f"{base}/keys?prefix=pending:&limit=100").get("result") or [])]
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:200]}
    pulled, ingested = 0, 0
    for key in keys:
        try:
            payload = _req(f"{base}/values/{key}")
        except Exception as e:  # noqa: BLE001
            return {"pulled": pulled, "ingested": ingested, "error": str(e)[:200]}
        pulled += 1
        ingested += ingest_cloudflare_issues(ds, payload.get("issues") or [])
        try:
            _req(f"{base}/values/{key}", method="DELETE")
        except Exception:  # noqa: BLE001
            pass   # 删除失败：下轮 seen 去重兜底
    return {"pulled": pulled, "ingested": ingested}


def cloudflare_kv_put(ds, key: str, value: str) -> dict:
    """往 CF KV 写一个键。返回 {"ok": bool, "error": str}。

    KV 免费档 1000 write/天，所以调用方必须自己限流——PR 列表同步是 15 分钟
    一次（96/天），在预算内。
    """
    cfg = cloudflare_cfg(ds)
    token, account = cfg.get("api_token") or "", cfg.get("account_id") or ""
    ns = cfg.get("kv_namespace_id") or ""
    if not (token and account and ns):
        return {"ok": False, "error": "not configured"}
    base = (f"https://api.cloudflare.com/client/v4/accounts/{account}"
            f"/storage/kv/namespaces/{ns}/values/{key}")
    import urllib.request
    try:
        req = urllib.request.Request(
            base, data=value.encode("utf-8"), method="PUT",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "text/plain"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
        return {"ok": bool(body.get("success")), "error": ""}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}


def cloudflare_last_pull(ds) -> str:
    p = ds.root / "state" / "cloudflare_last_pull.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("at", "")
    except Exception:  # noqa: BLE001
        return ""


# ---------- Dashboard 运行参数（前端可改，落 configs/dashboard.yaml） ----------

DASHBOARD_DEFAULTS = {
    "cf_pull_seconds": 20,        # Cloudflare KV 拉取间隔（20s = 4320 次读/天，占 10 万额度 4%）
    "pr_refresh_seconds": 120,    # PR 列表自动刷新间隔
    "gh_cache_ttl": 300,          # gh 外部命令结果缓存秒数
    "auto_repair_seconds": 300,   # 自动修复轮询间隔（0=关闭）；每日认领上限仍由 ClaimLedger 强制
}


def dashboard_settings(ds) -> dict:
    p = ds.root / "configs" / "dashboard.yaml"
    cfg = dict(DASHBOARD_DEFAULTS)
    try:
        cfg.update(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def cf_pull_seconds(ds) -> int:
    try:
        return max(10, int(dashboard_settings(ds)["cf_pull_seconds"]))
    except Exception:  # noqa: BLE001
        return DASHBOARD_DEFAULTS["cf_pull_seconds"]


class _BuiltinScorer:
    """discover.py 正则的容器内副本。

    2026-10-30：只保留正则（RE_BILLING / RE_FEATURE / RE_BAD_TITLE / RE_DOCS /
    RE_REPRO），用于入队前的客观事实否决。原先的 score() 打分方法已删除——
    合格判定完全交给 AI 的二元结论，规则不再参与打分。
    """

    import re as _re

    RE_REPRO = _re.compile(
        r"\b(?:steps?\s+to\s+(?:reproduce|repro)|traceback|minimal\s+(?:repro|example)|"
        r"reproduction\s+steps|expected\s+.{0,40}actual)\b", _re.I)
    _RE_BILLING = _re.compile(r"\b(quota|billing|subscription|refund|scam|re-?charge[d]?|plan limit)\b", _re.I)
    _RE_FEATURE = _re.compile(r"enhancement|feature|rfc|proposal|design|discussion|idea", _re.I)
    _RE_BAD_TITLE = _re.compile(
        r"^(how\s+(?:do|to|can)\b|is\s+it\s+possible|any\s+(?:plan|way|update)|"
        r"\[?question\]?|\[?feature\s+(?:request|idea)\]?|support\s+for\b|roadmap|"
        r"support\b|add\s+(?:support|ability|option))", _re.I)
    _RE_DOCS = _re.compile(r"\b(typo|doc(?:umentation|s)?|readme|changelog|comment\s+fix)\b", _re.I)
