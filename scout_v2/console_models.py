"""Persistent model overrides set from the authenticated console."""

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import yaml


MODEL_PATTERNS = {"repair_model": r"opencode-go/[\w.-]+", "reply_model": r"(?:opencode-go/)?[\w.-]+",
                  "codiv_model": r"[\w./-]+"}


def model_setting_error(key: str, value: str) -> str:
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


def public_models(root: Path) -> dict:
    from .workers.issue_scorer import load_scorer_config
    from .workers.openjev import OpenJevClient
    from .workers.repair import resolve_model

    reply = load_scorer_config(root)
    codiv = OpenJevClient(root)
    return {
        "repair_model": read_models(root).get("repair_model") or resolve_model(),
        "reply_model": reply["model"], "opencode_configured": bool(reply.get("api_key")),
        "codiv_base": codiv.base, "codiv_model": codiv.model, "codiv_configured": bool(codiv.key),
    }


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
