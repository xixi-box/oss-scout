"""用已落盘的验证补丁单独跑 publish，隔离网络问题与代码问题。

    python scripts/publish_from_artifact.py <artifact_dir> [issue_number]

管线在前几步（recheck/claim/AI 测试/后端修复/质量闸）通过后，只有 push+PR
没跑成时用这个：拿已有的 change.patch 走一遍 _publish_pr。

注意：不要用 `-e NO_PROXY="*"` 运行——容器靠 172.17.0.1:7890 的代理出网，
禁掉代理反而连不上 github.com（2026-10-28 实测）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from scout_v2.workers import auto_pipeline as ap  # noqa: E402

ROOT = Path("/data")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: publish_from_artifact.py <artifact_dir> [issue_number]")
        return 2
    art = Path(sys.argv[1])
    number = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    patch = art / "change.patch"
    if not patch.is_file():
        print(f"no change.patch in {art}")
        return 2
    repo = "xixi-box/oss-scout-v2-test"
    sha = ap.subprocess.run(["gh", "api", f"repos/{repo}/commits/HEAD", "--jq", ".sha"],
                            capture_output=True, text=True).stdout.strip()
    entry = {"repo": repo, "number": number, "title": "div() integer division regression"}
    summary: dict = {}
    url = ap._publish_pr(ROOT, entry, sha, patch, summary)
    print(json.dumps({"pr": url, "abort": summary.get("abort")},
                     ensure_ascii=False, indent=1))
    return 0 if url else 1


if __name__ == "__main__":
    raise SystemExit(main())
