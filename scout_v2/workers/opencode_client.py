"""OpenCode CLI 客户端：实现 ModelClient 协议，接通 model_fallback 的回退链。

约定（计划 §11）：
- 模型 ID 动态解析（`opencode models`），不写死在业务代码里；
- 对外只暴露 run(model, prompt)；错误分类交给 model_fallback（401/403 → AuthError 停链）；
- opencode run 的 JSON 事件流按行解析，取 text 事件聚合为输出。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# 需要原样透传给 model_fallback 的 HTTP 状态码（会出现在 error 事件里）
_STATUS_MARKERS = ("statusCode", "status_code")

# npm 生成的 .cmd shim 形如 `"%dp0%\node_modules\<pkg>\bin\<name>.exe" %*`
_SHIM_EXE_RE = re.compile(r'"([^"]+\.exe)"', re.IGNORECASE)

# CLI 文本模式会输出 ANSI 颜色；转成日志/detail 前清掉
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def resolve_binary(binary: str) -> str:
    """解析出真正可执行的原生路径。

    实测（2026-10-25）：Windows 上 `shutil.which` 返回 opencode.CMD；batch 文件经
    cmd.exe 执行时**多行参数会在第一个换行处被截断**（模型只收到 prompt 第一行，
    导致空补丁）。shim 旁边通常有原生 .exe，优先直接用 .exe 绕开 cmd.exe。
    找不到原生 exe 时保持原路径，由调用方决定是否可用。
    """
    import shutil
    path = shutil.which(binary) or binary
    p = Path(path)
    if os.name == "nt" and p.suffix.lower() in {".cmd", ".bat"}:
        native = _native_exe_from_shim(p)
        if native is not None:
            return str(native)
    return path


def _native_exe_from_shim(shim: Path) -> Path | None:
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for m in _SHIM_EXE_RE.finditer(text):
        raw = m.group(1)
        for token in ("%~dp0%", "%~dp0", "%dp0%", "%dp0"):
            raw = raw.replace(token, str(shim.parent))
        cand = Path(raw)
        if not cand.is_absolute():
            cand = shim.parent / cand
        if cand.is_file():
            return cand
    return None


def _child_env() -> dict[str, str]:
    """子进程环境：去掉 OPENCODE_* 会话标记，但保留凭据。

    实测（2026-10-25）：在 opencode 会话内（OPENCODE=1/OPENCODE_PID 存在）调用 CLI，
    它会附着到父会话的项目，**忽略 cwd 与 --dir**（模型读写了错误的仓库）。清掉
    这些标记后 worker 才会在最初指定的目录里独立运行。

    注意 OPENCODE_API_KEY 同样以 OPENCODE 开头，但它是凭据不是会话标记：
    一并剥掉会让沙箱里的 CLI 无法认证（repair 直接失败）。
    """
    keep = {"OPENCODE_API_KEY"}
    return {k: v for k, v in os.environ.items()
            if (not k.startswith("OPENCODE") or k in keep) and k != "PWD"}


class OpenCodeClient:
    def __init__(self, binary: str = "opencode", timeout_seconds: int = 600,
                 workdir: Path | None = None, json_events: bool = False):
        self.binary = resolve_binary(binary)
        self.timeout = timeout_seconds
        self.workdir = workdir
        self.json_events = json_events
        if Path(self.binary).suffix.lower() in {".cmd", ".bat"}:
            raise RuntimeError(
                f"{self.binary} is a shell shim with no native .exe beside it; "
                "multi-line prompts would be truncated by cmd.exe — refusing to run")

    def run(self, model: str, prompt: str) -> tuple[bool, str]:
        """执行一次 opencode run。返回 (ok, detail)。

        ok=True 时 detail 为模型输出文本；ok=False 时 detail 含可分类的错误
        串（包含 "401"/"403"/"429"/状态码与消息），由调用方决定停链还是换挡。

        实测（2026-10-28，Modal Sandbox）：`--format json` 在容器里会静默挂死
        （240s 零输出，rc=124），而默认文本模式 180s 内正常返回并直接改文件。
        因此默认走文本模式；json 事件流只在显式要求时使用（本地调试）。
        """
        cmd = [self.binary, "run", "-m", model]
        # --auto：必须加，否则 agent 一碰到需要确认的动作就无声退出。
        # 2026-10-30 实测 build agent 的权限表里有
        #   {"permission":"external_directory","action":"ask","pattern":"*"}
        # 而修复 agent 必然要访问工作目录之外的东西（~/.local/bin/uv、/tmp、
        # HOME 下的缓存）。headless 下没人回答 ask，opencode 直接结束——
        # 表现为"说一句话就退出"，我们白跑一整轮 Actions。
        # opencode run 本身就是多轮 agent loop（read/edit/bash 连续调用直到完成），
        # 加上 --auto 才真正能跑到"完成"而不是卡在第一次确认。
        cmd.append("--auto")
        if self.json_events:
            cmd += ["--format", "json"]
        if self.workdir is not None:
            cmd += ["--dir", str(self.workdir)]
        cmd.append(prompt)
        last: tuple[bool, str] = (False, "")
        for attempt in range(3):
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=self.timeout,
                                   cwd=str(self.workdir) if self.workdir else None,
                                   stdin=subprocess.DEVNULL,
                                   env=_child_env())
            except FileNotFoundError:
                return False, "opencode binary not found"
            except subprocess.TimeoutExpired as exc:
                # 超时也要把已经产生的输出带回去。2026-10-30 实测：pulumi 是 Go
                # 大仓库，agent 跑了整 20 分钟（正好撞上 timeout）然后返回一句
                # 固定字符串，完全看不出它卡在哪一步——是还在探索、还是在等权限。
                tail = ""
                for stream in (exc.stdout, exc.stderr):
                    if not stream:
                        continue
                    text = (stream.decode("utf-8", "replace")
                            if isinstance(stream, bytes) else str(stream))
                    text = _strip_ansi(text).strip()
                    if text:
                        tail = text[-800:]
                reason = (f"agent timed out after {self.timeout}s"
                          + (f"; last output: {tail}" if tail else "; no output captured"))
                return False, reason
            ok, detail = self._parse_events(p.stdout, p.stderr, p.returncode)
            if ok:
                return True, detail
            last = (ok, detail)
            # 本机 TLS 抖动是常态：证书/握手/EOF 类瞬时错误有限重试；
            # 401/403 认证错误绝不重试（停链语义归调用方）
            transient = any(m in detail.lower() for m in
                            ("certificate", "handshake", "eof", "connection reset",
                             "temporarily unavailable"))
            if not transient or "401" in detail or "403" in detail:
                return last
        return last

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
                # 实测（2026-10-25）：文本在 part.text 里，老版本可能直接在顶层 text
                part = ev.get("part")
                t = part.get("text") if isinstance(part, dict) else ev.get("text")
                texts.append(str(t or ""))
        if err_detail is not None:
            return False, err_detail
        if rc != 0:
            tail = (stderr or "").strip()[-300:]
            return False, f"exit {rc}: {tail}" if tail else f"exit {rc}"
        out = "\n".join(t for t in texts if t).strip()
        if not out:
            # 文本模式（默认）：stdout 就是模型回复，可能有 ANSI 与会话横幅行。
            # 修复场景的关键产物是文件改动，caller 会以 git diff 为准；
            # 这里只要拿到非空文本即视为成功，避免把好结果误判成 empty。
            out = _strip_ansi(stdout or "").strip()
        return (True, out[:4000]) if out else (False, "empty response (no text events)")

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
        p = subprocess.run([resolve_binary(binary), "models"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    return [ln.strip() for ln in p.stdout.splitlines()
            if ln.strip().endswith("-free")]
