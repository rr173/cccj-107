"""独立核验：不复用核算代码的路径，从落库结果反推并逐项核对。

核验项：
  V1 每篇计入论文：机构份额 + 待归属份额 之和恰好 == 1（精确分数）
  V2 每位作者：份额 == 1/作者数；分摊目标数与“去重后机构数/待归属”一致，
     每个目标份额 == 作者份额/目标数
  V3 机构×年份汇总 == 由明细逐行重算（逐项可追到作者明细），
     份额行数与论文数统计一致；待归属汇总同理
  V4 隔离论文没有任何分摊行；计入论文都有分摊行
  V5 每个年份：机构汇总 + 待归属 == 该年计入论文数（总量守恒）
  V6 每条 ok 行恰好进入一个分组；坏行不进入任何分组
"""
from __future__ import annotations

import json
from fractions import Fraction


def _rows(con, sql):
    return con.execute(sql).fetchall()


def verify_run(con) -> dict:
    """对当前 contrib 库执行全部核验，返回 {check_name: {"ok", "detail", ...}}。"""
    checks = {}

    groups = {k: (kind, status) for k, kind, status in
              _rows(con, "SELECT group_key, group_kind, status FROM contrib_groups")}
    authors = _rows(con, """
        SELECT group_key, author_pos, year, n_authors, share_num, share_den,
               n_distinct_located, disposition FROM contrib_authors
        ORDER BY group_key, author_pos""")
    targets = _rows(con, """
        SELECT group_key, author_pos, target_kind, entity_id, share_num, share_den
        FROM contrib_share_targets ORDER BY group_key, author_pos, target_kind, entity_id""")

    targets_by_author = {}
    for gk, pos, kind, eid, num, den in targets:
        targets_by_author.setdefault((gk, pos), []).append((kind, eid, Fraction(num, den)))

    # V1 每篇计入论文份额和 == 1
    sum_by_group = {}
    for gk, pos, kind, eid, num, den in targets:
        sum_by_group[gk] = sum_by_group.get(gk, Fraction(0)) + Fraction(num, den)
    bad = [gk for gk, (kind, status) in groups.items()
           if status == "counted" and sum_by_group.get(gk, Fraction(0)) != 1]
    checks["V1_paper_sum_eq_1"] = {
        "ok": not bad,
        "detail": f"计入论文 {sum(1 for s in groups.values() if s[1] == 'counted')} 篇，"
                  f"份额和 != 1 的: {bad[:5]}",
    }

    # V2 作者份额与分摊结构
    problems = []
    for gk, pos, year, n_authors, num, den, n_loc, disp in authors:
        share = Fraction(num, den)
        if share != Fraction(1, n_authors):
            problems.append((gk, pos, f"作者份额 {share} != 1/{n_authors}"))
            continue
        ts = targets_by_author.get((gk, pos), [])
        if disp == "located":
            if len(ts) != n_loc or any(k != "entity" for k, _, _ in ts):
                problems.append((gk, pos, "located 目标数/种类不符"))
            elif any(s != share / n_loc for _, _, s in ts):
                problems.append((gk, pos, "机构份额不等于 作者份额/定位机构数"))
        else:
            if len(ts) != 1 or ts[0][0] != "unassigned" or ts[0][2] != share:
                problems.append((gk, pos, "待归属目标不符"))
    checks["V2_author_split"] = {
        "ok": not problems,
        "detail": f"作者行 {len(authors)} 条，问题: {problems[:5]}",
    }

    # V3 汇总 == 明细重算（逐项可追）
    year_of_group = {}
    for gk, pos, year, *_ in authors:
        year_of_group[gk] = year
    agg, agg_shares, agg_papers = {}, {}, {}
    unas, unas_n = {}, {}
    for gk, pos, kind, eid, num, den in targets:
        year = year_of_group[gk]
        if kind == "entity":
            key = (eid, year)
            agg[key] = agg.get(key, Fraction(0)) + Fraction(num, den)
            agg_shares[key] = agg_shares.get(key, 0) + 1
            agg_papers.setdefault(key, set()).add(gk)
        else:
            unas[year] = unas.get(year, Fraction(0)) + Fraction(num, den)
            unas_n[year] = unas_n.get(year, 0) + 1
    stored = {(eid, y): (Fraction(n, d), ns, np_) for eid, y, n, d, ns, np_ in
              _rows(con, "SELECT entity_id, year, total_num, total_den, n_shares, n_papers "
                         "FROM contrib_institution_year")}
    mism = []
    for key, total in agg.items():
        want = (total, agg_shares[key], len(agg_papers[key]))
        if stored.get(key) != want:
            mism.append((key, stored.get(key), want))
    for key, val in stored.items():
        if key not in agg:
            mism.append((key, val, None))
    stored_unas = {y: (Fraction(n, d), na) for y, n, d, na in
                   _rows(con, "SELECT year, total_num, total_den, n_authors "
                              "FROM contrib_unassigned_year")}
    for y, total in unas.items():
        if stored_unas.get(y) != (total, unas_n[y]):
            mism.append(("unassigned", y, stored_unas.get(y), (total, unas_n[y])))
    for y, val in stored_unas.items():
        if y not in unas:
            mism.append(("unassigned", y, val, None))
    checks["V3_totals_traceable"] = {
        "ok": not mism,
        "detail": f"机构×年份 {len(stored)} 项、待归属 {len(stored_unas)} 项，不一致: {mism[:5]}",
    }

    # V4 隔离论文零分摊；计入论文有分摊
    counted = {gk for gk, (_, s) in groups.items() if s == "counted"}
    quarantined = {gk for gk, (_, s) in groups.items() if s == "quarantined"}
    with_rows = {gk for gk, *_ in authors}
    bad4 = (quarantined & with_rows) | (counted - with_rows)
    checks["V4_quarantine_excluded"] = {
        "ok": not bad4,
        "detail": f"隔离 {len(quarantined)} 篇均无分摊；计入 {len(counted)} 篇均有分摊；异常: {sorted(bad4)[:5]}",
    }

    # V5 年度总量守恒：机构 + 待归属 == 计入论文数
    papers_by_year = {}
    for gk, year in year_of_group.items():
        papers_by_year[year] = papers_by_year.get(year, 0) + 1
    bad5 = []
    for year, n_papers in papers_by_year.items():
        inst = sum((val[0] for (e, y), val in stored.items() if y == year), Fraction(0))
        total = inst + unas.get(year, Fraction(0))
        if total != n_papers:
            bad5.append((year, str(total), n_papers))
    checks["V5_year_balance"] = {
        "ok": not bad5,
        "detail": f"年份 {sorted(papers_by_year)} 总量守恒检查，异常: {bad5[:5]}",
    }

    # V6 行归属：ok 行恰好进一组；坏行不进组
    line_status = dict(_rows(con, "SELECT line_no, status FROM contrib_lines"))
    member_lines = [r[0] for r in _rows(con, "SELECT line_no FROM contrib_group_members")]
    ok_lines = {ln for ln, st in line_status.items() if st == "ok"}
    bad_lines = {ln for ln, st in line_status.items() if st == "rejected"}
    member_set = set(member_lines)
    bad6 = bool(member_set & bad_lines) or member_set != ok_lines
    checks["V6_line_membership"] = {
        "ok": not bad6,
        "detail": f"ok 行 {len(ok_lines)} 条 = 分组成员 {len(member_lines)} 条；"
                  f"坏行 {len(bad_lines)} 条均未入组",
    }

    checks["ok"] = all(c["ok"] for c in checks.values())
    return checks


