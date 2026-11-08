"""Persistent model overrides set from the authenticated console."""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml


MODEL_PATTERNS = {"repair_model": r"opencode-go/[\w.-]+", "reply_model": r"(?:opencode-go/)?[\w.-]+",
                  "codiv_model": r"[\w./-]+"}


def model_setting_error(key: str, value: str) -> str:
    if len(value) > 512:
        return "输入超过 512 字符。"
    if key in {"opencode_key", "codiv_key"}:
        return "" if value and not any(c.isspace() for c in value) else "API 密钥不能为空或包含空白。"
    if key == "opencode_variant":
        return "" if not value or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value) else "思考强度需要填写模型 variant。"
    if key == "codiv_think":
        return "" if re.fullmatch(r"[0-9]{1,4}", value) and int(value) <= 4096 else "思考预算须为 0–4096 的整数。"
    if value.lower().startswith(("sk-", "oc_")):
        return "请将 API 密钥填入对应的密钥列。"
    if key == "codiv_base":
        try:
            base = urlsplit(value)
            if (base.scheme == "https" and base.hostname
                    and (base.hostname == "codiv.ai" or base.hostname.endswith(".codiv.ai"))
                    and not (base.username or base.password or base.query or base.fragment)):
                return ""
        except ValueError:
            pass
        return "Codiv Base URL 需要使用官方 codiv.ai HTTPS 地址。"
    return "" if re.fullmatch(MODEL_PATTERNS[key], value) else "模型名称格式有误。"


def effective_models(root: Path) -> dict:
    from .workers.issue_scorer import load_scorer_config
    from .workers.openjev import OpenJevClient
    from .workers.repair import resolve_model

    reply = load_scorer_config(root)
    codiv = OpenJevClient(root)
    values = read_models(root)
    return {
        "repair_model": values.get("repair_model") or resolve_model(),
        "reply_model": reply["model"], "opencode_key": reply.get("api_key") or "",
        "opencode_variant": values.get("opencode_variant") or "",
        "codiv_base": codiv.base, "codiv_model": codiv.model, "codiv_key": codiv.key,
        "codiv_think": str(codiv.think),
    }


def public_models(root: Path) -> dict:
    values = effective_models(root)
    for provider in ("opencode", "codiv"):
        values[f"{provider}_configured"] = bool(values.pop(f"{provider}_key"))
    return values


def repair_model_selector(root: Path) -> str:
    from .workers.repair import resolve_model

    values = read_models(root)
    model = values.get("repair_model") or resolve_model()
    variant = values.get("opencode_variant")
    return f"{model}#{variant}" if variant else model


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
