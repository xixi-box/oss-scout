"""Repository weights shared by dispatch and the Feishu candidate table."""

import json
import math
from pathlib import Path

import yaml

from .roster import repo_key


WEIGHT_STATE = "state/repo_weights.json"


def numeric_weight(value):
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if type(value) is int:
        return value
    if type(value) is float and math.isfinite(value):
        return int(value) if value.is_integer() else value
    return None


def repo_weights(root: Path) -> dict[str, int | float]:
    roster = root / "configs/repositories.yaml"
    if not roster.exists():
        roster = Path(__file__).resolve().parents[1] / "configs/repositories.yaml"
    repos = (yaml.safe_load(roster.read_text(encoding="utf-8")) or {}).get("repositories") or {}
    weights = {repo_key(repo): numeric_weight((spec or {}).get("weight")) or 0
               for repo, spec in repos.items()}
    try:
        saved = json.loads((root / WEIGHT_STATE).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return weights
    if isinstance(saved, dict):
        for key in weights.keys() & saved.keys():
            if (value := numeric_weight(saved[key])) is not None:
                weights[key] = value
    return weights


def save_repo_weights(root: Path, weights: dict[str, int | float]) -> None:
    from .state_store import atomic_json_write

    path = root / WEIGHT_STATE
    try:
        if json.loads(path.read_text(encoding="utf-8")) == weights:
            return
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, weights, backup=False)
