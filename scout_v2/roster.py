"""Shared repository boundary for issue and PR automation."""

import tomllib
from pathlib import Path


def repo_key(repo: str) -> str:
    key = repo.strip().lower()
    # GitHub moved the repository without changing its repository ID.
    return "jlowin/fastmcp" if key == "prefecthq/fastmcp" else key


def monitored_repo(repo: str) -> bool:
    config = Path(__file__).resolve().parents[1] / "cloudflare/wrangler.toml"
    with config.open("rb") as source:
        repos = tomllib.load(source)["vars"]["REPOS"]
    return repo_key(repo) in {name.strip().lower() for name in repos.split(",")}
