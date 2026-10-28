"""端到端修复探针（在容器内手动跑）：

    python scripts/repair_e2e_probe.py <owner/repo>#<issue>

把指定候选走完整 auto_pipeline 链路并打印每步结果（不写 GitHub：
管道在证据链不全时自己中止；证据链完整时会真的 push+PR，慎用）。

用途是验证 JobRouter → Modal Sandbox → 结果信封 → 质量闸这条链。

注意：每日 1 次认领是硬闸，探针会临时绕开它（只在本进程内 monkeypatch，
不动 SQLite、不改生产代码），因此**只能用 fixture 仓库跑**。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from scout_v2 import repo_policy as rp  # noqa: E402
from scout_v2.state_store import ClaimLedger  # noqa: E402
from scout_v2.workers import auto_pipeline as ap  # noqa: E402


def _find(root: Path, cid: str) -> dict | None:
    """candidates.json 是 {id: entry} 的平铺 dict（历史格式还可能是 list）。"""
    for p in (root / "queue" / "candidates.json", Path("/data/queue/candidates.json")):
        if not p.exists():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict) and (k == cid or v.get("id") == cid):
                    v.setdefault("id", k)
                    return v
        elif isinstance(data, list):
            for c in data:
                if isinstance(c, dict) and c.get("id") == cid:
                    return c
    return None


def main() -> int:
    cid = sys.argv[1] if len(sys.argv) > 1 else "xixi-box/oss-scout-v2-test#8"
    if not cid.startswith("xixi-box/"):
        print("refusing: probe may only run against own fixture repo")
        return 2
    root = Path("/data")
    entry = _find(root, cid)
    if entry is None:
        print(f"candidate not found in queue: {cid}")
        return 2

    # 1) 日闸放行（仅本进程）
    rp.PolicyEngine.claim_slot_available = lambda self: True  # type: ignore[method-assign]
    # 2) 本地 claim 记录清掉，让 try_claim 能再占一次
    ClaimLedger(root / "state" / "scout_v2.sqlite3").release_day()

    print("entry:", json.dumps({k: entry.get(k) for k in
                                ("id", "repo", "number", "status", "lane")}, ensure_ascii=False))
    res = ap.candidate_pipeline(root, entry)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
