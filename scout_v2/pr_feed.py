"""Fetch the authenticated author's PRs without consuming the search quota."""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import time

_PR_FIELDS = """
fragment PrRecord on PullRequest {
  repository { nameWithOwner } number title url state createdAt updatedAt closedAt
}
"""
_FULL_QUERY = """
query($cursor: String) {
  viewer {
    pullRequests(first: 100, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { ...PrRecord }
    }
  }
}
""" + _PR_FIELDS
_DELTA_QUERY = """
query($cursor: String, $terms: String!) {
  search(query: $terms, type: ISSUE, first: 100, after: $cursor) {
    totalCount: issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ...PrRecord }
  }
}
""" + _PR_FIELDS


class PullRequestRateLimited(RuntimeError):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"GitHub PR API rate limited; retry in {int(retry_after + 0.999)}s")


def _github_json(args):
    command = ["gh", "api", "--include", *args]
    try:
        response = subprocess.run(command, capture_output=True, encoding="utf-8",
                                  errors="replace", timeout=45)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub PR snapshot timed out") from exc
    parts = re.split(r"\r?\n\r?\n", response.stdout, maxsplit=1)
    headers, status = {}, 0
    if len(parts) == 2 and parts[0].startswith("HTTP/"):
        line, *fields = parts[0].splitlines()
        status = int(line.split()[1])
        headers = {name.lower(): value.strip() for name, _, value in
                   (field.partition(":") for field in fields)}
    try:
        body = json.loads(parts[-1])
    except json.JSONDecodeError:
        body = {}
    errors = body.get("errors") or []
    message = (response.stderr + " " + str(body.get("message") or "") + " "
               + " ".join(str(error.get("message") or "") for error in errors)).lower()
    limited = (status == 429 or "rate limit" in message
               or any(error.get("type") == "RATE_LIMITED" for error in errors)
               or (status == 403 and headers.get("x-ratelimit-remaining") == "0"))
    if limited:
        deadlines = []
        retry_after = headers.get("retry-after", "")
        reset = headers.get("x-ratelimit-reset", "")
        if retry_after.isdecimal():
            deadlines.append(int(retry_after) + 1)
        if headers.get("x-ratelimit-remaining") == "0" and reset.isdecimal():
            deadlines.append(max(1, int(reset) - time.time() + 1))
        raise PullRequestRateLimited(max(deadlines, default=0))
    if response.returncode or errors or not body:
        raise RuntimeError(f"GitHub PR snapshot unavailable (HTTP {status or 'unknown'})")
    return body


def _read_page(cursor, cutoff):
    args = ["graphql", "-f", "query=" + (_FULL_QUERY if cutoff is None else _DELTA_QUERY)]
    if cursor:
        args.extend(["-f", "cursor=" + cursor])
    if cutoff is not None:
        since = dt.datetime.fromtimestamp(cutoff, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        args.extend(["-f", f"terms=is:pr author:@me updated:>={since}"])
    data = _github_json(args)["data"]
    return data["viewer"]["pullRequests"] if cutoff is None else data["search"]


class PullRequestFeed:
    """Keep a complete baseline and overlay PRs updated since the previous read."""
    def __init__(self, root=None):
        self.root = root
        self.rows = {}
        self.watermark = None
        self.version = 0
        self.retry_at = 0
        self.rate_failures = 0

    def _check_backoff(self):
        remaining = self.retry_at - time.time()
        if remaining > 0:
            raise PullRequestRateLimited(remaining)

    def _defer(self, exc):
        self.rate_failures += 1
        delay = exc.retry_after or 60 * 2 ** min(self.rate_failures - 1, 5)
        self.retry_at = time.time() + delay
        return PullRequestRateLimited(delay)

    def read_pr(self, repo, number):
        self._check_backoff()
        try:
            result = _github_json([f"repos/{repo}/pulls/{number}"])
        except PullRequestRateLimited as exc:
            raise self._defer(exc) from exc
        self.retry_at = self.rate_failures = 0
        return result

    def snapshot(self, *, full: bool):
        self._check_backoff()
        started = time.time()
        full = full or self.watermark is None
        # The overlap covers seconds-level timestamps and changes during pagination.
        cutoff = None if full else self.watermark - 300
        fetched = {}
        cursor = None
        cursors = set()
        total = None
        try:
            edge_page = None
            if self.root is not None:
                from .edge_reads import EdgeRateLimited, edge_read
                since = (None if cutoff is None else dt.datetime.fromtimestamp(
                    cutoff, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
                try:
                    edge_page = edge_read(self.root, "pr-snapshot", {"cutoff": since})
                except EdgeRateLimited as exc:
                    raise PullRequestRateLimited(exc.seconds) from exc
                if edge_page is not None and edge_page.get("pageInfo", {}).get("hasNextPage") is not False:
                    raise RuntimeError("Cloudflare PR snapshot incomplete")
            while True:
                page = edge_page if edge_page is not None else _read_page(cursor, cutoff)
                expected = page["totalCount"]
                if total is not None and expected != total:
                    raise RuntimeError("GitHub PR snapshot count changed during pagination")
                total = expected
                for node in page["nodes"]:
                    repo = node["repository"]["nameWithOwner"]
                    key = (repo.lower(), node["number"])
                    state = node["state"].lower()
                    if key in fetched or state not in ("open", "closed", "merged"):
                        raise RuntimeError("GitHub PR snapshot contains an invalid or duplicate PR")
                    fetched[key] = {"repo": repo, "number": node["number"], "title": node["title"],
                                    "url": node["url"], "state": state, "created_at": node["createdAt"],
                                    "updated_at": node["updatedAt"], "closed_at": node["closedAt"]}
                info = page["pageInfo"]
                if not info["hasNextPage"]:
                    break
                cursor = info["endCursor"]
                if not cursor or cursor in cursors or not page["nodes"]:
                    raise RuntimeError("GitHub PR snapshot pagination stalled")
                cursors.add(cursor)
        except PullRequestRateLimited as exc:
            raise self._defer(exc) from exc
        if len(fetched) != total:
            raise RuntimeError("GitHub PR snapshot is incomplete")
        rows = fetched
        if not full:
            # Search indexing can lag behind a newer row from the full feed.
            rows = self.rows.copy()
            for key, row in fetched.items():
                if key not in rows or row["updated_at"] >= rows[key]["updated_at"]:
                    rows[key] = row
        if rows != self.rows:
            self.version += 1
        self.rows = rows
        self.watermark = started
        self.retry_at = self.rate_failures = 0
        return [row.copy() for row in rows.values()]
