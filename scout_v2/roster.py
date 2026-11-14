"""Shared repository boundary for issue and PR automation."""

import tomllib
from pathlib import Path


def repo_key(repo: str) -> str:
    key = repo.strip().lower()
    # GitHub moved the repository without changing its repository ID.
    return "prefecthq/fastmcp" if key == "jlowin/fastmcp" else key


def monitored_repo(repo: str) -> bool:
    config = Path(__file__).resolve().parents[1] / "cloudflare/wrangler.toml"
    with config.open("rb") as source:
        variables = tomllib.load(source)["vars"]
    # PR tracking includes honorary repositories paused for new issue discovery.
    repos = variables.get("TRACKED_REPOS", variables["REPOS"])
    return repo_key(repo) in {repo_key(name) for name in repos.split(",")}
