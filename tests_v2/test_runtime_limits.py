"""Runtime limits affect the real queue and quota ledger without a restart."""
import datetime as dt

import pytest

from scout_v2.console_models import effective_models, model_setting_error, runtime_limit, write_models
from scout_v2.feishu_models import _apply_runtime_limits
from scout_v2.repo_policy import PolicyEngine
from scout_v2.state_store import (ClaimLedger, business_now, queue_transaction,
                                  publication_quota_remaining, rotate_staged_queue)


@pytest.mark.parametrize("key", ["candidate_pool_size", "daily_pr_quota"])
@pytest.mark.parametrize("value", ["0", "-1", "1.5", "", "abc", "01"])
def test_invalid_limits_are_rejected(key, value):
    assert model_setting_error(key, value)


def test_defaults_and_invalid_local_overrides(tmp_path):
    models = effective_models(tmp_path)
    assert models["candidate_pool_size"] == "10"
    assert models["daily_pr_quota"] == "5"
    write_models(tmp_path, {"candidate_pool_size": "0", "daily_pr_quota": "bad"})
    assert runtime_limit(tmp_path, "candidate_pool_size", 10) == 10
    assert runtime_limit(tmp_path, "daily_pr_quota", 5) == 5


def test_live_quota_uses_existing_publications_and_retains_pending_work(tmp_path):
    engine = PolicyEngine(tmp_path, {}, {"defaults": {"max_new_claims_per_business_day": 5}}, {}, "rules")
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    try:
        assert ledger.reserve_publication(engine.tz_name, engine.max_claims_per_day, "first", "a/b", "head1", "x/y")
        ledger.finish_publication("first", day=business_now().date().isoformat(), url="https://github.com/a/b/pull/1")
        write_models(tmp_path, {"daily_pr_quota": "1"})
        assert engine.max_claims_per_day == 1
        assert publication_quota_remaining(tmp_path, engine.tz_name, engine.max_claims_per_day) == 0
        assert not ledger.reserve_publication(engine.tz_name, engine.max_claims_per_day, "second", "a/b", "head2", "x/y")
        write_models(tmp_path, {"daily_pr_quota": "2"})
        assert ledger.reserve_publication(engine.tz_name, engine.max_claims_per_day, "second", "a/b", "head2", "x/y")
        write_models(tmp_path, {"daily_pr_quota": "1"})
        assert ledger.pending_publication()[0] == "second"
        assert publication_quota_remaining(tmp_path, engine.tz_name, engine.max_claims_per_day) == 0
    finally:
        ledger.close()


def test_shrinking_pool_keeps_newest_and_protects_active_work(tmp_path):
    now = business_now()
    with queue_transaction(tmp_path) as queue:
        for number in range(1, 5):
            queue[str(number)] = {"status": "staged", "staged_at": (now + dt.timedelta(seconds=number)).isoformat()}
        queue["running"] = {"status": "running"}
        queue["reserved"] = {"status": "staged", "direct_start_reserved_at": now.isoformat()}
        write_models(tmp_path, {"candidate_pool_size": "2"})
        assert rotate_staged_queue(tmp_path, queue) == ["1", "2"]
        assert queue["1"]["abort_reason"] == "暂存满 2 条，按入队时间清除最早候选"
        assert queue["running"] == {"status": "running"}
        assert queue["reserved"]["direct_start_reserved_at"] == now.isoformat()
        write_models(tmp_path, {"candidate_pool_size": "6"})
        assert rotate_staged_queue(tmp_path, queue) == []
        assert queue["1"]["status"] == "queue_evicted"


def test_limit_application_trims_and_releases_only_todays_quota_waiters(tmp_path):
    now = business_now()
    with queue_transaction(tmp_path) as queue:
        for name, day in (("old", now - dt.timedelta(days=1)), ("today", now)):
            queue[name] = {"status": "staged", "discovered_at": day.isoformat(),
                           "staged_at": day.isoformat(), "defer_reason": "daily_quota",
                           "eligible_at": (now + dt.timedelta(days=1)).isoformat(),
                           "screen_retry_at": (now + dt.timedelta(minutes=5)).isoformat()}
    write_models(tmp_path, {"candidate_pool_size": "10", "daily_pr_quota": "6"})
    _apply_runtime_limits(tmp_path, {"daily_pr_quota": "5"}, effective_models(tmp_path))
    with queue_transaction(tmp_path) as queue:
        assert queue["today"]["defer_reason"] == "execution_wait"
        assert queue["today"]["eligible_at"] <= business_now().isoformat()
        assert queue["today"]["screen_retry_at"] > business_now().isoformat()
        assert queue["old"]["defer_reason"] == "daily_quota"
        assert queue["old"]["eligible_at"] > business_now().isoformat()
    write_models(tmp_path, {"candidate_pool_size": "1", "daily_pr_quota": "6"})
    _apply_runtime_limits(tmp_path, {"daily_pr_quota": "6"}, effective_models(tmp_path))
    with queue_transaction(tmp_path) as queue:
        assert queue["old"]["status"] == "queue_evicted"
        assert queue["today"]["status"] == "staged"
