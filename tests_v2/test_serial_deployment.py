"""A serial rollout drains existing writers and releases only abandoned handoffs."""
import ast
import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import yaml
import pytest

from scout_v2.state_store import ClaimLedger,candidate_execution_lock


def test_deploy_holds_admission_while_existing_writers_finish():
    workflow=yaml.safe_load((Path(__file__).parents[1]/'.github/workflows/deploy.yml').read_text(encoding='utf-8'))
    source=next(step['run'] for step in workflow['jobs']['deploy']['steps'] if step.get('name')=='Deploy to self-hosted runner')
    script=source.split("python3 - ",1)[1].split("\n",1)[1].split("\nPY",1)[0]
    tree=ast.parse(script)
    tree.body=[node for node in tree.body if not isinstance(node,(ast.Import,ast.ImportFrom))]
    events=[]
    pending=iter((2,1,0))
    class Admission:
        in_transaction=False
        def execute(self,sql):
            assert not self.in_transaction
            self.in_transaction=True
            events.append('admission_acquired')
        def close(self):
            events.append('admission_released')
            self.in_transaction=False
    lock=Admission()
    class Snapshot:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def execute(self,sql):return self
        def fetchone(self):return (next(pending),)
    def connect(path,**kwargs):
        return Snapshot() if kwargs.get('uri') else lock
    class FakePath:
        def __init__(self,*args):pass
        def __truediv__(self,other):return FakePath()
        def __str__(self):return 'dispatcher.sqlite3'
        def read_text(self):return '{}'
    def wait(seconds):
        assert lock.in_transaction
        assert 'replaced' not in events
        events.append('existing_job_finished')
    def run(command,check):
        assert lock.in_transaction and events.count('existing_job_finished')==2
        assert command==['docker','compose','-f','compose.yaml','up','-d','--no-build','--remove-orphans']
        events.append('replaced')
    exec(compile(tree,'deployment-drain','exec'),{
        'Path':FakePath,'sqlite3':SimpleNamespace(connect=connect,OperationalError=RuntimeError),
        'json':json,'subprocess':SimpleNamespace(run=run),
        'sys':SimpleNamespace(argv=['deploy','compose.yaml','/data/oss-scout-data']),'time':SimpleNamespace(sleep=wait)})
    assert events==['admission_acquired','existing_job_finished','existing_job_finished','replaced','admission_released']


def test_restart_clears_only_unowned_unstarted_handoffs(tmp_path,monkeypatch):
    import threading
    monkeypatch.setenv('OSSCOUT_DISABLE_AUTO_REPAIR','1')
    from dashboard import app as dashboard
    from scout_v2.workers import auto_pipeline
    (tmp_path/'queue').mkdir()
    queue={f'a/b#{n}':{'id':f'a/b#{n}','repo':'a/b','status':'staged','direct_start_reserved_at':'2026-11-14T12:00:00+08:00'} for n in (1,2,3,4)}
    queue['a/b#4']['dispatch_job_id']='job-unconfirmed-handoff'
    (tmp_path/'queue/candidates.json').write_text(json.dumps(queue))
    ledger=ClaimLedger(tmp_path/'state/scout_v2.sqlite3')
    assert ledger.try_claim('Asia/Shanghai','a/b#4')[0]
    assert ledger.reserve_dispatch('Asia/Shanghai',30,'job-live','a/b','a/b#2')
    monkeypatch.setattr(auto_pipeline.PolicyEngine,'load',lambda _: (SimpleNamespace(repo_entry=lambda _:{},tz_name='Asia/Shanghai',max_claims_per_day=30),None))
    monkeypatch.setattr(dashboard,'modifiable_repo',lambda _:True)
    monkeypatch.setattr(threading.Thread,'start',lambda _:None)
    monkeypatch.delenv('OSSCOUT_DISABLE_AUTO_REPAIR')
    with ExitStack() as stack:
        assert stack.enter_context(candidate_execution_lock(tmp_path,'a/b#2'))
        assert stack.enter_context(candidate_execution_lock(tmp_path,'a/b#3'))
        dashboard.create_app(tmp_path,auth_enabled=False,password='test')
    result=json.loads((tmp_path/'queue/candidates.json').read_text(encoding='utf-8'))
    assert not result['a/b#1'].get('direct_start_reserved_at')
    assert result['a/b#2']['direct_start_reserved_at']
    assert result['a/b#3']['direct_start_reserved_at']
    assert result['a/b#4']['status']=='staged'
    assert result['a/b#4']['dispatch_job_id']=='job-unconfirmed-handoff'
    assert result['a/b#4']['direct_start_reserved_at']
    assert ledger.preparations()==[('job-live','a/b#2')]
    ledger.close()


