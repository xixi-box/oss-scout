"""Competing admission, independent execution and recovery of multiple writers."""
import datetime as dt
import json
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from scout_v2 import state_store as state
from scout_v2.state_store import ClaimLedger, candidate_execution_lock
from scout_v2.workers import auto_pipeline as pipeline

TZ = "Asia/Shanghai"


def entry(n):
    return {"id": f"a/b#{n}", "repo": "a/b", "number": n, "status": "staged",
            "discovered_at": state.business_now().isoformat(), "eligible_at": "2000-01-01T00:00:00+08:00",
            "ai_verdict": {"usable": True}}


@pytest.mark.parametrize("remaining,limit", [(0,0),(1,1),(2,1),(3,1),(4,1),(5,1),(10,1),(20,1),(30,1)])
def test_daily_quota_never_increases_serial_execution_limit(tmp_path, remaining, limit):
    ledger = ClaimLedger(tmp_path / "quota.sqlite3")
    day = state.business_now().date().isoformat()
    for n in range(30 - remaining):
        assert ledger.reserve_publication(TZ, 30, f"done-{n}", "a/b", f"head-{n}", "x/y")
        ledger.finish_publication(f"done-{n}", day=day, url=f"https://github.com/a/b/pull/{n+1}")
    capacity = ledger.dispatch_capacity(TZ, 30)
    assert (capacity["remaining"], capacity["limit"], capacity["available"]) == (remaining, limit, limit)
    ledger.close()


def _competing_reservation(arguments):
    db, n, candidate = arguments
    ledger = ClaimLedger(db)
    try:
        return ledger.reserve_dispatch(TZ, 10, f"job-{n}", "a/b", candidate)
    finally:
        ledger.close()


def test_competing_preparations_reserve_only_one_execution_slot(tmp_path):
    db = tmp_path / "quota.sqlite3"
    with ThreadPoolExecutor(max_workers=16) as workers:
        winners = list(workers.map(_competing_reservation, [(db,n,f"a/b#{n}") for n in range(16)]))
    assert sum(winners) == 1
    ledger = ClaimLedger(db)
    assert ledger.dispatch_capacity(TZ, 10)["active"] == 1
    assert ledger.dispatch_capacity(TZ, 10)["available"] == 0
    assert ledger.dispatch_capacity(TZ, 10)["limit"] == 1
    ledger.close()


def test_same_candidate_case_variants_cannot_both_be_reserved(tmp_path):
    db = tmp_path / "quota.sqlite3"
    with ThreadPoolExecutor(max_workers=2) as workers:
        winners = list(workers.map(_competing_reservation, [(db,1,"A/B#1"),(db,2,"a/b#1")]))
    assert sum(winners) == 1


def test_existing_parallel_writers_drain_before_serial_dispatch_resumes(tmp_path):
    ledger = ClaimLedger(tmp_path / "quota.sqlite3")
    ledger.dispatch_capacity(TZ,30)
    # These writers were admitted by the previous parallel version.
    for n in range(3):
        ledger._conn.execute("INSERT INTO publications(job_id,repo,head,runner_repo,candidate_id,phase) VALUES (?,?,?,?,?,'dispatching')",
            (f"job-{n}","a/b",f"owner:fix/issue-{n}-abcdef0-12345678","x/y",f"a/b#{n}"))
    assert ledger.dispatch_capacity(TZ,30)['active']==3
    for n in range(3):
        assert not ledger.reserve_dispatch(TZ,30,'replacement','a/b','a/b#99')
        assert ledger.dispatch_capacity(TZ,30)['available']==0
        ledger.finish_publication(f'job-{n}')
    assert ledger.reserve_dispatch(TZ,30,'replacement','a/b','a/b#99')
    assert ledger.dispatch_capacity(TZ,30)['active']==1
    assert ledger.dispatch_capacity(TZ,1)['available']==0
    ledger.release_preparation('replacement')
    assert ledger.dispatch_capacity(TZ,30)['available']==1
    ledger.close()


