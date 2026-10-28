"""对 wide_hunt 结果做二次过滤：用修好的复检逻辑确认真无竞争。

    python scripts/verify_candidates.py [--in state/wide_hunt_full.json] [--top N]

第二次过滤的意义（2026-10-28）：wide_hunt 只查了 timeline 的 cross-referenced，
而很多人直接开 PR 不写 Fixes（ansible#87799 就是这样漏掉的）。这里对每条候选
再按标题里的标识符搜一次 open PR。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scout_v2.git_host import recheck_candidate  # noqa: E402

BAD_LABELS = ("parked", "duplicate", "wontfix", "invalid", "stale",
              "needs_triage", "needs-triage", "needs investigation",
              "needs-investigation", "not yet ready")


def keywords(title: str) -> list[str]:
    raw = [w for w in re.split(r"[^a-zA-Z0-9_.-]+", title)
           if len(w) >= 5 and not w.isdigit()]
    ident = [w for w in raw if "_" in w or "." in w]
    return (ident + [w for w in raw if w not in ident])[:3]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(ROOT / "state" / "wide_hunt_full.json"))
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--out", default=str(ROOT / "state" / "verified.json"))
    args = ap.parse_args()

    data = json.loads(Path(args.inp).read_text(encoding="utf-8"))
    hits = data.get("hits", [])
    if args.top:
        hits = hits[:args.top]

    clean: list[dict] = []
    rejected: list[dict] = []
    for i, h in enumerate(hits, 1):
        labs = " ".join(str(x).lower() for x in h.get("labels") or [])
        if any(b in labs for b in BAD_LABELS):
            rejected.append({**h, "why": "label"})
            continue
        kws = keywords(h["title"])
        try:
            r = recheck_candidate(h["repo"], h["number"], root_cause_keywords=kws or None)
        except Exception as e:  # noqa: BLE001
            rejected.append({**h, "why": f"recheck error: {type(e).__name__}"})
            continue
        if r is None or not r.complete:
            rejected.append({**h, "why": "recheck incomplete"})
            continue
        if not r.issue_open:
            rejected.append({**h, "why": "closed"})
            continue
        if not r.unassigned:
            rejected.append({**h, "why": "assigned"})
            continue
        if r.has_cross_ref_pr or r.open_pr_hits:
            rejected.append({**h, "why": f"competed: {(r.open_pr_hits or ['xref'])[0][:60]}"})
            continue
        if r.has_claim_signal:
            rejected.append({**h, "why": "claim signal"})
            continue
        clean.append({**h, "keywords": kws})
        print(f"  CLEAN {h['repo']}#{h['number']}  {h['title'][:56]}", flush=True)

    Path(args.out).write_text(
        json.dumps({"clean": clean, "rejected": rejected}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    print(f"\n=== {len(clean)} truly clean / {len(hits)} checked ===")
    for h in clean:
        print("%-36s #%-6d %6.1fh c=%-2d %s" %
              (h["repo"], h["number"], h["age_h"], h["comments"], h["title"][:52]))
    print(f"\nsaved: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
