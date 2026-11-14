import datetime as dt
import json
from types import SimpleNamespace

import pytest

from scout_v2.state_store import ClaimLedger, business_now
from scout_v2.workers import auto_pipeline as pipeline
from scout_v2.workers.codex_client import CODEX_COMMUNICATION_RETRY_PREFIX

TZ = "Asia/Shanghai"
FAILURE = CODEX_COMMUNICATION_RETRY_PREFIX + " operation=sync; lease renewal deadline exceeded"


def writer(tmp_path):
    (tmp_path / "queue").mkdir()
    key="traefik/traefik#1"
    entry={"id":key,"repo":"traefik/traefik","number":1,"status":"running",
           "discovered_at":business_now().isoformat(),"dispatch_job_id":"job-one",
           "dispatch_started_at":business_now().isoformat(),"ai_verdict":{"usable":True}}
    (tmp_path / "queue/candidates.json").write_text(json.dumps({key:entry}))
    ledger=ClaimLedger(tmp_path / "state/scout_v2.sqlite3")
    assert ledger.try_claim(TZ,key)[0]
    assert ledger.reserve_publication(TZ,5,"job-one","traefik/traefik","xixi-box:fix/issue-1-abcdef0-12345678","xixi-box/oss-scout")
    ledger.set_publication_run("job-one","one")
    return key,ledger


@pytest.mark.parametrize("recovery",[False,True])
@pytest.mark.parametrize("failure",[FAILURE,"codex repair run failed: Codex start/result error: TimeoutError","codex repair run failed: Codex login broker returned 503"])
def test_completed_unpublished_communication_failure_is_staged_with_retry_authorization(tmp_path,monkeypatch,recovery,failure):
    key,ledger=writer(tmp_path)
    result={"job_id":"job-one","outcome":"failed","unverified_items":[failure]}
    monkeypatch.setattr(pipeline,"gh_read",lambda args:
        {"display_title":"repair job-one","status":"completed","conclusion":"success"}
        if '/actions/runs/' in args[0] else [])
    monkeypatch.setattr(pipeline.ActionsBackend,"collect",lambda *args:result)
    assert pipeline._reconcile_publication(ledger,TZ,root=tmp_path,
        worker_result=None if recovery else result,job_id="job-one")=={}
    saved=json.loads((tmp_path / "queue/candidates.json").read_text(encoding="utf-8"))[key]
    assert saved['status']=='staged' and saved['repair_outcome']=='communication_unavailable'
    assert saved['retry_authorized_job_id']==saved['repair_job_id']=='job-one'
    assert dt.datetime.fromisoformat(saved['screen_retry_at'])>business_now()
    assert saved['communication_failures']==1
    assert not saved.get('dispatch_job_id') and not saved.get('dispatch_started_at')
    assert ledger.pending_publication() is None and ledger.dispatch_capacity(TZ,5)['active']==0
    assert not ledger.try_claim(TZ,key)[0]  # Default same-day dedup remains intact.
    assert ledger.try_claim(TZ,key,retry_failed=True)[0]
    assert not ledger.try_claim(TZ,key,retry_failed=True)[0]  # A live claim never reopens.
    assert pipeline._communication_retry_fields(saved,'job-one','again',TZ)=={}
    ledger.close()


def test_unfinished_remote_writer_is_never_requeued_on_communication_details(tmp_path,monkeypatch):
    key,ledger=writer(tmp_path)
    monkeypatch.setattr(pipeline,'gh_read',lambda args:
        {'display_title':'repair job-one','status':'in_progress'})
    assert pipeline._reconcile_publication(ledger,TZ,root=tmp_path,job_id='job-one',
        worker_result={'job_id':'job-one','outcome':'failed','unverified_items':[FAILURE]}) is None
    assert ledger.pending_publication() is not None
    assert json.loads((tmp_path/'queue/candidates.json').read_text())[key]['status']=='running'
    assert not ledger.try_claim(TZ,key,retry_failed=True)[0]
    ledger.close()


