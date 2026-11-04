"""诊断输出：容器健康 + 凭据接线 + pr-fix 派发审计（只打印布尔与摘要）。

被 diagnose.yml 调用。2026-10-31 仓库转公开后，token 从
/data/configs/cloudflare.yaml 移到了环境变量（CF_API_TOKEN / CF_INGEST_TOKEN），
这里验证接线是否正确。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml


def _pr_watch_tail() -> None:
    """最近 10 条 pr_watch 审计（pr-fix 派发/草拟结果在这里）。"""
    try:
        lines = Path("/data/state/worklog.jsonl").read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    hits = [ln for ln in lines
            if 'pr_fix' in ln or 'pr_needs_reply' in ln or 'pr_reply' in ln
            or 'pr_ci_failed' in ln or 'pr_watch_error' in ln
            or 'pr_sync' in ln]
    print(f"--- pr_watch audit tail (matched {len(hits)}) ---")
    for ln in hits[-14:]:
        try:
            r = json.loads(ln)
            print(f"  {r.get('ts')} {r.get('action')} {r.get('target')} :: "
                  f"{str(r.get('result'))[:110]}")
        except Exception:  # noqa: BLE001
            print(" ", ln[-140:])


def _dup_probe() -> None:
    """对已知案例跑一次 duplicate 判定，确认闸门生效（只读）。"""
    try:
        from scout_v2.git_host import recheck_candidate
    except Exception as e:  # noqa: BLE001
        print("dup probe: import failed", str(e)[:100])
        return
    cases = [("qdrant/qdrant", 11360, "KNOWN-DUP(->10465)"),
             ("milvus-io/milvus", 55033, "normal")]
    print("--- duplicate probe ---")
    for repo, num, label in cases:
        try:
            r = recheck_candidate(repo, num)
        except Exception as e:  # noqa: BLE001
            print(f"  {label}: error {str(e)[:80]}")
            continue
        if r is None or not r.complete:
            print(f"  {label}: incomplete")
            continue
        print(f"  {label}: open={r.issue_open} dup={r.is_duplicate} "
              f"of={r.duplicate_of}")


def main() -> int:
    f = yaml.safe_load(open("/data/configs/cloudflare.yaml", encoding="utf-8")) or {}
    print("file_token_empty:", f.get("api_token") in ("", None))
    print("env_api_set:", bool(os.environ.get("CF_API_TOKEN")))
    print("env_ingest_set:", bool(os.environ.get("CF_INGEST_TOKEN")))
    sys.path.insert(0, "/app")
    from dashboard import data as D

    class _DS:
        root = Path("/data")

    cfg = D.cloudflare_cfg(_DS())
    print("effective_api_set:", bool(cfg.get("api_token")))
    print("effective_ingest_set:", bool(cfg.get("ingest_token")))
    # PR 评论通道的前提：Worker 能读到 PR 列表（dashboard 每 15 分钟写 KV）
    sync = Path("/data/state/pr_sync.json")
    try:
        prs = json.loads(sync.read_text(encoding="utf-8"))
        print("pr_sync_at:", prs.get("at"))
        print("pr_sync_count:", len(prs.get("prs", [])))
    except Exception:  # noqa: BLE001
        print("pr_sync_count: 0")
    _pr_watch_tail()
    _cap_audit()
    _dup_probe()
    _feishu_probe()
    return 0


def _feishu_probe() -> None:
    """飞书应用接线：凭证是否注入 + 能否找到群（只打印布尔与群名）。"""
    sys.path.insert(0, "/app")
    print("--- feishu app ---")
    for k in ("FEISHU_APP_ID", "FEISHU_APP_SECRET",
              "FEISHU_VERIFICATION_TOKEN", "FEISHU_CHAT_ID"):
        print(f"  {k} set:", bool(os.environ.get(k)))
    try:
        from scout_v2 import feishu_app
    except Exception as e:  # noqa: BLE001
        print("  import failed:", str(e)[:120])
        return
    print("  app_configured:", feishu_app.app_configured())
    items, err = feishu_app.list_chats()
    print("  chats visible:", len(items), ("err=" + err[:160]) if err else "")
    for it in items[:5]:
        print(f"    {it.get('chat_id')}  {it.get('name')}")
    print("  resolved chat:", feishu_app.find_group_chat_id() or "NONE")


def _cap_audit() -> None:
    """今日 claim 计数 vs sqlite 实际占额（排查额度穿透）。"""
    import collections
    acts = collections.Counter()
    per_target = collections.Counter()
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    try:
        lines = Path("/data/state/worklog.jsonl").read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not str(r.get("ts", "")).startswith(today):
            continue
        a = str(r.get("action", ""))
        if a == "claim" or a.startswith("pipeline_"):
            acts[a] += 1
            if a == "claim":
                per_target[str(r.get("target"))] += 1
    print(f"--- claim cap audit ({today}) ---")
    for k, v in acts.most_common(10):
        print(f"  {k:30s} {v}")
    print("  repeated claims:")
    for t, n in per_target.most_common(6):
        if n > 1:
            print(f"    {t:40s} x{n}")


if __name__ == "__main__":
    raise SystemExit(main())