def test_old_pending_writer_is_preserved_and_counted_after_schema_migration(tmp_path):
    import sqlite3
    db = tmp_path / "quota.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE publications(job_id TEXT PRIMARY KEY,repo TEXT NOT NULL,head TEXT NOT NULL,runner_repo TEXT NOT NULL,remote_id TEXT,expected_url TEXT,day TEXT,url TEXT UNIQUE)")
        conn.execute("INSERT INTO publications(job_id,repo,head,runner_repo,remote_id) VALUES ('old','a/b','owner:fix/issue-1-abcdef0-12345678','x/y','42')")
    ledger = ClaimLedger(db)
    assert ledger.pending_publication()[4] == "42"
    assert ledger.reserved_candidates() == ["a/b#1"]
    assert not ledger.reserve_dispatch(TZ,20,"duplicate","a/b","a/b#1")
    assert not ledger.reserve_dispatch(TZ,5,"other","a/b","a/b#2")
    assert ledger.pending_publication()[0] == "old"
    assert ledger.dispatch_capacity(TZ,5)["active"] == 1
    ledger.close()


def test_second_pipeline_waits_until_the_first_releases_its_execution_slot(tmp_path, monkeypatch):
    (tmp_path / "queue").mkdir()
    queue = tmp_path / "queue/candidates.json"
    queue.write_text(json.dumps({f"a/b#{n}":entry(n) for n in range(1,4)}))
    policy = SimpleNamespace(tz_name=TZ,max_claims_per_day=5)
    monkeypatch.setattr(pipeline.PolicyEngine,"load",lambda _: (policy,None))
    release = Event()
    entered = [Event(),Event()]
    seen = []
    def run(root, value, **kwargs):
        seen.append(value["id"])
        entered[value["number"]-1].set()
        assert release.wait(10)
        return {"id":value["id"],"completed":True}
    monkeypatch.setattr(pipeline,"_candidate_pipeline",run)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(pipeline.candidate_pipeline,tmp_path,entry(1))
        assert entered[0].wait(5)
        second = workers.submit(pipeline.candidate_pipeline,tmp_path,entry(2))
        try:
            assert second.result(timeout=5)["abort"] == "dispatcher busy"
            assert not entered[1].is_set()
            assert pipeline.candidate_pipeline(tmp_path,entry(1))["abort"] == "dispatcher busy"
            assert pipeline.candidate_pipeline(tmp_path,entry(3))["abort"] == "dispatcher busy"
            ledger=ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
            assert ledger.dispatch_capacity(TZ,5)["active"] == 1
            ledger.close()
        finally:
            release.set()
        assert first.result()["completed"]
    assert pipeline.candidate_pipeline(tmp_path,entry(2))["completed"]
    assert sorted(seen) == ["a/b#1","a/b#2"]
    ledger=ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    assert ledger.dispatch_capacity(TZ,5)["active"] == 0
    ledger.close()


def test_recovery_checks_all_writers_and_skips_live_owner(tmp_path,monkeypatch):
    (tmp_path/"queue").mkdir()
    (tmp_path/"queue/candidates.json").write_text(json.dumps({f"a/b#{n}":entry(n) for n in (1,2,3)}))
    ledger=ClaimLedger(tmp_path/"state/scout_v2.sqlite3")
    for n in (1,2,3):
        ledger.dispatch_capacity(TZ,10)
        ledger._conn.execute("INSERT INTO publications(job_id,repo,head,runner_repo,candidate_id) VALUES (?,?,?,?,?)",
            (f"job-{n}","a/b",f"owner:fix/issue-{n}-abcdef0-12345678","x/y",f"a/b#{n}"))
        ledger.set_publication_run(f"job-{n}",str(n))
    reads=[]
    def github(args,**kw):
        reads.append(args[0])
        if "/actions/runs/" in args[0]:
            n=args[0].rsplit("/",1)[1]
            return {"display_title":f"repair job-{n}","status":"in_progress" if n=="3" else "completed","conclusion":"failure"}
        return []
    monkeypatch.setattr(pipeline,"gh_read",github)
    monkeypatch.setattr(pipeline.ActionsBackend,"collect",lambda self,r: {"job_id":f"job-{r}","outcome":"failed"})
    with candidate_execution_lock(tmp_path,"a/b#1") as owned:
        assert owned
        assert pipeline.recover_pending_publication(tmp_path)
    assert ledger.pending_publication("job-1") is not None
    assert ledger.pending_publication("job-2") is None
    assert ledger.pending_publication("job-3") is not None
    assert not any(path.endswith('/actions/runs/1') for path in reads)
    queue=json.loads((tmp_path/"queue/candidates.json").read_text(encoding="utf-8"))
    assert queue["a/b#2"]["status"] == "aborted_no_evidence"
    assert queue["a/b#1"]["status"] == "staged"
    ledger.close()