@pytest.mark.parametrize("state", ["unstarted", "running", "started", "other-owner", "write-failed"])
def test_restart_preparation_cleanup_is_owned_and_durable_before_ledger_release(tmp_path, monkeypatch, state):
    import threading
    from dashboard import app as dashboard
    from scout_v2 import state_store
    from scout_v2.workers import auto_pipeline
    (tmp_path / "queue").mkdir()
    key, job = "a/b#1", "job-preparing"
    entry = {"id": key, "repo": "a/b", "status": "running" if state == "running" else "staged",
             "dispatch_job_id": "job-other" if state == "other-owner" else job,
             "direct_start_reserved_at": "2026-11-18T16:58:00+08:00"}
    if state == "started":
        entry["dispatch_started_at"] = "2026-11-18T16:59:00+08:00"
    path = tmp_path / "queue/candidates.json"
    path.write_text(json.dumps({key: entry}))
    ledger = ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    assert ledger.reserve_dispatch("Asia/Shanghai", 30, job, "a/b", key)
    monkeypatch.setattr(auto_pipeline.PolicyEngine, "load", lambda _: (
        SimpleNamespace(repo_entry=lambda _: {}, tz_name="Asia/Shanghai", max_claims_per_day=30), None))
    monkeypatch.setattr(dashboard, "modifiable_repo", lambda _: True)
    monkeypatch.setattr(threading.Thread, "start", lambda _: None)
    monkeypatch.delenv("OSSCOUT_DISABLE_AUTO_REPAIR", raising=False)
    release = ClaimLedger.release_preparation
    released = []
    def checked_release(instance, job_id):
        current = json.loads(path.read_text())[key]
        assert not current.get("dispatch_job_id") and not current.get("direct_start_reserved_at")
        released.append(job_id)
        release(instance, job_id)
    monkeypatch.setattr(ClaimLedger, "release_preparation", checked_release)
    if state == "write-failed":
        persist = state_store.atomic_json_write
        def fail_queue(destination, value, **kwargs):
            if destination == path:
                raise OSError("queue persistence failed")
            return persist(destination, value, **kwargs)
        monkeypatch.setattr(state_store, "atomic_json_write", fail_queue)
    try:
        if state == "write-failed":
            with pytest.raises(OSError, match="queue persistence failed"):
                dashboard.create_app(tmp_path, auth_enabled=False, password="test")
        else:
            dashboard.create_app(tmp_path, auth_enabled=False, password="test")
        if state == "unstarted":
            assert released == [job] and ledger.preparations() == []
        else:
            assert not released and ledger.preparations() == [(job, key)]
            current = json.loads(path.read_text())[key]
            assert current["dispatch_job_id"] == entry["dispatch_job_id"]
            assert current["direct_start_reserved_at"] == entry["direct_start_reserved_at"]
    finally:
        ledger.close()


def test_runtime_smoke_reports_variable_ai_classification_without_blocking_deploy(tmp_path,monkeypatch,capsys):
    import sys
    from scripts import runtime_status
    from scout_v2.workers.openjev import OpenJevClient
    monkeypatch.setattr(sys,'argv',['runtime_status','--root',str(tmp_path),'--screening'])
    monkeypatch.setattr(runtime_status,'inspect',lambda root:{'repositories':65,'formal_repositories':50,'honor_repositories':15,'daily_cap':30,'automatic_repositories':57})
    results=iter(({'usable':True},{'usable':True,'probability':0.651},{'usable':False,'probability':0.315},
                  {'usable':False,'blocking_gate':'private_disclosure'},
                  {'usable':False,'blocking_gate':'unmet_prerequisite'}, {'usable':True}))
    monkeypatch.setattr(OpenJevClient,'score_issue',lambda *a:next(results))
    assert runtime_status.main()==0
    report=json.loads(capsys.readouterr().out)
    assert report['intent_screening_warning']
    assert report['intent_screening_probes']['zh']['probability']==0.651
    assert report['policy_screening_probes']['prerequisite_open']['blocking_gate']=='unmet_prerequisite'
    assert 'policy_screening_warning' not in report


def test_runtime_smoke_still_fails_when_screening_service_unavailable(tmp_path,monkeypatch):
    import sys
    import pytest
    from scripts import runtime_status
    from scout_v2.workers.openjev import OpenJevClient
    monkeypatch.setattr(sys,'argv',['runtime_status','--root',str(tmp_path),'--screening'])
    monkeypatch.setattr(runtime_status,'inspect',lambda root:{'repositories':65,'formal_repositories':50,'honor_repositories':15,'daily_cap':30,'automatic_repositories':57})
    results=iter(({'usable':True},{'usable':None,'reason':'HTTP 503'},{'usable':False},
                  {'usable':False,'blocking_gate':'private_disclosure'},
                  {'usable':False,'blocking_gate':'unmet_prerequisite'}, {'usable':True}))
    monkeypatch.setattr(OpenJevClient,'score_issue',lambda *a:next(results))
    with pytest.raises(AssertionError,match='service unavailable'):runtime_status.main()
