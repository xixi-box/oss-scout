"""Explicit self-test: concurrent subscription refresh and two tiny CLI requests.

No target repository, PR, publication reservation or GitHub write is involved.
Never prints credentials or response bodies from the authentication broker.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import tempfile
import time
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scout_v2.workers.codex_client import CodexClient


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--model",default="gpt-6.1-sol#medium")
    parser.add_argument("--refresh",action="store_true")
    parser.add_argument("--recover",action="store_true",help="One explicit recovery attempt for a quarantined refresh generation")
    parser.add_argument("--cli-probe-root",type=Path,help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.cli_probe_root:
        ok,reply=CodexClient(timeout_seconds=180,workdir=args.cli_probe_root).run(
            args.model,"Reply with exactly OSS_SCOUT_OK. Do not run commands, modify files, or access other directories.")
        passed=ok and "OSS_SCOUT_OK" in reply
        if not passed:
            print(json.dumps({"cli_probe_ok":ok,"detail":reply[:2000]}),flush=True)
        raise SystemExit(0 if passed else 1)
    leases=[]
    control=CodexClient(timeout_seconds=180,workdir=Path.cwd())
    refreshed=False
    try:
        for _ in range(2):
            acquired=control._exchange("acquire",protocol=2,recover=args.recover)
            assert acquired.get("protocol")==2 and acquired.get("lease"), "Concurrent credential lease was not granted"
            leases.append(acquired)
        assert leases[0]["lease"]!=leases[1]["lease"], "Lease ownership was reused"
        assert all(x["auth"]["tokens"]["refresh_token"].startswith("oss-scout-lease:") for x in leases)
        if args.refresh:
            def refresh(value):
                return control.refresh_credentials(value,recover=args.recover)
            with ThreadPoolExecutor(max_workers=2) as workers:
                responses=list(workers.map(refresh,leases))
            assert responses[0]["generation"]==responses[1]["generation"], "Concurrent refresh generations diverged"
            refreshed=responses[0]["generation"]!=leases[0]["generation"]
            assert refreshed, "Expected a refreshed credential generation"
    finally:
        for lease in leases:
            control._exchange("release",protocol=2,lease=lease["lease"])

    intervals=[]
    with tempfile.TemporaryDirectory(prefix="oss-codex-check-") as temporary:
        roots=[Path(temporary)/str(n) for n in (1,2)]
        for root in roots:
            root.mkdir()
            subprocess.run(["git","init","-q",str(root)],check=True)
            (root/"AGENTS.md").write_text("This is an oss-scout self-test. Reply only; do not run tools or change files.")
        def probe(root):
            start=time.monotonic()
            # Separate Python parents mirror separate Actions jobs. Each client's
            # detached-daemon cleanup then owns only its own descendants.
            completed=subprocess.run([sys.executable,str(Path(__file__).resolve()),
                "--model",args.model,"--cli-probe-root",str(root)],
                capture_output=True,text=True,encoding="utf-8",timeout=210)
            intervals.append((start,time.monotonic()))
            if completed.returncode:
                print(completed.stdout[-3000:],flush=True)
            return completed.returncode==0
        with ThreadPoolExecutor(max_workers=2) as workers:
            results=list(workers.map(probe,roots))
    assert all(results), "Concurrent Codex CLI self-test failed (credential values intentionally omitted)"
    assert max(start for start,_ in intervals)<min(end for _,end in intervals), "CLI execution did not overlap"
    print(json.dumps({"concurrent_leases":2,"broker_refresh_verified":refreshed,
                      "cli_requests_passed":2,"execution_overlapped":True,"github_writes":0}))


if __name__=="__main__":
    main()
