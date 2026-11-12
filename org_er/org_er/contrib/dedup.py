"""论文去重与版本选取。

分组键：
  有 DOI   -> ("doi", 规范化 DOI)        同一篇被多家重复收录时归并
  无 DOI   -> ("noid", 供应方, 论文号)    按供应方+论文号独立保留，互不归并

版本选取（同组多版本时）：
  1) 按命令指定的供应方优先级取唯一最高优先级版本的整份作者名单（不跨名单拼作者）；
  2) 年份或作者顺序等有冲突时仍按优先级选取，但逐版本记录差异与选取理由；
  3) 优先级打平：内容一致则按 (供应方, 论文号) 确定序取一（理由 tie_identical_content）；
     内容不同则整篇隔离（quarantine），绝不按输入顺序挑一个。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .parse import CatalogLine


def content_fingerprint(line: CatalogLine) -> str:
    """版本内容指纹：年份 + 有序作者名单（作者名规范化、署名集合排序）。

    署名条目内部顺序不影响指纹（署名次序无语义）；作者顺序影响指纹。
    """
    payload = {
        "year": line.year,
        "authors": [
            [a.name_norm, sorted(list(r.key_parts()) for r in a.affils)]
            for a in line.authors
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _author_entries(line: CatalogLine):
    """[(name_norm, tuple(sorted affil key parts)), ...] 按作者顺序。"""
    return [(a.name_norm, tuple(sorted(r.key_parts() for r in a.affils)))
            for a in line.authors]


def diff_flags(selected: CatalogLine, other: CatalogLine) -> list:
    """比较两个版本的内容差异，返回差异标记列表（空 = 内容一致）。"""
    flags = []
    if selected.year != other.year:
        flags.append("year_differs")
    sel_names = [e[0] for e in _author_entries(selected)]
    oth_names = [e[0] for e in _author_entries(other)]
    if sorted(sel_names) != sorted(oth_names):
        flags.append("author_list_differs")       # 作者名单（人）不同
    elif sel_names != oth_names:
        flags.append("author_order_differs")      # 同人不同序
    else:
        sel_affils = [e[1] for e in _author_entries(selected)]
        oth_affils = [e[1] for e in _author_entries(other)]
        if sel_affils != oth_affils:
            flags.append("affil_differs")         # 同人同序但署名不同
    return flags


@dataclass
class Version:
    line: CatalogLine
    content_hash: str
    rank: int                       # 供应方优先级名次（未列入 = len(priority)）
    is_selected: bool = False


@dataclass
class PaperGroup:
    group_key: str
    group_kind: str                 # 'doi' | 'noid'
    versions: list = field(default_factory=list)  # Version，确定序
    status: str = ""                # 'counted' | 'quarantined'
    selected: Version = None
    select_reason: str = ""
    version_diffs: list = field(default_factory=list)  # 逐版本差异说明


def group_key_of(line: CatalogLine):
    if line.doi_norm:
        return (f"doi::{line.doi_norm}", "doi")
    return (f"noid::{line.supplier_norm}::{line.paper_id_norm}", "noid")


def select_group(group: PaperGroup) -> PaperGroup:
    """对分组执行版本选取/隔离。versions 已按确定序排列。"""
    vs = group.versions
    best_rank = min(v.rank for v in vs)
    best = [v for v in vs if v.rank == best_rank]
    if len(vs) == 1:
        group.selected, group.select_reason = vs[0], "single_version"
    elif len(best) == 1:
        group.selected, group.select_reason = best[0], "unique_best_priority"
    else:
        hashes = {v.content_hash for v in best}
        if len(hashes) == 1:
            group.selected, group.select_reason = best[0], "tie_identical_content"
        else:
            group.status = "quarantined"
            group.select_reason = "quarantine:tie_conflicting_content"
            group.selected = None
    if group.selected is not None:
        group.status = "counted"
        group.selected.is_selected = True

    # 逐版本差异说明（相对被选中版本；隔离时相对确定序首版本，仅供核查）
    base = group.selected.line if group.selected else vs[0].line
    for v in vs:
        group.version_diffs.append({
            "supplier": v.line.supplier,
            "paper_id": v.line.paper_id,
            "line_no": v.line.line_no,
            "year": v.line.year,
            "n_authors": len(v.line.authors),
            "priority_rank": v.rank,
            "content_hash": v.content_hash,
            "selected": v.is_selected,
            "diffs": ([] if v.line is base else diff_flags(base, v.line)),
        })
    return group


def dedup_lines(ok_lines: list, priority: list) -> list:
    """把 ok 行分组并逐组选取版本，返回确定序的 PaperGroup 列表。

    priority: 供应方优先级（高 -> 低），未列入的供应方并列最低。
    """
    rank_of = {}
    for i, s in enumerate(priority):
        key = str(s).strip().casefold()
        if key and key not in rank_of:
            rank_of[key] = i
    default_rank = len(rank_of)

    groups = {}
    for line in ok_lines:
        gkey, kind = group_key_of(line)
        g = groups.get(gkey)
        if g is None:
            g = groups[gkey] = PaperGroup(group_key=gkey, group_kind=kind)
        rank = rank_of.get(line.supplier_norm, default_rank)
        g.versions.append(Version(line=line, content_hash=content_fingerprint(line), rank=rank))

    out = []
    for gkey in sorted(groups):
        g = groups[gkey]
        # 确定序：优先级名次 -> 供应方规范键 -> 论文号规范键 -> 输入行号（仅稳定排序用）
        g.versions.sort(key=lambda v: (v.rank, v.line.supplier_norm,
                                       v.line.paper_id_norm, v.line.line_no))
        out.append(select_group(g))
    return out
