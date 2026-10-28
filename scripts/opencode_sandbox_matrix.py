"""在 Modal Sandbox 里验证 opencode CLI 的真实行为（诊断用，不参与生产）。

背景（2026-10-28）：worker 容器里 `opencode run` 对"改文件"任务长时间无输出、
不退出，导致 repair 占满 Sandbox 直到超时。本脚本把可疑变量逐个隔离。

**结论已在 2026-10-28 得出，并据此把 repair 迁到 GitHub 托管 runner：**
opencode CLI 是交互式 TUI 工具，无 tty 时会在 bootstrap 阶段卡住（插件安装、
权限询问、models 端点探测，对应上游 issue #44684 / #14473 / #47282 / #44782）。
纯文本提问偶尔能过，任何需要编辑文件的任务都会挂。这不是我们代码的问题，
加 --pure/--auto 或改 snapshot/autoupdate 配置都无法绕过。

保留本脚本是为了：容器里再出现"CLI 不响应"时，能快速确认是不是同一个原因，
而不是重新摸索一遍。它只做诊断，生产 repair 走 .github/workflows/oss-scout-repair.yml。

矩阵写入 Sandbox 内的 /tmp/matrix.sh，避免 Python f-string 里嵌套 heredoc 的
引号地狱（第一版就是这么炸的）。所有输出落 /tmp/matrix.log。

用法（服务器上，避免本地对 Modal 的长连接被重置）：
    docker exec -e NO_PROXY="*" -e OPENCODE_API_KEY=... oss-scout \
        sh -c 'cd /app && python scripts/opencode_sandbox_matrix.py'
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import modal

DF = Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.worker"

# 场景定义：(name, git?, perm?, flags, task, seconds)
SCENARIOS = [
    ("c1_chat_nogit", False, False, "", "Say OK", 90),
    ("c2_chat_git", True, False, "", "Say OK", 90),
    ("c3_edit_git_plain", True, False, "",
     "Edit calc.py: change // to / so div(7,2)==3.5. Only edit calc.py.", 240),
    ("c4_edit_git_perm", True, True, "",
     "Edit calc.py: change // to / so div(7,2)==3.5. Only edit calc.py.", 240),
    ("c5_edit_git_pure_auto", True, True, "--pure --auto",
     "Edit calc.py: change // to / so div(7,2)==3.5. Only edit calc.py.", 240),
]

PERM_JSON = '{"permission":{"bash":"deny","webfetch":"deny","edit":"allow"}}'


def build_shell() -> str:
    """拼一份纯 shell 脚本：不经过 Python f-string 转义。"""
    lines = [
        "#!/bin/bash",
        "set -u",
        'mkdir -p "$HOME/.local/share/opencode"',
        "python3 - <<'PY'",
        "import json, os",
        'p = os.path.expanduser("~/.local/share/opencode/auth.json")',
        "os.makedirs(os.path.dirname(p), exist_ok=True)",
        'open(p, "w").write(json.dumps({"opencode-go": {"type": "api", '
        '"key": os.environ["OPENCODE_API_KEY"]}}))',
        "PY",
        "",
    ]
    for name, git, perm, flags, task, secs in SCENARIOS:
        lines += [
            f'echo "########## {name} ##########"',
            f'cd /tmp && rm -rf "{name}" && mkdir "{name}" && cd "{name}"',
            "printf 'def div(a, b):\\n    return a // b\\n' > calc.py",
        ]
        if git:
            lines.append("git init -q -b main . && git add -A && "
                         "git -c user.name=t -c user.email=t@t commit -qm base")
        if perm:
            lines.append(f"printf '%s' '{PERM_JSON}' > opencode.json")
        lines += [
            "S=$(date +%s)",
            f'timeout {secs} opencode run {flags} -m opencode-go/deepseek-v4.1-flash '
            f'"{task}" > /tmp/{name}.txt 2>&1',
            "RC=$?",
            "E=$(date +%s)",
            f'echo "rc=$RC elapsed=$((E-S))s bytes=$(wc -c < /tmp/{name}.txt)"',
            'echo "--- calc.py ---"; cat calc.py',
            f'echo "--- output tail ---"; tail -c 400 /tmp/{name}.txt',
            "",
        ]
    lines += ['echo "########## DONE ##########"']
    return "\n".join(lines)


def main() -> int:
    image = (modal.Image.from_dockerfile(str(DF), context_dir=str(DF.parents[1]))
             .entrypoint([]))
    app = modal.App.lookup("oss-scout-worker", create_if_missing=True)
    shell = build_shell()
    # 先落盘再执行：把脚本写成单引号 heredoc，避免任何二次解释
    wrapper = ("cat > /tmp/matrix.sh <<'MATRIX_EOF'\n" + shell
               + "\nMATRIX_EOF\nchmod +x /tmp/matrix.sh\n"
                 "bash /tmp/matrix.sh > /tmp/matrix.log 2>&1\n"
                 'echo "########## DONE ##########" >> /tmp/matrix.log\n')
    sb = modal.Sandbox.create(
        "bash", "-lc", wrapper, app=app, image=image,
        env={"OPENCODE_API_KEY": os.environ.get("OPENCODE_API_KEY", "")},
        timeout=1800, cpu=2, memory=4096)
    print("sandbox_id:", sb.object_id, flush=True)
    sb.wait()
    print(sb.stdout.read()[-2000:])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
