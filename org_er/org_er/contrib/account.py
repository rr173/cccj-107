"""贡献核算：每篇论文总贡献 1，精确有理数（Fraction）分摊。

规则：
  1) 论文总贡献 1 均分给全部作者：作者份额 = 1 / 作者数；
  2) 作者份额再均分给他能定位到的不同机构：同一机构的多条署名先去重再分；
  3) 部分署名能定位：只在已定位机构间分配，未定位引用保留待核查；
  4) 署名存在但都定位不到，或作者没有署名：作者整份记入待归属项；
  5) 隔离论文不计贡献。

机构身份完全来自指定轮次的 entity_members（record_key -> entity_id），
本作业不做任何机构同一性猜测。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from .dedup import PaperGroup
from .normalize import affil_record_key


@dataclass
class ShareTarget:
    target_kind: str              # 'entity' | 'unassigned'
    entity_id: str                # unassigned 时为 ''
    share: Fraction


@dataclass
class AuthorAlloc:
    group_key: str
    year: int
    author_pos: int               # 1 起，按被选取版本的作者顺序
    author_name: str
    n_authors: int
    author_share: Fraction        # 1 / n_authors
    affil_refs: list              # 原始署名引用 [{"source","entry"}...]（去重前）
    located_entities: list        # 定位到的不同机构 entity_id（按引用出现序去重）
    unlocated_refs: list          # 未定位引用 [{"source","entry","record_key"}...]
    disposition: str              # 'located' | 'unassigned_no_affils' | 'unassigned_none_located'
    division_basis: str           # 每次除法的依据（人读）
    targets: list = field(default_factory=list)  # ShareTarget


@dataclass
class AccountResult:
    authors: list = field(default_factory=list)          # AuthorAlloc
    institution_year: dict = field(default_factory=dict)  # (entity_id, year) -> [Fraction, n_shares, set(groups)]
    unassigned_year: dict = field(default_factory=dict)   # year -> [Fraction, n_authors]


def allocate_author(group_key: str, year: int, pos: int, author,
                    n_authors: int, entity_map: dict) -> AuthorAlloc:
    """对单个作者做定位与分摊。entity_map: record_key -> entity_id（指定轮次）。"""
    share = Fraction(1, n_authors)
    affil_refs = [{"source": r.source, "entry": r.entry} for r in author.affils]

    located, unlocated = [], []
    seen_entities = set()
    for r in author.affils:
        rk = affil_record_key(r.source, r.entry)
        eid = entity_map.get(rk)
        if eid is None:
            unlocated.append({"source": r.source, "entry": r.entry, "record_key": rk})
        elif eid not in seen_entities:  # 同一机构多条署名：先去重
            seen_entities.add(eid)
            located.append(eid)

    alloc = AuthorAlloc(
        group_key=group_key, year=year, author_pos=pos,
        author_name=author.name, n_authors=n_authors, author_share=share,
        affil_refs=affil_refs, located_entities=located, unlocated_refs=unlocated,
        disposition="", division_basis="",
    )
    base = f"作者均分 1/{n_authors}"
    if located:
        per = share / len(located)
        alloc.disposition = "located"
        if len(located) == 1:
            alloc.division_basis = f"{base}；定位到 1 个机构，独得 {per}"
        else:
            alloc.division_basis = f"{base}；定位到 {len(located)} 个不同机构（去重后），各得 {share}/{len(located)}={per}"
        alloc.targets = [ShareTarget("entity", eid, per) for eid in located]
    else:
        alloc.disposition = "unassigned_no_affils" if not author.affils else "unassigned_none_located"
        reason = "无署名" if not author.affils else f"{len(author.affils)} 条署名均未定位"
        alloc.division_basis = f"{base}；{reason}，整份 {share} 记入待归属"
        alloc.targets = [ShareTarget("unassigned", "", share)]
    return alloc


def account_groups(groups: list, entity_map: dict) -> AccountResult:
    """对全部计入组执行核算并汇总（隔离组跳过）。精确分数累计，不用小数。"""
    result = AccountResult()
    for g in groups:
        if g.status != "counted":
            continue  # 隔离论文不计贡献
        line = g.selected.line
        n = len(line.authors)
        for pos, author in enumerate(line.authors, start=1):
            alloc = allocate_author(g.group_key, line.year, pos, author, n, entity_map)
            result.authors.append(alloc)
            for t in alloc.targets:
                if t.target_kind == "entity":
                    slot = result.institution_year.setdefault((t.entity_id, line.year),
                                                              [Fraction(0), 0, set()])
                    slot[0] += t.share
                    slot[1] += 1
                    slot[2].add(g.group_key)
                else:
                    slot = result.unassigned_year.setdefault(line.year, [Fraction(0), 0])
                    slot[0] += t.share
                    slot[1] += 1
    return result
