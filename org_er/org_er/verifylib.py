"""验证工具：配置样例自检、乱序稳定性、增量 vs 全量重算等价性。"""
from __future__ import annotations

import random

from .normalize import norm_regno
from .pipeline import fresh_build, state_signature
from .rules import regno_is_valid


def check_config_examples(cfg) -> list:
    """用配置中的样例自检注册号规则与占位值排除，返回问题列表（空 = 通过）。"""
    problems = []
    for country, rule in cfg.registration_rules.items():
        for v in rule.get("examples", {}).get("valid", []):
            nr = norm_regno(v, cfg.unicode_form, cfg.regno_strip_pattern)
            if not regno_is_valid(nr, country, cfg):
                problems.append(f"[{country}] 应合法但被拒: {v!r} (norm={nr!r})")
        for v in rule.get("examples", {}).get("invalid", []):
            nr = norm_regno(v, cfg.unicode_form, cfg.regno_strip_pattern)
            if regno_is_valid(nr, country, cfg):
                problems.append(f"[{country}] 应非法但被接受: {v!r} (norm={nr!r})")
    # 占位值在任何国家都必须无效
    for p in cfg.raw.get("placeholder_regnos", []):
        nr = norm_regno(p, cfg.unicode_form, cfg.regno_strip_pattern)
        for country in list(cfg.registration_rules) + ["XX"]:
            if regno_is_valid(nr, country, cfg):
                problems.append(f"占位值 {p!r} 在 {country} 被误判为合法")
    return problems


def check_shuffle_stability(raw_rows, cfg, seeds=(1, 2, 3)) -> dict:
    """打乱输入顺序重建，结果签名必须与原始顺序一致。"""
    baseline = state_signature(fresh_build(list(raw_rows), cfg))
    failures = []
    for seed in seeds:
        shuffled = list(raw_rows)
        random.Random(seed).shuffle(shuffled)
        sig = state_signature(fresh_build(shuffled, cfg))
        if sig != baseline:
            failures.append(seed)
    return {"ok": not failures, "seeds": list(seeds), "failures": failures}


def check_incremental_equivalence(raw_rows_final, incremental_result, cfg) -> dict:
    """增量结果必须等价于在全部当前条目上重新执行确定算法。"""
    fresh = fresh_build(list(raw_rows_final), cfg)
    same = state_signature(fresh) == state_signature(incremental_result)
    return {"ok": same}


def check_constraint(members: dict, records: dict) -> list:
    """不变量：同一实体内同一国家不得有两个不同有效注册号。"""
    by_ent = {}
    for k, eid in members.items():
        by_ent.setdefault(eid, []).append(k)
    violations = []
    for eid, keys in by_ent.items():
        seen = {}
        for k in keys:
            r = records[k]
            if r.regno_valid:
                prev = seen.setdefault(r.country_code, r.registration_no)
                if prev != r.registration_no:
                    violations.append((eid, r.country_code, prev, r.registration_no))
    return violations
