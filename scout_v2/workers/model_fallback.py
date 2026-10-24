"""OpenCode 免费优先模型路由（计划 §11/§16）：免费按序尝试，付费 fail-closed。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ModelClient(Protocol):
    """模型调用协议。真实实现包 opencode CLI；测试用 fake。返回 (ok, detail)。"""

    def run(self, model: str, prompt: str) -> tuple[bool, str]: ...


class PaidDisabledError(RuntimeError):
    """免费模型全部失败且付费未授权：暂停任务，绝不静默调付费。"""


class AuthError(RuntimeError):
    """401/403 认证失败：报警并停止整条凭证链，不得自动切付费。"""


@dataclass
class ModelPolicy:
    mode: str = "free_first"
    allow_paid_api: bool = False
    max_retries_per_model: int = 1
    free_preferences: list[str] = field(default_factory=list)
    primary_models: list[str] = field(default_factory=list)   # 用户指定的固定首选（如订阅模型）
    paid_fallback_enabled: bool = False

    def models(self) -> list[str]:
        """按序尝试的模型列表：primary 优先，其次免费偏好。"""
        return self.primary_models + self.free_preferences

    @classmethod
    def from_yaml(cls, cfg: dict | None) -> "ModelPolicy":
        cfg = cfg or {}
        return cls(
            mode=cfg.get("mode", "free_first"),
            allow_paid_api=bool(cfg.get("allow_paid_api", False)),
            max_retries_per_model=int(cfg.get("max_retries_per_model", 1)),
            free_preferences=list((cfg.get("free_preferences") or [])),
            primary_models=list((cfg.get("primary_models") or [])),
            paid_fallback_enabled=bool((cfg.get("paid_fallback") or {}).get("enabled", False)),
        )


# 可重试的暂时性失败（换下一个免费模型继续）；402/429/5xx/下线
RETRYABLE_MARKERS = ("402", "429", "rate limit", "quota", "unavailable",
                     "500", "502", "503", "504", "offline", "decommissioned")


def run_with_fallback(policy: ModelPolicy, client: ModelClient, prompt: str,
                      *, on_alert=None) -> tuple[str, str]:
    """按免费偏好顺序调用；返回 (model, output)。

    - 暂时性失败（402/429/5xx/下线）→ 尝试下一个免费模型（每模型 max_retries 次）。
    - 401/403 → AuthError：报警并停链（凭证链可能已泄露/被撤，重试无意义且危险）。
    - 全部免费失败：付费未启用 → PaidDisabledError（暂停任务）；已启用仍需外部授权额。
    """
    tried: list[str] = []
    for model in policy.models():
        for _ in range(policy.max_retries_per_model + 1):
            ok, detail = client.run(model, prompt)
            if ok:
                return model, detail
            if "401" in detail or "403" in detail or "unauthorized" in detail.lower():
                if on_alert:
                    on_alert(f"model auth failure on {model}: {detail[:120]}")
                raise AuthError(f"auth error on {model}: {detail[:200]}")
            tried.append(f"{model}: {detail[:80]}")
            if not any(m in detail.lower() for m in RETRYABLE_MARKERS):
                break  # 非暂时性失败（如内容拒绝）：换模型
    if policy.allow_paid_api and policy.paid_fallback_enabled:
        raise PaidDisabledError(
            "free models exhausted; paid fallback is configured but requires "
            "verifiable prepaid budget + explicit approval before first use")
    raise PaidDisabledError("free models exhausted (" + "; ".join(tried) + "); paid disabled — pausing task")