def test_existing_published_pr_wins_over_a_communication_failure(tmp_path,monkeypatch):
    key,ledger=writer(tmp_path)
    pr={'html_url':'https://github.com/traefik/traefik/pull/2','user':{'login':'xixi-box'},
        'body':'<!-- oss-scout-job:job-one -->','created_at':business_now().isoformat()}
    monkeypatch.setattr(pipeline,'gh_read',lambda args:
        {'display_title':'repair job-one','status':'completed'}
        if '/actions/runs/' in args[0] else [pr])
    assert pipeline._reconcile_publication(ledger,TZ,root=tmp_path,job_id='job-one',
        worker_result={'job_id':'job-one','outcome':'failed','unverified_items':[FAILURE]})==pr
    saved=json.loads((tmp_path/'queue/candidates.json').read_text(encoding='utf-8'))[key]
    assert saved['status']=='pr_open' and not saved.get('retry_authorized_job_id')
    assert ledger.quota_used(business_now().date().isoformat())==1
    assert not ledger.try_claim(TZ,key,retry_failed=True)[0]
    ledger.close()


def test_real_pipeline_can_retry_a_confirmed_communication_failure_once(tmp_path,monkeypatch):
    key,ledger=writer(tmp_path)
    ledger.finish_publication('job-one')
    ledger.release_claim(key,TZ)
    ledger.close()
    now=business_now().isoformat()
    entry={'id':key,'repo':'traefik/traefik','number':1,'status':'staged','title':'Parser bug',
           'discovered_at':now,'screened_at':now,'eligible_at':now,'ai_verdict':{'usable':True},
           'repair_job_id':'job-one','retry_authorized_job_id':'job-one'}
    (tmp_path/'queue/candidates.json').write_text(json.dumps({key:entry}))
    eng=SimpleNamespace(repo_lane=lambda _:'open',tz_name=TZ,max_claims_per_day=5,
                        snapshot_id=lambda:'test',repos_cfg={'repositories':{'traefik/traefik':{}}})
    monkeypatch.setattr(pipeline.PolicyEngine,'load',lambda _:(eng,None))
    monkeypatch.setattr(pipeline,'recheck_candidate',lambda *a,**kw: SimpleNamespace(
        complete=True,issue_open=True,unassigned=True,has_claim_signal=False,is_duplicate=False,
        has_cross_ref_pr=False,open_pr_hits=[],screening_source=({'title':'Parser bug','state':'open','labels':[]},[],[])))
    monkeypatch.setattr('scout_v2.workers.cla.preflight',lambda *a:{'blocking':False})
    monkeypatch.setattr('scout_v2.console_models.repair_settings',lambda _:('opencode','test'))
    monkeypatch.setattr(pipeline.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0,stdout='a'*40,stderr=''))
    monkeypatch.setattr(pipeline,'_build_router',lambda *a:SimpleNamespace(route=lambda job:SimpleNamespace(backend='github_actions')))
    submitted=[]
    def submit(root,job,eng):
        submitted.append(job)
        return 'github_actions','two',{}
    monkeypatch.setattr(pipeline,'_submit_job',submit)
    monkeypatch.setattr(pipeline,'gh_read',lambda args: {'state':'active'} if '/workflows/' in args[0] else
        {'display_title':f'repair {submitted[-1].job_id}','status':'completed','conclusion':'success'}
        if '/actions/runs/' in args[0] else [])
    monkeypatch.setattr(pipeline,'_collect',lambda *a:{'result':{'job_id':submitted[-1].job_id,
        'outcome':'failed','unverified_items':[FAILURE]}})
    result=pipeline.candidate_pipeline(tmp_path,entry)
    assert result.get('retry') is True and len(submitted)==1
    current=json.loads((tmp_path/'queue/candidates.json').read_text(encoding='utf-8'))[key]
    assert current['status']=='staged' and current['repair_outcome']=='communication_unavailable'
    assert current['retry_authorized_job_id']==submitted[0].job_id
    assert current['retry_authorized_job_id']!='job-one'
    assert pipeline.candidate_pipeline(tmp_path,current)['abort']=='candidate not currently eligible'
    assert len(submitted)==1  # Backoff does not permit an immediate duplicate retry.


