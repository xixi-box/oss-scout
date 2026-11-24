import json
import urllib.error
import pytest
from scout_v2 import edge_reads, pr_feed


def config(tmp_path, monkeypatch):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/cloudflare.yaml").write_text(
        'edge_reads_enabled: true\nedge_url: "https://oss-scout-hunter.example.workers.dev"\ningest_token: "fixture"\n')
    for name in ("CF_EDGE_URL", "CF_INGEST_TOKEN", "CF_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    edge_reads._BACKOFF.clear()
    return tmp_path


def test_authenticated_read_and_no_redirect_fallback(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch)
    def fake(request, timeout):
        assert request.full_url.endswith("/internal/v1/issue-context")
        assert request.get_header("X-ingest-token") == "fixture"
        assert json.loads(request.data)["number"] == 1
        return {"complete": True}
    monkeypatch.setattr(edge_reads, "_read_json", fake)
    assert edge_reads.edge_read(tmp_path, "issue-context", {"number": 1}) == {"complete": True}
    monkeypatch.setenv("CF_EDGE_URL", "https://untrusted.example")
    assert edge_reads.edge_read(tmp_path, "issue-context", {"number": 1}) is None


def test_rate_limit_does_not_fallback(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch)
    def limited(*args):
        raise urllib.error.HTTPError("https://worker", 429, "limited", {"Retry-After":"90"}, None)
    monkeypatch.setattr(edge_reads, "_read_json", limited)
    with pytest.raises(edge_reads.EdgeRateLimited) as err:
        edge_reads.edge_read(tmp_path, "pr-snapshot", {"cutoff":None})
    assert err.value.seconds == 90


def test_pr_feed_consumes_complete_worker_snapshot(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch)
    row = {"repository":{"nameWithOwner":"a/b"}, "number":1, "state":"OPEN", "title":"Fix",
           "url":"https://github.com/a/b/pull/1","createdAt":"2026-11-24T00:00:00Z",
           "updatedAt":"2026-11-24T00:00:00Z","closedAt":None}
    monkeypatch.setattr(edge_reads, "_read_json", lambda *_: {
        "totalCount":1,"nodes":[row],"pageInfo":{"hasNextPage":False}})
    def local(*args):
        raise AssertionError("should not use local GH pagination")
    monkeypatch.setattr(pr_feed, "_read_page", local)
    feed = pr_feed.PullRequestFeed(root=tmp_path)
    assert feed.snapshot(full=True)[0]["repo"] == "a/b"
    monkeypatch.setattr(edge_reads, "_read_json", lambda *_: {
        "totalCount":2,"nodes":[row],"pageInfo":{"hasNextPage":False}})
    with pytest.raises(RuntimeError, match="incomplete"):
        feed.snapshot(full=True)
    assert len(feed.rows) == 1


def test_policy_uses_edge_without_local_git_subprocess(tmp_path, monkeypatch):
    from scout_v2 import screening_evidence as evidence
    config(tmp_path, monkeypatch)
    monkeypatch.setenv("OSSCOUT_DASHBOARD_ROOT", str(tmp_path))
    evidence._CACHE.clear()
    document = {"kind":"contributing","repository":"a/b","path":"CONTRIBUTING.md",
                "sha":"a"*40,"content":"Keep fixes focused"}
    monkeypatch.setattr(edge_reads, "_read_json", lambda *_: {
        "complete":True,"documents":[document],"confirmed_absent":["security"]})
    def local(*args, **kwargs):
        raise AssertionError("local GitHub subprocess was called")
    monkeypatch.setattr(evidence, "_get", local)
    try:
        assert "Keep fixes focused" in evidence.render_repository_policy("a/b")
    finally:
        evidence._CACHE.clear()


def test_competition_uses_edge_but_preserves_python_gate(tmp_path, monkeypatch):
    from scout_v2 import git_host
    config(tmp_path, monkeypatch)
    issue = {"number":1,"title":"Parser returns wrong value","body":"","state":"open","user":{"login":"reporter"}}
    def read(request, timeout):
        payload = json.loads(request.data)
        return {"complete":True,"results":[{"term":term,"items":[
            {"number":2,"title":"Fix parser","body":"Fixes #1","state":"open","user":{"login":"other"}}
        ]} for term in payload["terms"]]}
    monkeypatch.setattr(edge_reads, "_read_json", read)
    monkeypatch.setattr(git_host, "gh_read", lambda *_: pytest.fail("local GitHub subprocess called"))
    result = git_host.recheck_candidate("a/b", 1, screening_source=(issue, [], []),
                                       use_title_keywords=True, edge_root=tmp_path)
    assert result.complete and result.open_pr_hits


def test_feishu_token_only_sent_to_fixed_edge_index_route(tmp_path, monkeypatch):
    config(tmp_path, monkeypatch)
    def fake(request, timeout):
        assert request.full_url.endswith("/internal/v1/feishu-record-index")
        assert request.get_header("X-feishu-tenant-token") == "temporary"
        assert "temporary" not in request.data.decode()
        return {"complete":True,"rows":[]}
    monkeypatch.setattr(edge_reads, "_read_json", fake)
    assert edge_reads.edge_read(tmp_path, "feishu-record-index", {}, feishu_token="temporary")["complete"]
    with pytest.raises(ValueError):
        edge_reads.edge_read(tmp_path, "issue-context", {}, feishu_token="temporary")