def test_processes_share_the_same_quota_reservations(tmp_path):
    db=tmp_path/"quota.sqlite3"
    with ProcessPoolExecutor(max_workers=6) as workers:
        results=list(workers.map(_competing_reservation,[(db,n,f"a/b#{n}") for n in range(12)]))
    assert sum(results)==1
    ledger=ClaimLedger(db)
    assert ledger.dispatch_capacity(TZ,10)["available"]==0
    ledger.close()


def test_controller_only_launches_one_repair_even_with_large_daily_quota(tmp_path,monkeypatch):
    import inspect
    import threading
    monkeypatch.setenv("OSSCOUT_DISABLE_AUTO_REPAIR","1")
    from dashboard.app import create_app
    (tmp_path/"queue").mkdir()
    (tmp_path/"configs").mkdir()
    (tmp_path/"configs/console_models.yaml").write_text("daily_pr_quota: 20\ncandidate_pool_size: 20\n")
    q={f"a/b#{n}":entry(n) for n in range(20)}
    (tmp_path/"queue/candidates.json").write_text(json.dumps(q))
    eng=SimpleNamespace(tz_name=TZ,max_claims_per_day=20,repo_lane=lambda _:"open")
    monkeypatch.setattr(pipeline.PolicyEngine,"load",lambda _: (eng,None))
    monkeypatch.setattr(pipeline,"monitored_repo",lambda _:True)
    launches=[]
    class Thread:
        def __init__(self,*,target,args,**kw):self.value=args[0]
        def start(self):launches.append(self.value["id"])
    monkeypatch.setattr(threading,"Thread",Thread)
    app=create_app(tmp_path,auth_enabled=False,password="test")
    monkeypatch.delenv("OSSCOUT_DISABLE_AUTO_REPAIR")
    endpoint=next(r.endpoint for r in app.routes if r.path=="/api/pipeline/run")
    dispatch=inspect.getclosurevars(endpoint).nonlocals["_dispatch"]
    assert len(dispatch(rescreen=True))==1
    assert len(set(launches))==1
    assert dispatch(rescreen=True)==[]


def test_pr_table_refreshes_every_five_minutes_and_other_tables_keep_fifteen(tmp_path,monkeypatch):
    import threading
    monkeypatch.setenv("OSSCOUT_DISABLE_AUTO_REPAIR","1")
    import dashboard.app as module
    (tmp_path/"queue").mkdir()
    (tmp_path/"configs").mkdir()
    (tmp_path/"queue/candidates.json").write_text("{}")
    (tmp_path/"AGENTS.md").write_text("Test policy")
    (tmp_path/"configs/repositories.yaml").write_text("repositories: {}")
    clock=[0.0]
    calls=[]
    loops={}
    class Stop(BaseException):pass
    class Wake:
        def clear(self):pass
        def set(self):pass
        def wait(self,seconds):
            clock[0]+=seconds
            if clock[0]>=930:raise Stop()
    class Thread:
        def __init__(self,*,target,name=None,**kw):self.target=target;self.name=name
        def start(self):loops[self.name]=self.target
    monkeypatch.setattr(threading,"Thread",Thread)
    monkeypatch.setattr(threading,"Event",Wake)
    monkeypatch.setattr(module.time,"sleep",lambda _:None)
    monkeypatch.setattr(module.time,"monotonic",lambda:clock[0])
    monkeypatch.setattr(module,"_sync_bitables",lambda root,**kw:calls.append((clock[0],kw["table_names"],kw["activity_only"])))
    monkeypatch.delenv("OSSCOUT_DISABLE_AUTO_REPAIR")
    module.create_app(tmp_path,auth_enabled=False,password="test")
    with pytest.raises(Stop):loops["bitable-sync"]()
    schedule={moment:(names,activity) for moment,names,activity in calls}
    assert schedule[0]==(None,False)
    assert schedule[300]==({"pr","scan","candidates"},False)
    assert schedule[600]==({"pr","scan","candidates"},False)
    assert schedule[900]==(None,False)
    assert schedule[30]==({"scan","candidates"},True)


