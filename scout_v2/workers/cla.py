"""CLA 签署。

2026-10-31：池子里有 6 个仓库要签 CLA，机制分三种，能不能自动化完全不同：

  comment_sign    回复一句固定口令就完成签署（Arize-ai/phoenix）。可全自动。
  cla-assistant   第三方 App，点链接走 OAuth。**不能自动**——那一步要真人在
                  网页上授权，agent 做不到也不该碰。
  microsoft       EasyCLA，同样要网页签署。

对不能自动的，价值在于**提前知道**：开 PR 前就提示"这个仓库要你先去签 CLA"，
而不是把 PR 开出去挂着一个 not_signed 的检查项，让 maintainer 看到一个连 CLA
都没签的贡献者。

判断依据只读 configs/pr_gates.yaml 的 cla 段——那是核实过的，不要在这里硬编码。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

SIGNABLE = {"comment_sign"}      # 能自动签的类型


def _gates(root: Path) -> dict:
    for p in (Path("/app/configs.seed/pr_gates.yaml"),
              root / "configs" / "pr_gates.yaml",
              Path(__file__).resolve().parents[2] / "configs" / "pr_gates.yaml"):
        if p.exists():
            try:
                return (yaml.safe_load(p.read_text(encoding="utf-8")) or {})
            except yaml.YAMLError:
                continue
    return {}


def requirement(root: Path, repo: str) -> dict | None:
    """这个仓库要签什么 CLA。不需要则返回 None。"""
    cfg = _gates(root).get("cla") or {}
    entry = cfg.get(repo)
    return dict(entry) if isinstance(entry, dict) else None


def can_auto_sign(root: Path, repo: str) -> bool:
    req = requirement(root, repo)
    return bool(req and req.get("service") in SIGNABLE and req.get("phrase"))


def _gh(args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["gh"] + args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timeout")


def _signed(root: Path, repo: str, pr_number: int) -> bool | None:
    """PR 上 CLA 检查的状态。None = 看不出来（不要当成已签）。

    认两种痕迹：cla-assistant 的 bot 评论，以及 statusCheckRollup 里的 CLA 项。
    """
    out = _gh(["pr", "view", str(pr_number), "-R", repo,
               "--json", "statusCheckRollup,comments"])
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return None
    for c in (data.get("statusCheckRollup") or []):
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or c.get("context") or "").lower()
        if "cla" not in name:
            continue
        concl = (c.get("conclusion") or c.get("state") or "").upper()
        if concl in ("SUCCESS", "NEUTRAL"):
            return True
        if concl in ("FAILURE", "ERROR", "PENDING", "ACTION_REQUIRED"):
            return False
    for c in (data.get("comments") or []):
        body = str(c.get("body") or "").lower()
        if "cla" not in body:
            continue
        if "not_signed" in body or "not signed" in body:
            return False
        if "signed" in body and "not_signed" not in body:
            return True
    return None


def sign(root: Path, repo: str, pr_number: int) -> dict:
    """在 PR 上发签署口令。返回 {ok, action, detail}。

    只在仓库支持 comment_sign 时调用；其他类型返回 need_manual。
    """
    req = requirement(root, repo)
    if not req:
        return {"ok": True, "action": "not_required", "detail": ""}
    service = req.get("service")
    if service not in SIGNABLE:
        return {"ok": False, "action": "need_manual",
                "detail": f"{repo} requires CLA via {service}; a human has to sign it "
                          f"in the browser before the PR can merge"}
    state = _signed(root, repo, pr_number)
    if state is True:
        return {"ok": True, "action": "already_signed", "detail": ""}
    phrase = str(req.get("phrase") or "").strip()
    if not phrase:
        return {"ok": False, "action": "need_manual",
                "detail": "no signature phrase configured"}
    out = _gh(["pr", "comment", str(pr_number), "-R", repo, "--body", phrase])
    if out.returncode != 0:
        return {"ok": False, "action": "comment_failed",
                "detail": (out.stderr or "")[:200]}
    return {"ok": True, "action": "signed", "detail": phrase}


def sign_url(root: Path, repo: str) -> str:
    """人工签 CLA 的入口链接。签一次终身有效。"""
    req = requirement(root, repo)
    if not req:
        return ""
    service = req.get("service")
    if service == "cla-assistant":
        return f"https://cla-assistant.io/{repo}"
    if service == "microsoft":
        return "https://cla.opensource.microsoft.com"
    return ""


def preflight(root: Path, repo: str) -> dict:
    """开 PR 之前的检查。返回 {blocking, need_manual, detail, url}。

    blocking=True 表示这个仓库的 CLA 只能人工签，现在开 PR 也是挂着等，
    应该先提醒人去签。
    """
    req = requirement(root, repo)
    if not req:
        return {"blocking": False, "need_manual": False, "detail": "", "url": ""}
    service = req.get("service")
    if service in SIGNABLE:
        return {"blocking": False, "need_manual": False, "url": "",
                "detail": f"CLA signs automatically ({service})"}
    url = sign_url(root, repo)
    return {"blocking": True, "need_manual": True, "url": url,
            "detail": (f"{repo} requires a CLA signed via {service} in the browser. "
                       f"Sign it once at {url or 'the project CLA page'}, then this "
                       f"repo can be contributed to normally.")}
