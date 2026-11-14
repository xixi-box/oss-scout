"""A serial rollout drains existing writers and releases only abandoned handoffs."""
import ast
import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import yaml

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
        assert command==['docker','compose','-f','compose.yaml','up','-d','--remove-orphans']
        events.append('replaced')
    exec(compile(tree,'deployment-drain','exec'),{
        'Path':FakePath,'sqlite3':SimpleNamespace(connect=connect,OperationalError=RuntimeError),
        'json':json,'subprocess':SimpleNamespace(run=run),
        'sys':SimpleNamespace(argv=['deploy','compose.yaml']),'time':SimpleNamespace(sleep=wait)})
    assert events==['admission_acquired','existing_job_finished','existing_job_finished','replaced','admission_released']


def test_restart_clears_only_unowned_unstarted_handoffs(tmp_path,monkeypatch):
    import threading
    monkeypatch.setenv('OSSCOUT_DISABLE_AUTO_REPAIR','1')
    from dashboard import app as dashboard
    from scout_v2.workers import auto_pipeline
    (tmp_path/'queue').mkdir()
    queue={f'a/b#{n}':{'id':f'a/b#{n}','repo':'a/b','status':'staged','direct_start_reserved_at':'2026-11-14T12:00:00+08:00'} for n in (1,2,3)}
    (tmp_path/'queue/candidates.json').write_text(json.dumps(queue))
    ledger=ClaimLedger(tmp_path/'state/scout_v2.sqlite3')
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
    assert ledger.preparations()==[('job-live','a/b#2')]
    ledger.close()


def test_runtime_smoke_reports_variable_ai_classification_without_blocking_deploy(tmp_path,monkeypatch,capsys):
    import sys
    from scripts import runtime_status
    from scout_v2.workers.openjev import OpenJevClient
    monkeypatch.setattr(sys,'argv',['runtime_status','--root',str(tmp_path),'--screening'])
    monkeypatch.setattr(runtime_status,'inspect',lambda root:{'repositories':58,'formal_repositories':50,'honor_repositories':8,'daily_cap':30,'automatic_repositories':57})
    results=iter(({'usable':True},{'usable':True,'probability':0.651},{'usable':False,'probability':0.315}))
    monkeypatch.setattr(OpenJevClient,'score_issue',lambda *a:next(results))
    assert runtime_status.main()==0
    report=json.loads(capsys.readouterr().out)
    assert report['intent_screening_warning']
    assert report['intent_screening_probes']['zh']['probability']==0.651


def test_runtime_smoke_still_fails_when_screening_service_unavailable(tmp_path,monkeypatch):
    import sys
    import pytest
    from scripts import runtime_status
    from scout_v2.workers.openjev import OpenJevClient
    monkeypatch.setattr(sys,'argv',['runtime_status','--root',str(tmp_path),'--screening'])
    monkeypatch.setattr(runtime_status,'inspect',lambda root:{'repositories':58,'formal_repositories':50,'honor_repositories':8,'daily_cap':30,'automatic_repositories':57})
    results=iter(({'usable':True},{'usable':None,'reason':'HTTP 503'},{'usable':False}))
    monkeypatch.setattr(OpenJevClient,'score_issue',lambda *a:next(results))
    with pytest.raises(AssertionError,match='service unavailable'):runtime_status.main()