def test_actions_lookup_paginates_without_confusing_other_jobs(tmp_path):
    from scout_v2.adapters.actions_backend import ActionsBackend
    calls=[]
    def github(args):
        calls.append(args)
        page=next(arg for arg in args if arg.startswith('page='))
        runs=[{"id":n,"display_title":f"repair other-{n}","event":"workflow_dispatch"} for n in range(100)] if page=='page=1' else [{"id":4321,"display_title":"repair wanted","event":"workflow_dispatch"}]
        return 0,json.dumps({"workflow_runs":runs})
    backend=ActionsBackend(enabled=True,runner_repo="x/y",workflow="oss-scout-repair.yml",gh_runner=github)
    assert backend._find_run("wanted",attempts=1)==4321
    assert len(calls)==2


def test_recovery_lookup_finds_job_beyond_first_hundred(monkeypatch):
    pages=[]
    def github(args):
        pages.append(args[0])
        runs=[{"id":n,"display_title":f"repair other-{n}"} for n in range(100)] if args[0].endswith('page=1') else [{"id":999,"display_title":"repair wanted"}]
        return {"workflow_runs":runs}
    monkeypatch.setattr(pipeline,"gh_read",github)
    run,complete=pipeline._find_repair_run("x/y","wanted")
    assert complete and run["id"]==999
    assert len(pages)==2


def test_auth_refresh_does_not_block_intake_or_health(tmp_path,monkeypatch):
    import asyncio
    import httpx
    monkeypatch.setenv("OSSCOUT_DISABLE_AUTO_REPAIR","1")
    monkeypatch.setenv("CF_INGEST_TOKEN","test-worker-token")
    from dashboard.app import create_app
    from scout_v2 import codex_auth
    entered,release=Event(),Event()
    def exchange(root,body):
        entered.set()
        assert release.wait(5)
        return {"ok":True},200
    monkeypatch.setattr(codex_auth,"exchange",exchange)
    app=create_app(tmp_path,auth_enabled=False,password="test")
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            request=asyncio.create_task(client.post("/api/ingest/codex-auth",headers={"X-Ingest-Token":"test-worker-token"},json={"operation":"refresh"}))
            try:
                assert await asyncio.to_thread(entered.wait,3)
                assert not request.done()
                health=await asyncio.wait_for(client.get("/healthz"),timeout=1)
                assert health.json()=={"ok":True}
                assert not request.done()
            finally:
                release.set()
            assert (await request).status_code==200
    asyncio.run(scenario())


def test_many_credential_waiters_leave_health_threadpool_available(tmp_path,monkeypatch):
    import asyncio
    import httpx
    monkeypatch.setenv("OSSCOUT_DISABLE_AUTO_REPAIR","1")
    monkeypatch.setenv("CF_INGEST_TOKEN","test-worker-token")
    from dashboard.app import create_app
    from scout_v2 import codex_auth
    release=Event()
    app=create_app(tmp_path,auth_enabled=False,password="test")
    async def scenario():
        loop=asyncio.get_running_loop()
        entered=asyncio.Event()
        def exchange(root,body):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
            return {"ok":True},200
        monkeypatch.setattr(codex_auth,"exchange",exchange)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test") as client:
            requests=[asyncio.create_task(client.post("/api/ingest/codex-auth",headers={"X-Ingest-Token":"test-worker-token"},json={"operation":"refresh"})) for _ in range(45)]
            try:
                await asyncio.wait_for(entered.wait(),timeout=2)
                await asyncio.sleep(0.05)
                health=await asyncio.wait_for(client.get("/healthz"),timeout=1)
                assert health.json()=={"ok":True}
                assert not any(request.done() for request in requests)
            finally:
                release.set()
                responses=await asyncio.gather(*requests)
            assert all(response.status_code==200 for response in responses)
    asyncio.run(scenario())
