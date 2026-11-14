"""Conservative multilingual evidence; no match never proves absence of intent."""
import re
from pathlib import Path

import yaml


MULTILINGUAL_REVIEW_RULES = (
    "Read Chinese, English and mixed-language comments for meaning, not just keywords. "
    "Distinguish a concrete code request, a question, ownership/duplicate-work coordination and thanks. "
    "An author saying they have submitted their own implementation in another PR, want to continue "
    "there or avoid duplicate work (例如：为避免两边重复投入，我想继续跟进另一份 PR) is coordination, "
    "not a request to change our code. Do not invent a fix, promise to close a PR or dismiss it as thanks. "
    "For coordination alone, leave code unchanged and offer an independently useful acknowledgement "
    "only when appropriate; requested_change must be false. Never close, merge or post anything yourself."
)


_EN_ACTION = (
    r"(?:fix\b|work\s+on\b|implement\b|handle\b|"
    r"(?:prepare|submit|open|send|provide)\b[^.!?\n]{0,80}\b(?:fix|patch|pr|pull\s+request|regression\s+tests?)\b)"
)
SELF_FIX_PATTERN = (
    r"\b(?:i|we)\s*(?:'ll|'d\s+like\s+to|will|would\s+like\s+to|can|want\s+to|"
    r"plan\s+to|intend\s+to|am\s+(?:happy|willing)\s+to)\s+" + _EN_ACTION
    + r"|\b(?:may|can|could|shall|should)\s+i\s+" + _EN_ACTION
    + r"|\b(?:happy|glad|willing)\s+to\s+" + _EN_ACTION
    + r"|(?:我|我们|本人)(?:这边)?\s*(?:愿意|可以|打算|计划|准备|想要|想|会|来|正在|已经|已)"
    r"\s*(?:自己|亲自|继续|尝试|试着)?\s*(?:提交|提供|准备|编写|实现|修复|解决|处理|跟进|认领)"
    r"[^。！？\n]{0,60}(?:修复|回归测试|补丁|实现|这个问题|该问题|这个|此问题|PR\b|拉取请求)"
    r"|(?:这个问题|该问题|这个\s*issue)[^。！？\n]{0,8}(?:我|我们)来(?:修|处理|解决)"
    r"|(?:我|我们)来(?:修复|处理|解决)(?:这个|该|此)?(?:问题|缺陷|bug|issue)"
    r"|(?:我|我们)(?:愿意|可以|打算|计划|准备|正在|已经|已|会|来)(?:自己|亲自)?(?:修复|认领)"
)
ASSIGNMENT_PATTERN = (
    r"(?:已(?:经)?(?:被(?:其他人|别人|人|\s*@[-\w]+)?\s*)?(?:指派|分配|认领)|(?:请|由|交给)\s*@[-\w]+[^。！？\n]{0,15}(?:修复|处理|负责))"
)
NO_AI_PATTERN = r"(?:不要|请勿|禁止|不接受|不允许)[^。！？\n]{0,16}(?:AI|智能体|机器人|自动化贡献)"
WORK_INTENT_PATTERN = f"(?:{SELF_FIX_PATTERN})|(?:{ASSIGNMENT_PATTERN})|(?:{NO_AI_PATTERN})"
RE_SELF_FIX = re.compile(SELF_FIX_PATTERN, re.I)
RE_WORK_INTENT = re.compile(WORK_INTENT_PATTERN, re.I)
_NEGATED = re.compile(
    r"\b(?:i|we)\s+(?:(?:am|are)\s+not|(?:do|will|can|would)\s+not|don't|won't|can't|cannot|no\s+longer)"
    r"[^.!?]{0,25}\b(?:fix|work|submit|prepare|implement|handle)\b"
    r"|\b(?:nobody|no\s+one)\s+(?:is\s+)?working\b"
    r"|(?:我|我们)(?:目前|现在)?(?:不打算|不准备|不愿意|不会|无法|不能|不再)[^。！？]{0,15}(?:修复|处理|提交|跟进)"
    r"|(?:提供(?:一些|相关|测试)?(?:日志|截图|堆栈)|\bprovide\s+(?:(?:some|the)\s+)?(?:logs|screenshots|tracebacks)\b)", re.I)
_COORDINATION = re.compile(
    r"避免[^。！？\n]{0,15}重复(?:投入|劳动|工作|开发|修复)|不(?:另|再)[^。！？\n]{0,30}(?:推进|跟进|处理)"
    r"|(?:已经|已)[^。！？\n]{0,60}(?:提交|实现|修复)[^。！？\n]{0,40}#\d+"
    r"|\bavoid\b[^.!?\n]{0,30}\bduplicat\w*\s+(?:work|effort)\b|\b(?:my|our|own)\b[^.!?\n]{0,60}\b(?:fix|implementation|PR)\b[^.!?\n]{0,40}#\d+"
    r"|\b(?:continue|focus|discuss)\b[^.!?\n]{0,40}#\d+", re.I)


def prose_clauses(text: str) -> list[str]:
    text = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1[^\n]*$", "", text or "")
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    text = re.sub(r"`[^`\n]+`", "", text)
    return [part.strip() for part in re.split(r"[。！？\n]|(?<=[.!?])\s+|[,，;；]", text) if part.strip()]


def claim_evidence(text: str, *, pattern=RE_WORK_INTENT) -> list[str]:
    return [part[:300] for part in prose_clauses(text)
            if not _NEGATED.search(part) and (pattern.search(part) or RE_WORK_INTENT.search(part))][:5]


def self_fix_evidence(text: str) -> list[str]:
    return [part[:300] for part in prose_clauses(text)
            if not _NEGATED.search(part) and RE_SELF_FIX.search(part)][:5]


def coordination_evidence(text: str) -> bool:
    return any(_COORDINATION.search(part) for part in prose_clauses(text))


def pr_automation_hold(root: Path, repo: str, number: int) -> bool:
    path = root / "configs/pr_automation_holds.yaml"
    holds = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return f"{repo}#{number}".lower() in {str(key).lower() for key in (holds or {})}
