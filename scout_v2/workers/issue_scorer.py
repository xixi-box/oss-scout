"""OpenCode Go API 直连客户端 + issue 评分（用户裁定 2026-10-25）。

- 不经过 OpenCode CLI：直接 POST OpenAI 兼容端点（带 x-opencode-session 头）。
- 模型列表动态拉取（Go 套餐全部可用模型），模型名存 configs/model_policy.yaml。
- 评分输出结构化 {grade, score, reason}；只做参考意见，最终判定仍由规则引擎决定
  （Playbook：不把 AI 推测当已证实事实）。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid

import tenacity
from pathlib import Path

BASE = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"

SCORE_PROMPT = """You are triaging a GitHub issue for an open-source contribution program.
Grade it: "A" = clear, reproducible bug worth fixing, small scope, no competing work;
"B" = plausible bug worth a look; "C" = question/feature/docs/duplicate/billing — not actionable.

Issue from {repo}#{number} (labels: {labels}, reactions: {reactions}, comments: {comments}):
Title: {title}
Body (truncated):
{body}

Rules: reproducible bug > feature; small scope preferred; billing/quota complaints are C;
documentation-only is C. Answer in EXACTLY this format, nothing else:
GRADE: A|B|C
SCORE: 0-10
REASON: one line
"""


class GoClient:
    def __init__(self, key: str, model: str = DEFAULT_MODEL, timeout: int = 60):
        self.key = key
        # API 端点是 /zen/go/v1，model 字段必须是裸模型名。
        # "opencode-go/xxx" 这种 provider 前缀是 OpenCode CLI 的写法，
        # 直调会 400 "Model is unavailable"（2026-10-28 实测）。
        self.model = model.split("/", 1)[1] if "/" in model else model
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "User-Agent": "opencode/1.18.27",
                "x-opencode-session": str(uuid.uuid4())}

    class _Retryable(Exception):
        """TLS/5xx 类瞬时错误，tenacity 捕获重试。"""

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_fixed(4),
        retry=tenacity.retry_if_exception_type(_Retryable),
        reraise=True,
    )
    def _chat_once(self, body: bytes) -> dict:
        if not BASE.startswith("https://"):
            raise ValueError("blocked non-https base url")
        try:
            req = urllib.request.Request(f"{BASE}/chat/completions", data=body,
                                         headers=self._headers())
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            low = detail.lower()
            # 上游模型临时不可用（容量/限流）与 5xx 同类：可换模型重试
            if e.code >= 500 or "model is unavailable" in low or "unavailable" in low:
                raise GoClient._Retryable(f"HTTP {e.code}: {detail}")
            return {"__http_error__": f"HTTP {e.code}: {detail}"}
        except Exception as e:  # noqa: BLE001 — TLS 抖动
            raise GoClient._Retryable(str(e)[:150])

    def chat(self, prompt: str, max_tokens: int = 200) -> tuple[bool, str]:
        """单轮补全。返回 (ok, text|error_detail)。瞬时错误 tenacity 重试；4xx 不重试。"""
        body = json.dumps({"model": self.model,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        try:
            d = self._chat_once(body)
        except GoClient._Retryable as e:
            return False, str(e)[:200]
        if "__http_error__" in d:
            return False, d["__http_error__"]
        text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return True, text.strip()

    def list_models(self) -> list[str]:
        """Go 套餐可用模型 ID 列表，每次实时从 OpenCode 拉取（绝不写死）。
        失败返回空列表——界面显示"拉取失败"，不用本地兜底表。"""
        if not BASE.startswith("https://"):
            return []
        try:
            req = urllib.request.Request(f"{BASE}/models", headers=self._headers())
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode())
            return sorted(m["id"] for m in data.get("data", []) if m.get("id"))
        except Exception:  # noqa: BLE001
            return []

    def score_issue(self, issue: dict) -> dict:
        """给一条 issue 评分。返回 {grade, score, reason, model}；失败 grade="error"。"""
        prompt = SCORE_PROMPT.format(
            repo=issue.get("repo", ""), number=issue.get("number", ""),
            labels=", ".join(issue.get("labels") or []) or "none",
            reactions=issue.get("reactions", 0), comments=issue.get("comments", 0),
            title=str(issue.get("title", ""))[:200],
            body=str(issue.get("body_excerpt", ""))[:2500])
        ok, text = self.chat(prompt)
        if not ok:
            return {"grade": "error", "score": None, "reason": text[:200], "model": self.model}
        grade, score, reason = "C", None, text[:200]
        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("GRADE:"):
                g = line.split(":", 1)[1].strip().upper()[:1]
                grade = g if g in ("A", "B", "C") else "C"
            elif line.upper().startswith("SCORE:"):
                try:
                    score = int("".join(ch for ch in line.split(":", 1)[1] if ch.isdigit())[:2])
                except ValueError:
                    score = None
            elif line.upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()[:200]
        return {"grade": grade, "score": score, "reason": reason, "model": self.model}


    def chat_with_fallback(self, prompt: str, fallbacks: list[str] | None = None
                           ) -> tuple[bool, str, str]:
        """先试当前模型，失败则按 fallbacks 顺序降级。

        返回 (ok, text_or_error, model_actually_used)。上游偶发 "Model is
        unavailable"（容量/限流）时换模型比原地重试更有效。
        """
        chain = [self.model] + [m for m in (fallbacks or []) if m != self.model]
        last = ""
        for m in chain:
            self.model = m
            ok, detail = self.chat(prompt)
            if ok:
                return True, detail, m
            last = f"{m}: {detail[:120]}"
        return False, last, chain[-1] if chain else ""


def _config_candidates(root: Path, name: str) -> list[Path]:
    """配置文件候选路径：源码目录 → 数据根（容器 /app vs /data 分离）。"""
    root = Path(root)
    dirs = [root, root.parent.parent if root.parent.name else root]
    data_root = Path(os.environ.get("OSSCOUT_DASHBOARD_ROOT", "/data"))
    dirs.append(data_root)
    out, seen = [], set()
    for d in dirs:
        p = (d / "configs" / name).resolve()
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def load_scorer_config(root: Path) -> dict:
    """读取模型配置。按候选路径探测，不假设单一目录结构。"""
    cfg: dict = {}
    for p in _config_candidates(root, "scorer.yaml"):
        if p.exists():
            try:
                cfg = yaml_safe_load(p) or {}
                break
            except Exception:  # noqa: BLE001
                cfg = {}
    # scorer.yaml 缺失时，从 model_policy.yaml 取首选模型与 key
    if not cfg.get("model"):
        for p in _config_candidates(root, "model_policy.yaml"):
            if not p.exists():
                continue
            try:
                mp = yaml_safe_load(p) or {}
                models = (mp.get("primary_models") or []) + (mp.get("free_preferences") or [])
                if models:
                    cfg.setdefault("model", models[0])
                cfg.setdefault("allow_paid_api", bool(mp.get("allow_paid_api", False)))
                break
            except Exception:  # noqa: BLE001
                pass
    if not cfg.get("api_key"):
        # 1) 环境变量优先（容器/CI 里由 Secret 注入，不落盘）
        env_key = (os.environ.get("OPENCODE_API_KEY")
                   or os.environ.get("OPENCODE_GO_API_KEY") or "").strip()
        if env_key:
            cfg["api_key"] = env_key
        else:
            # 2) 本地开发：opencode CLI 的登录态
            try:
                auth = json.loads((Path.home() / ".local/share/opencode/auth.json")
                                  .read_text(encoding="utf-8"))
                cfg["api_key"] = (auth.get("opencode-go") or {}).get("key", "")
            except Exception:  # noqa: BLE001
                cfg["api_key"] = ""
    cfg.setdefault("model", DEFAULT_MODEL)
    return cfg


def yaml_safe_load(path: Path) -> dict:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
