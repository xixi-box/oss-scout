"""Shared repository boundary for issue and PR automation."""

import tomllib
from pathlib import Path


def repo_key(repo: str) -> str:
    key = repo.strip().lower()
    # GitHub moved the repository without changing its repository ID.
    return "prefecthq/fastmcp" if key == "jlowin/fastmcp" else key


def _worker_repos() -> dict:
    config = Path(__file__).resolve().parents[1] / "cloudflare/wrangler.toml"
    with config.open("rb") as source:
        return tomllib.load(source)["vars"]


def monitored_repo(repo: str) -> bool:
    variables = _worker_repos()
    # PR tracking includes honorary repositories paused for new issue discovery.
    repos = variables.get("TRACKED_REPOS", variables["REPOS"])
    return repo_key(repo) in {repo_key(name) for name in repos.split(",")}


def scans_issues(repo: str) -> bool:
    return repo_key(repo) in {repo_key(name) for name in _worker_repos()["REPOS"].split(",")}
