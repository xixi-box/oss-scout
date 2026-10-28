"""从控制器侧跑完整 candidate_pipeline，验证 patch 回传与 PR 创建。

    python scripts/vps_pipeline_probe.py <owner/repo>#<issue>

只用于自建 fixture 仓库（会真的 push 分支 + 开 PR）。日闸会被临时放行，
仅在本进程内生效，不动 SQLite、不改生产代码。

验证目标（2026-10-28 的教训）：
  - 后端（GitHub Actions）产出的 patch 能否跨进程/跨机器回到控制器；
  - 质量闸与证据链是否真的通过；
  - PR 是否真的开出来。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from scout_v2 import repo_policy as rp          # noqa: E402
from scout_v2.state_store import ClaimLedger    # noqa: E402
from scout_v2.workers import auto_pipeline as ap  # noqa: E402

ROOT = Path("/data")


def _find(cid: str) -> dict | None:
    p = ROOT / "queue" / "candidates.json"
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and (k == cid or v.get("id") == cid):
                v.setdefault("id", k)
                return v
    return None


def main() -> int:
    cid = sys.argv[1] if len(sys.argv) > 1 else "xixi-box/oss-scout-v2-test#8"
    # 默认只允许自有 fixture 仓库：探针会绕过每日认领闸，跑在真实仓库上就是
    # 真实外发动作。要跑真实目标必须显式加 --real，避免手滑。
    real = "--real" in sys.argv
    if not real and not cid.startswith("xixi-box/"):
        print("refusing: probe may only run against own fixture repo "
              "(pass --real to target a real repo)")
        return 2
    entry = _find(cid)
    if entry is None:
        print(f"candidate not found: {cid}")
        return 2

    # 日闸放行（仅本进程）+ 清掉本地占额，让 try_claim 能再占一次。
    # 走 --real 时不绕过：真实仓库必须严格守每日 1 认领。
    if not real:
        rp.PolicyEngine.claim_slot_available = lambda self: True  # type: ignore[method-assign]
        ClaimLedger(ROOT / "state" / "scout_v2.sqlite3").release_day()

    print("entry:", json.dumps({k: entry.get(k) for k in
                                ("id", "repo", "number", "status", "lane")}, ensure_ascii=False))
    res = ap.candidate_pipeline(ROOT, entry)
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
