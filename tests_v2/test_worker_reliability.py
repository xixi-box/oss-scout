"""Run the Worker against fake GitHub, KV and dashboard APIs, without network."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_worker_reliability():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for Cloudflare Worker tests")
    script = Path(__file__).with_name("worker_reliability.mjs")
    out = subprocess.run([node, "--test", str(script)], capture_output=True,
                         text=True, encoding="utf-8", timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
