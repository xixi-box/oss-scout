"""OpenCode CLI 客户端：实现 ModelClient 协议，接通 model_fallback 的回退链。

约定（计划 §11）：
- 模型 ID 动态解析（`opencode models`），不写死在业务代码里；
- 对外只暴露 run(model, prompt)；错误分类交给 model_fallback（401/403 → AuthError 停链）；
- opencode run 的 JSON 事件流按行解析，取 text 事件聚合为输出。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

# 需要原样透传给 model_fallback 的 HTTP 状态码（会出现在 error 事件里）
_STATUS_MARKERS = ("statusCode", "status_code")


class OpenCodeClient:
    def __init__(self, binary: str = "opencode", timeout_seconds: int = 600,
                 workdir: Path | None = None):
        self.binary = binary
        self.timeout = timeout_seconds
        self.workdir = workdir

    def run(self, model: str, prompt: str) -> tuple[bool, str]:
        """执行一次 opencode run。返回 (ok, detail)。

        ok=True 时 detail 为模型输出文本；ok=False 时 detail 含可分类的错误
        串（包含 "401"/"403"/"429"/状态码与消息），由调用方决定停链还是换挡。
        """
        cmd = [self.binary, "run", "-m", model, "--format", "json", prompt]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=self.timeout,
                               cwd=str(self.workdir) if self.workdir else None)
        except FileNotFoundError:
            return False, "opencode binary not found"
        except subprocess.TimeoutExpired:
            return False, f"429 opencode run timed out after {self.timeout}s"
        return self._parse_events(p.stdout, p.stderr, p.returncode)

    # ---- 内部 ----

    @staticmethod
    def _parse_events(stdout: str, stderr: str, rc: int) -> tuple[bool, str]:
        err_detail: str | None = None
        texts: list[str] = []
        for line in (stdout or "").splitlines():
            line = line.strip()
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "error":
                err_detail = OpenCodeClient._format_error(ev)
            elif ev.get("type") == "text":
                texts.append(str(ev.get("text") or ev.get("part") or ""))
        if err_detail is not None:
            return False, err_detail
        if rc != 0:
            tail = (stderr or "").strip()[-300:]
            return False, f"exit {rc}: {tail}" if tail else f"exit {rc}"
        out = "\n".join(t for t in texts if t).strip()
        return (True, out) if out else (False, "empty response (no text events)")

    @staticmethod
    def _format_error(ev: dict) -> str:
        data = ev.get("error") or {}
        inner = data.get("data") or {}
        status = None
        for k in _STATUS_MARKERS:
            if isinstance(inner, dict) and inner.get(k) is not None:
                status = inner[k]
                break
        code = inner.get("code") if isinstance(inner, dict) else None
        msg = (inner.get("message") if isinstance(inner, dict) else None) \
            or data.get("message") or "unknown error"
        parts = [str(x) for x in (status, code) if x is not None]
        return " ".join(parts + [str(msg)])[:400]


def discover_free_models(binary: str = "opencode") -> list[str]:
    """动态解析当前目录里的免费模型 ID（计划 §11：不把免费模型名写死在执行器中）。"""
    try:
        p = subprocess.run([binary, "models"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    return [ln.strip() for ln in p.stdout.splitlines()
            if ln.strip().endswith("-free")]
