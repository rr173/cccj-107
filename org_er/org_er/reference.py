"""小规模穷举参考程序：全配对评估规则，核验阻塞候选不漏掉规则可接受的边。

仅用于验证（O(N^2)，仅限小规模）；生产管线只走三路阻塞。
"""
from __future__ import annotations

from .rules import evaluate_pair


def exhaustive_pairs(records: dict, cfg) -> dict:
    """对全部条目两两评估规则，返回 {(key_a, key_b): (edge_type, score)}。"""
    keys = sorted(records)
    out = {}
    for i, ka in enumerate(keys):
        for kb in keys[i + 1:]:
            res = evaluate_pair(records[ka], records[kb], cfg)
            if res["fired"]:
                out[(ka, kb)] = (res["edge_type"], res["score"])
    return out


def check_against_pipeline(records: dict, cfg, candidates: list, edges: list) -> dict:
    """对照管线结果，返回核验报告。"""
    ref = exhaustive_pairs(records, cfg)
    cand_set = {(c[0], c[1]) for c in candidates}
    pipe = {(e["key_a"], e["key_b"]): (e["edge_type"], e["score"]) for e in edges}

    missing_in_candidates = sorted(set(ref) - cand_set)          # 候选遗漏（必须为空）
    not_evaluated = sorted(set(ref) - set(pipe))                 # 管线未判定的可接受对（必须为空）
    extra_in_pipeline = sorted(set(pipe) - set(ref))             # 管线多判（必须为空）
    mismatched = sorted(p for p in set(ref) & set(pipe) if ref[p] != pipe[p])

    return {
        "n_records": len(records),
        "all_pairs": len(records) * (len(records) - 1) // 2,
        "n_candidates": len(cand_set),
        "n_reference_pairs": len(ref),
        "missing_in_candidates": missing_in_candidates,
        "not_evaluated": not_evaluated,
        "extra_in_pipeline": extra_in_pipeline,
        "mismatched": mismatched,
        "ok": not (missing_in_candidates or not_evaluated or extra_in_pipeline or mismatched),
    }
