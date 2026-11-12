"""名称相似度算法（配置可选，全部确定）。"""
from __future__ import annotations


def token_jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def token_dice(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))


def _levenshtein(s: str, t: str) -> int:
    if len(s) < len(t):
        s, t = t, s
    prev = list(range(len(t) + 1))
    for i, cs in enumerate(s, 1):
        cur = [i]
        for j, ct in enumerate(t, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (cs != ct)))
        prev = cur
    return prev[-1]


def levenshtein_ratio(s: str, t: str) -> float:
    if not s or not t:
        return 0.0
    d = _levenshtein(s, t)
    return 1.0 - d / max(len(s), len(t))


# 算法作用对象：token 集（Record.tokens）或规范化名称字符串（Record.legal_name）
ALGORITHMS = {
    "token_jaccard": lambda ra, rb: token_jaccard(ra.tokens, rb.tokens),
    "token_dice": lambda ra, rb: token_dice(ra.tokens, rb.tokens),
    "levenshtein_ratio": lambda ra, rb: levenshtein_ratio(
        ra.legal_name.casefold(), rb.legal_name.casefold()
    ),
}


def name_similarity(ra, rb, algorithm: str) -> float:
    return ALGORITHMS[algorithm](ra, rb)