def test_reconciliation_after_interruption_does_not_extend_communication_backoff(tmp_path,monkeypatch):
    key,ledger=writer(tmp_path)
    result={'job_id':'job-one','outcome':'failed','unverified_items':[FAILURE]}
    monkeypatch.setattr(pipeline,'gh_read',lambda args:
        {'display_title':'repair job-one','status':'completed','conclusion':'success'}
        if '/actions/runs/' in args[0] else [])
    with monkeypatch.context() as patch:
        patch.setattr(ledger,'finish_publication',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('simulated controller interruption')))
        with pytest.raises(RuntimeError,match='controller interruption'):
            pipeline._reconcile_publication(ledger,TZ,root=tmp_path,worker_result=result,job_id='job-one')
    before=json.loads((tmp_path/'queue/candidates.json').read_text(encoding='utf-8'))[key]
    assert ledger.pending_publication() is not None
    pipeline._reconcile_publication(ledger,TZ,root=tmp_path,worker_result=result,job_id='job-one')
    after=json.loads((tmp_path/'queue/candidates.json').read_text(encoding='utf-8'))[key]
    assert after['screen_retry_at']==before['screen_retry_at']
    assert after['communication_failures']==before['communication_failures']==1
    assert after['repair_outcome']=='communication_unavailable'
    assert after['retry_authorized_job_id']=='job-one'
    assert ledger.pending_publication() is None
    ledger.close()


@pytest.mark.parametrize('failure',['Codex timed out after 2400s','compile failed','Codex exited with status 1','Codex login broker returned 403'])
def test_code_failures_and_total_runtime_timeout_are_not_reclassified(tmp_path,monkeypatch,failure):
    key,ledger=writer(tmp_path)
    monkeypatch.setattr(pipeline,'gh_read',lambda args:
        {'display_title':'repair job-one','status':'completed','conclusion':'success'}
        if '/actions/runs/' in args[0] else [])
    pipeline._reconcile_publication(ledger,TZ,root=tmp_path,job_id='job-one',
        worker_result={'job_id':'job-one','outcome':'failed','unverified_items':[failure]})
    saved=json.loads((tmp_path/'queue/candidates.json').read_text(encoding='utf-8'))[key]
    assert saved['status']=='aborted_no_evidence' and not saved.get('retry_authorized_job_id')
    assert not ledger.try_claim(TZ,key,retry_failed=True)[0]
    ledger.close()


def test_controller_restart_preserves_confirmed_retry_but_protects_unknown_claims(tmp_path,monkeypatch):
    import threading
    monkeypatch.setenv('OSSCOUT_DISABLE_AUTO_REPAIR','1')
    from dashboard import app as dashboard
    (tmp_path/'queue').mkdir()
    queue={}
    ledger=ClaimLedger(tmp_path/'state/scout_v2.sqlite3')
    for number in (1,2,3,4):
        key=f'traefik/traefik#{number}'
        queue[key]={'id':key,'repo':'traefik/traefik','number':number,'status':'staged',
                    'repair_job_id':f'job-{number}','retry_authorized_job_id':f'job-{number}'}
        assert ledger.try_claim(TZ,key)[0]
    ledger.release_claim('traefik/traefik#1',TZ)
    ledger.release_claim('traefik/traefik#2',TZ)
    queue['traefik/traefik#2'].pop('retry_authorized_job_id')
    queue['traefik/traefik#4']['status']='running'
    assert ledger.reserve_publication(TZ,10,'job-4','traefik/traefik','xixi-box:fix/issue-4-abcdef0-12345678','xixi-box/oss-scout')
    ledger.close()
    (tmp_path/'queue/candidates.json').write_text(json.dumps(queue))
    monkeypatch.setattr(pipeline.PolicyEngine,'load',lambda _: (SimpleNamespace(repo_entry=lambda _:{},tz_name=TZ,max_claims_per_day=5),None))
    monkeypatch.setattr(dashboard,'modifiable_repo',lambda _:True)
    monkeypatch.setattr(threading.Thread,'start',lambda _:None)
    monkeypatch.delenv('OSSCOUT_DISABLE_AUTO_REPAIR')
    dashboard.create_app(tmp_path,auth_enabled=False,password='test')
    result=json.loads((tmp_path/'queue/candidates.json').read_text(encoding='utf-8'))
    assert result['traefik/traefik#1']['status']=='staged'
    assert result['traefik/traefik#2']['status']=='escalated'
    assert result['traefik/traefik#3']['status']=='escalated'
    assert result['traefik/traefik#4']['status']=='running'