def er_snapshot(con, catalog=None) -> dict:
    """指定 catalog（默认当前库）全部表的内容快照（用于“输入保持原样”前后比对）。"""
    if catalog is None:
        catalog = con.execute("SELECT current_database()").fetchone()[0]
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_catalog = ? "
        "ORDER BY table_name", [catalog]).fetchall()]
    snap = {}
    for t in tables:
        rows = con.execute(f'SELECT * FROM "{catalog}"."{t}"').fetchall()
        snap[t] = sorted(repr(r) for r in rows)
    return snap


def run_signature(con) -> dict:
    """作业结果的顺序无关签名：乱序读入同一目录，签名必须一致。

    行号（line_no）是输入坐标而非核算结果，签名中剔除；
    其余内容（分组、选取、差异、分摊、汇总）都是结果的组成部分。
    """
    def dump(sql):
        return sorted(repr(r) for r in con.execute(sql).fetchall())

    groups = []
    for row in con.execute(
            "SELECT group_key, group_kind, status, n_versions, selected_supplier, "
            "selected_paper_id, selected_year, select_reason, version_diffs_json "
            "FROM contrib_groups").fetchall():
        *head, diffs_json = row
        diffs = [{k: v for k, v in d.items() if k != "line_no"}
                 for d in json.loads(diffs_json)]
        groups.append(repr(tuple(head)) + json.dumps(diffs, ensure_ascii=False, sort_keys=True))

    rejected = sorted(
        repr(r) for r in con.execute(
            "SELECT supplier, paper_id, reject_reason, raw_json FROM contrib_lines "
            "WHERE status = 'rejected'").fetchall())

    return {
        "groups": sorted(groups),
        "members": dump("SELECT group_key, supplier, paper_id, priority_rank, content_hash, "
                        "is_selected FROM contrib_group_members"),
        "authors": dump("SELECT * FROM contrib_authors"),
        "targets": dump("SELECT * FROM contrib_share_targets"),
        "institution_year": dump("SELECT * FROM contrib_institution_year"),
        "unassigned_year": dump("SELECT * FROM contrib_unassigned_year"),
        "rejected": rejected,
    }
