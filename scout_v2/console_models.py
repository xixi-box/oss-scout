"""Persistent model overrides set from the authenticated console."""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml


DEFAULT_OPENCODE_BASE = "https://opencode.ai/zen/go/v1"
MODEL_PATTERNS = {"repair_model": r"opencode-go/[\w.-]+", "reply_model": r"(?:opencode-go/)?[\w.-]+",
                  "codiv_model": r"[\w./-]+", "codex_model": r"[\w.-]*"}


def model_setting_error(key: str, value: str) -> str:
    if len(value) > 512:
        return "输入超过 512 字符。"
    if key == "repair_agent":
        return "" if value in ("opencode", "codex") else "请选择 OpenCode 或 Codex。"
    if key in {"candidate_pool_size", "daily_pr_quota"}:
        return "" if re.fullmatch(r"[1-9][0-9]*", value) else "请填写大于 0 的整数。"
    if key == "codex_variant":
        return "" if value in ("", "low", "medium", "high", "xhigh") else "请选择默认、low、medium、high 或 xhigh。"
    if key in {"opencode_key", "codiv_key"}:
        return "" if value and not any(c.isspace() for c in value) else "API 密钥不能为空或包含空白。"
    if key == "opencode_variant":
        return "" if not value or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value) else "思考强度需要填写模型 variant。"
    if key == "codiv_think":
        return "" if re.fullmatch(r"[0-9]{1,4}", value) and int(value) <= 4096 else "思考预算须为 0–4096 的整数。"
    if value.lower().startswith(("sk-", "oc_")):
        return "请将 API 密钥填入对应的密钥列。"
    if key in {"codiv_base", "opencode_base"}:
        domain = "codiv.ai" if key == "codiv_base" else "opencode.ai"
        provider = "Codiv" if key == "codiv_base" else "OpenCode"
        try:
            base = urlsplit(value)
            if (base.scheme == "https" and base.hostname
                    and (base.hostname == domain or base.hostname.endswith("." + domain))
                    and (key == "codiv_base" or base.port in (None, 443))
                    and not (base.username or base.password or base.query or base.fragment)):
                return ""
        except ValueError:
            pass
        return f"{provider} Base URL 需要使用官方 {domain} HTTPS 地址。"
    return "" if re.fullmatch(MODEL_PATTERNS[key], value) else "模型名称格式有误。"


def effective_models(root: Path) -> dict:
    from .workers.issue_scorer import load_scorer_config
    from .workers.openjev import OpenJevClient
    from .workers.repair import resolve_model

    reply = load_scorer_config(root)
    codiv = OpenJevClient(root)
    values = read_models(root)
    return {
        "repair_agent": values.get("repair_agent") or "opencode",
        "candidate_pool_size": str(runtime_limit(root, "candidate_pool_size", 10)),
        "daily_pr_quota": str(daily_pr_quota(root)),
        "codex_model": values.get("codex_model") or "",
        "codex_variant": values.get("codex_variant") or "",
        "repair_model": values.get("repair_model") or resolve_model(),
        "reply_model": reply["model"], "opencode_key": reply.get("api_key") or "",
        "opencode_base": reply["base_url"],
        "opencode_variant": values.get("opencode_variant") or "",
        "codiv_base": codiv.base, "codiv_model": codiv.model, "codiv_key": codiv.key,
        "codiv_think": str(codiv.think),
    }


def public_models(root: Path) -> dict:
    values = effective_models(root)
    for provider in ("opencode", "codiv"):
        values[f"{provider}_configured"] = bool(values.pop(f"{provider}_key"))
    return values


def opencode_base_url(root: Path) -> str:
    return (read_models(root).get("opencode_base") or DEFAULT_OPENCODE_BASE).rstrip("/")


def repair_model_selector(root: Path) -> str:
    return repair_settings(root)[1]


def repair_settings(root: Path) -> tuple[str, str]:
    """Read the tool and its model from one atomic configuration snapshot."""
    from .workers.repair import resolve_model

    values = read_models(root)
    agent = values.get("repair_agent") or "opencode"
    if agent not in ("opencode", "codex"):
        raise ValueError("Unknown repair agent")
    if agent == "codex":
        model = values.get("codex_model") or ""
        variant = values.get("codex_variant") or ""
    else:
        model = values.get("repair_model") or resolve_model()
        variant = values.get("opencode_variant") or ""
    return agent, f"{model}#{variant}" if variant else model


def rotate_opencode_key(root: Path, key: str) -> None:
    from .workers.auto_pipeline import _actions_runner_repo

    if _actions_runner_repo(root) != "xixi-box/oss-scout":
        raise ValueError("两个 Actions 仓库需要分别更新 OpenCode 密钥。")
    try:
        changed = subprocess.run(
            ["gh", "secret", "set", "OPENCODE_API_KEY", "-R", "xixi-box/oss-scout"],
            input=key, text=True, capture_output=True, timeout=25)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("无法更新 GitHub Actions 的 OpenCode 密钥。") from None
    if changed.returncode:
        raise RuntimeError("无法更新 GitHub Actions 的 OpenCode 密钥。")


def read_models(root: Path) -> dict:
    try:
        return yaml.safe_load((root / "configs/console_models.yaml").read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}


def runtime_limit(root: Path, key: str, default: int) -> int:
    value = str(read_models(root).get(key, ""))
    return default if model_setting_error(key, value) else int(value)


def daily_pr_quota(root: Path) -> int:
    path = root / "configs/repositories.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    default = int(((config or {}).get("defaults") or {}).get("max_new_claims_per_business_day", 5))
    return runtime_limit(root, "daily_pr_quota", default)




def write_models(root: Path, values: dict) -> None:
    path = root / "configs/console_models.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
        try:
            yaml.safe_dump(values, handle)
            handle.close()
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
