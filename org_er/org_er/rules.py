"""匹配规则（配置化、确定）。管线与穷举参考程序共用本模块，保证语义唯一。

规则：
  强边：同国家 且 双方注册号均合法 且 规范化注册号相同；
  弱边：规范化域名相同、域名可作证据（非免费邮箱/共享托管）、名称相似度达到阈值。
"""
from __future__ import annotations

import re

from .similarity import name_similarity


def regno_is_valid(norm_regno: str, country: str, cfg) -> bool:
    """合法注册号判定：非空、非占位值（列表或模式）、匹配国家正则（无国家规则用兜底）。"""
    if not norm_regno:
        return False
    if norm_regno in cfg.placeholders:
        return False
    for pat in cfg.placeholder_patterns:
        if pat.fullmatch(norm_regno):
            return False
    pattern = cfg.registration_rules.get(country, {}).get("pattern", cfg.default_regno_pattern)
    return re.fullmatch(pattern, norm_regno) is not None


def host_exclusion(host: str, cfg) -> str:
    """域名排除类别：''（可作证据）/ 'free_email' / 'shared_hosting'。精确或子域名命中即排除。"""
    if not host:
        return ""
    for d in cfg.free_email_domains:
        if host == d or host.endswith("." + d):
            return "free_email"
    for d in cfg.shared_hosting_domains:
        if host == d or host.endswith("." + d):
            return "shared_hosting"
    return ""


def evaluate_pair(ra, rb, cfg) -> dict:
    """评估一对条目是否满足匹配规则，返回可解释结果。

    返回 dict：
      fired     是否有规则命中
      edge_type 'strong' | 'weak' | None（两者同时命中时强边优先，证据并集保留）
      score     强边 1.0；弱边为名称相似度
      evidence  命中规则的结构化证据列表
      notes     未命中原因（用于候选审计，如域名被排除、相似度不足）
    """
    evidence, notes = [], []

    strong = False
    if ra.country_code == rb.country_code:
        if ra.regno_valid and rb.regno_valid:
            if ra.registration_no == rb.registration_no:
                strong = True
                evidence.append({
                    "rule": "strong:same_country_same_valid_regno",
                    "country": ra.country_code,
                    "registration_no": ra.registration_no,
                })
            else:
                notes.append(f"regno_differs({ra.country_code})")
        else:
            notes.append("regno_invalid_or_missing")
    else:
        notes.append("country_differs")

    weak = False
    sim = None
    if ra.website_host and ra.website_host == rb.website_host:
        if ra.host_evidence and rb.host_evidence:
            sim = name_similarity(ra, rb, cfg.sim_algorithm)
            if sim >= cfg.sim_threshold:
                weak = True
                evidence.append({
                    "rule": "weak:same_host_and_name_similarity",
                    "host": ra.website_host,
                    "similarity": round(sim, 6),
                    "algorithm": cfg.sim_algorithm,
                    "threshold": cfg.sim_threshold,
                })
            else:
                notes.append(f"name_similarity_below_threshold({sim:.4f}<{cfg.sim_threshold})")
        else:
            excl = ra.host_exclusion or rb.host_exclusion or "not_evidence"
            notes.append(f"host_not_evidence({ra.website_host}:{excl})")

    fired = strong or weak
    edge_type = "strong" if strong else ("weak" if weak else None)
    score = 1.0 if strong else (round(sim, 6) if weak else None)
    return {"fired": fired, "edge_type": edge_type, "score": score,
            "evidence": evidence, "notes": notes}
