"""OpenCode Go API 直连客户端 + issue 评分（用户裁定 2026-10-25）。

- 不经过 OpenCode CLI：直接 POST OpenAI 兼容端点（带 x-opencode-session 头）。
- 模型列表动态拉取（Go 套餐全部可用模型），模型名存 configs/model_policy.yaml。
- 评分输出结构化 {grade, score, reason}；只做参考意见，最终判定仍由规则引擎决定
  （Playbook：不把 AI 推测当已证实事实）。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid
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
        self.model = model
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "User-Agent": "opencode/1.18.27",
                "x-opencode-session": str(uuid.uuid4())}

    def chat(self, prompt: str, max_tokens: int = 200) -> tuple[bool, str]:
        """单轮补全。返回 (ok, text|error_detail)。TLS 抖动重试 2 次；4xx 不重试。"""
        body = json.dumps({"model": self.model,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        last = ""
        for attempt in range(3):
            try:
                req = urllib.request.Request(f"{BASE}/chat/completions", data=body,
                                             headers=self._headers())
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read().decode())
                text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
                return True, text.strip()
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:200]
                if e.code < 500:
                    return False, f"HTTP {e.code}: {detail}"   # 认证/参数错误不重试
                last = f"HTTP {e.code}"
            except Exception as e:  # noqa: BLE001 — TLS 抖动
                last = str(e)[:150]
            time.sleep(min(8, (attempt + 1) * 4))
        return False, last

    def list_models(self) -> list[str]:
        """Go 套餐可用模型 ID 列表。失败返回空列表。"""
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


def load_scorer_config(root: Path) -> dict:
    """configs/scorer.yaml：{model, api_key?}。key 缺省取 opencode auth.json 登录态。"""
    p = root / "configs" / "scorer.yaml"
    cfg: dict = {}
    if p.exists():
        try:
            cfg = yaml_safe_load(p) or {}
        except Exception:  # noqa: BLE001
            cfg = {}
    if not cfg.get("api_key"):
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
