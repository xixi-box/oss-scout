"""workflow 文件必须通过 GitHub 的严格解析，而不是 PyYAML 的宽松解析。

2026-11-04 实录：pr-fix workflow 的一个 step 里写了两个 env 键。PyYAML
静默容忍重复键（取最后一个），本地测试全绿；GitHub 的解析器直接判文件
非法——后果是两层的：每次 push 多一个红叉（"workflow file issue"），
而且 workflow_dispatch 全部瘫痪，review 修复流水线停摆一天。
"""
from __future__ import annotations

import glob
from pathlib import Path

import yaml


class _StrictLoader(yaml.SafeLoader):
    pass


def _no_dup(loader, node, deep=False):
    seen = set()
    for k, _v in node.value:
        key = loader.construct_object(k, deep=deep)
        if key in seen:
            raise ValueError(f"duplicate key: {key!r}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup)


def test_all_workflow_files_have_no_duplicate_keys():
    files = sorted(glob.glob(str(Path(__file__).resolve().parents[1]
                                 / ".github" / "workflows" / "*.yml")))
    assert files, "workflow directory not found"
    bad = []
    for f in files:
        try:
            yaml.load(open(f, encoding="utf-8"), Loader=_StrictLoader)
        except Exception as e:
            bad.append(f"{Path(f).name}: {e}")
    assert not bad, "GitHub 会拒绝这些文件（PyYAML 不报错）：" + "; ".join(bad)
