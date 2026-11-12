"""配置加载与校验：注册号规则、占位值、域名证据排除、相似度、阻塞参数。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .normalize import norm_regno
from .similarity import ALGORITHMS


@dataclass(frozen=True)
class ERConfig:
    path: str
    unicode_form: str
    strip_www: bool
    regno_strip_pattern: str
    legal_form_tokens: frozenset
    cjk_legal_suffixes: tuple
    registration_rules: dict          # country -> {"pattern": str, "examples": {...}}
    default_regno_pattern: str
    placeholders: frozenset           # 规范化后的占位注册号
    placeholder_patterns: tuple       # 编译后的占位正则
    free_email_domains: frozenset
    shared_hosting_domains: frozenset
    sim_algorithm: str
    sim_threshold: float
    max_token_block_size: int
    raw: dict


def _has_cjk(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in s)


def load_config(path) -> ERConfig:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)

    norm = raw.get("normalization", {})
    form = norm.get("unicode_form", "NFKC")
    strip_pattern = norm.get("regno_strip_pattern", "")

    rules_raw = raw.get("registration_rules", {})
    default_pattern = rules_raw.get("default", {}).get("pattern", "^[A-Z0-9]{4,20}$")
    country_rules = {k: v for k, v in rules_raw.items() if k not in ("default", "comment")}
    re.compile(default_pattern)
    for c, r in country_rules.items():
        if "pattern" not in r:
            raise ValueError(f"注册号规则 {country} 缺少 pattern")
        re.compile(r["pattern"])  # 提前暴露配置错误

    # 占位值与条目走同一规范化，保证 "n/a" 与 "N/A" 等价命中
    placeholders = frozenset(norm_regno(p, form, strip_pattern) for p in raw.get("placeholder_regnos", []))
    placeholder_patterns = tuple(re.compile(p) for p in raw.get("placeholder_patterns", []))

    dom = raw.get("domain_evidence", {})
    free_email = frozenset(d.strip().lower() for d in dom.get("free_email_domains", []))
    shared_hosting = frozenset(d.strip().lower() for d in dom.get("shared_hosting_domains", []))

    sim = raw.get("similarity", {})
    algo = sim.get("algorithm", "token_jaccard")
    if algo not in ALGORITHMS:
        raise ValueError(f"未知相似度算法: {algo}（可选：{sorted(ALGORITHMS)}）")
    threshold = float(sim.get("threshold", 0.5))
    if not 0.0 < threshold <= 1.0:
        raise ValueError(f"相似度阈值须在 (0,1]：{threshold}")

    legal = frozenset(t.casefold() for t in norm.get("legal_form_tokens", []))
    cjk_suffixes = tuple(sorted((t for t in legal if _has_cjk(t)), key=len, reverse=True))

    blocking = raw.get("blocking", {})

    return ERConfig(
        path=str(path),
        unicode_form=form,
        strip_www=bool(norm.get("strip_www", True)),
        regno_strip_pattern=strip_pattern,
        legal_form_tokens=legal,
        cjk_legal_suffixes=cjk_suffixes,
        registration_rules=country_rules,
        default_regno_pattern=default_pattern,
        placeholders=placeholders,
        placeholder_patterns=placeholder_patterns,
        free_email_domains=free_email,
        shared_hosting_domains=shared_hosting,
        sim_algorithm=algo,
        sim_threshold=threshold,
        max_token_block_size=int(blocking.get("max_token_block_size", 100000)),
        raw=raw,
    )
