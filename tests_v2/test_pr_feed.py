import json
from types import SimpleNamespace

import pytest

from scout_v2 import pr_feed


def row(number, state="OPEN"):
    return {"repository": {"nameWithOwner": "a/b"}, "number": number, "title": "fix",
            "url": f"https://github.com/a/b/pull/{number}", "state": state,
            "createdAt": "2026-11-22T11:00:00Z", "updatedAt": "2026-11-22T11:00:00Z",
            "closedAt": None}


def page(nodes, *, count, next_cursor=None):
    return {"nodes": nodes, "totalCount": count,
            "pageInfo": {"hasNextPage": bool(next_cursor), "endCursor": next_cursor}}


def test_delta_merges_states_then_full_reconciles_removed_rows(monkeypatch):
    now = [1000]
    calls = []

    def read(cursor, cutoff):
        calls.append((cursor, cutoff))
        return next(pages)

    pages = iter([page([row(1)], count=2, next_cursor="second"),
                  page([row(2, "MERGED")], count=2),
                  page([row(1, "CLOSED"), row(3)], count=2),
                  page([row(3)], count=1)])
    monkeypatch.setattr(pr_feed.time, "time", lambda: now[0])
    monkeypatch.setattr(pr_feed, "_read_page", read)
    feed = pr_feed.PullRequestFeed()
    assert [r["state"] for r in feed.snapshot(full=False)] == ["open", "merged"]
    now[0] += 30
    assert {r["number"]: r["state"] for r in feed.snapshot(full=False)} == {
        1: "closed", 2: "merged", 3: "open"}
    assert [r["number"] for r in feed.snapshot(full=True)] == [3]
    assert calls == [(None, None), ("second", None), (None, 700), (None, None)]


def test_rate_limit_keeps_baseline_and_watermark_until_reset(monkeypatch):
    now = [1000]
    calls = []

    def read(cursor, cutoff):
        calls.append((cursor, cutoff))
        if len(calls) == 2:
            return page([row(2)], count=2, next_cursor="second")
        if len(calls) == 3:
            raise pr_feed.PullRequestRateLimited(130)
        return page([row(1)], count=1)

    monkeypatch.setattr(pr_feed.time, "time", lambda: now[0])
    monkeypatch.setattr(pr_feed, "_read_page", read)
    feed = pr_feed.PullRequestFeed()
    first = feed.snapshot(full=True)
    now[0] = 1030
    with pytest.raises(pr_feed.PullRequestRateLimited, match="130s"):
        feed.snapshot(full=False)
    now[0] = 1159
    with pytest.raises(pr_feed.PullRequestRateLimited):
        feed.snapshot(full=False)
    assert len(calls) == 3 and feed.watermark == 1000
    assert list(feed.rows.values()) == first
    now[0] = 1160
    assert feed.snapshot(full=False) == first
    assert calls[-1] == (None, 700)


@pytest.mark.parametrize(("status", "headers", "body", "delay"), [
    (403, "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 1220", {"message": "rate limit exceeded"}, 221),
    (429, "Retry-After: 95", {"message": "busy"}, 96),
    (200, "X-RateLimit-Remaining: 0\nX-RateLimit-Reset: 1010", {"errors": [{"type": "RATE_LIMITED"}]}, 11),
    (403, "", {"message": "secondary rate limit"}, 0),
])
def test_rate_limit_response_headers(monkeypatch, status, headers, body, delay):
    response = SimpleNamespace(returncode=0 if status == 200 else 1, stderr="",
                               stdout=f"HTTP/2.0 {status}\n{headers}\n\n" + json.dumps(body))
    monkeypatch.setattr(pr_feed.time, "time", lambda: 1000)
    monkeypatch.setattr(pr_feed.subprocess, "run", lambda *a, **k: response)
    with pytest.raises(pr_feed.PullRequestRateLimited) as exc:
        pr_feed._read_page(None, None)
    assert exc.value.retry_after == delay


def test_incomplete_full_snapshot_never_replaces_baseline(monkeypatch):
    pages = iter([page([row(1)], count=1), page([], count=1)])
    monkeypatch.setattr(pr_feed, "_read_page", lambda *_: next(pages))
    feed = pr_feed.PullRequestFeed()
    baseline = feed.snapshot(full=True)
    with pytest.raises(RuntimeError, match="incomplete"):
        feed.snapshot(full=True)
    assert list(feed.rows.values()) == baseline


def test_stale_search_result_cannot_overwrite_newer_full_row(monkeypatch):
    newer = {**row(1, "MERGED"), "updatedAt": "2026-11-22T11:01:00Z"}
    pages = iter([page([newer], count=1), page([row(1)], count=1)])
    monkeypatch.setattr(pr_feed, "_read_page", lambda *_: next(pages))
    feed = pr_feed.PullRequestFeed()
    current = feed.snapshot(full=True)
    assert feed.snapshot(full=False) == current
