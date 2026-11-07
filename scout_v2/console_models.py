"""Persistent model overrides set from the authenticated console."""

import os
import tempfile
from pathlib import Path

import yaml


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
