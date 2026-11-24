"""Parse trusted worker result envelopes from GitHub Actions logs."""
import json

RESULT_MARKER = "##OSSCOUT_RESULT##"

def parse_envelope(stdout: str) -> dict | None:
    """从 worker stdout 解析单行结果信封。找不到/解析失败 → None（调用方记 inconclusive）。

    兼容两种载体：本地 stdout（marker 在行首）、GitHub Actions 日志
    （每行有 `step \ttimestamp\t` 前缀）——因此按行内位置查找。
    """
    for line in stdout.splitlines():
        # 从后往前找：同一行可能多次出现 marker（如 Actions 日志里 echo 的源码行），
        # 最后一次出现之后才是真正的载荷
        idx = line.rfind(RESULT_MARKER)
        if idx < 0:
            continue
        payload = line[idx + len(RESULT_MARKER):].strip()
        try:
            env = json.loads(payload)
            if isinstance(env, dict) and "result" in env:
                return env
        except json.JSONDecodeError:
            continue  # 源码回显等伪 marker 行：跳过，继续找真正的输出行
    return None
