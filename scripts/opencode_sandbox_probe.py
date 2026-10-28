"""隔离测试：Sandbox 里 opencode CLI 能否正常完成一次最小调用。

不涉及 git、不涉及修复，只回答"CLI 在沙箱里到底能不能用、卡在哪"。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/app")
import modal  # noqa: E402

KEY = __import__("os").environ.get("OPENCODE_API_KEY", "").strip()
DF = Path("/app/docker/Dockerfile.worker")


def main() -> int:
    # 镜像带 ENTRYPOINT（跑 worker job）。这里要跑诊断脚本，先清空它。
    image = modal.Image.from_dockerfile(
        str(DF), context_dir=str(DF.resolve().parents[1])).entrypoint([])
    app = modal.App.lookup("oss-scout-worker", create_if_missing=True)
    script = r'''
set -x
echo "--- version ---"
opencode --version || echo "VERSION FAILED rc=$?"
echo "--- which ---"
which opencode; ls -l /usr/local/bin/opencode
echo "--- auth ---"
mkdir -p "$HOME/.local/share/opencode"
printf '%s' "$OPENCODE_API_KEY" | head -c 8; echo "..."
python - <<'PY'
import json, os
p = os.path.expanduser("~/.local/share/opencode/auth.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
open(p, "w").write(json.dumps({"opencode-go": {"type": "api", "key": os.environ["OPENCODE_API_KEY"]}}))
print("wrote", p)
PY
echo "--- run (120s cap) ---"
cd /tmp && mkdir -p probe && cd probe
timeout 120 opencode run -m opencode-go/deepseek-v4.1-flash --format json "Reply with the single word: OK" 2>&1 | head -40
echo "RC=$?"
'''
    sb = modal.Sandbox.create(
        "bash", "-lc", script, app=app, image=image,
        env={"OPENCODE_API_KEY": KEY},
        timeout=300, cpu=2, memory=4096)
    rc = sb.wait()
    print("=== rc:", rc)
    print("=== STDOUT ===")
    print(sb.stdout.read()[-6000:])
    print("=== STDERR ===")
    print(sb.stderr.read()[-3000:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
