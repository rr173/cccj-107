"""核算作业的 DuckDB 存储与 CSV 导出。

所有表以 contrib_ 为前缀，建在作业自己的库文件中；机构解析库只以
READ_ONLY 方式 ATTACH，本模块绝不写入它。
分子/分母用 HUGEINT 保存精确有理数，不用浮点累计。
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

TABLES = [
    "contrib_lines", "contrib_groups", "contrib_group_members",
    "contrib_authors", "contrib_share_targets",
    "contrib_institution_year", "contrib_unassigned_year", "contrib_meta",
]

DDL = [
    """
    CREATE TABLE contrib_lines (
      line_no INTEGER, status TEXT, reject_reason TEXT,
      supplier TEXT, paper_id TEXT, doi_raw TEXT, doi_norm TEXT,
      year_raw TEXT, year INTEGER, n_authors INTEGER, raw_json TEXT
    )
    """,
    """
    CREATE TABLE contrib_groups (
      group_key TEXT PRIMARY KEY, group_kind TEXT, status TEXT, n_versions INTEGER,
      selected_supplier TEXT, selected_paper_id TEXT, selected_year INTEGER,
      select_reason TEXT, version_diffs_json TEXT
    )
    """,
    """
    CREATE TABLE contrib_group_members (
      group_key TEXT, supplier TEXT, paper_id TEXT, line_no INTEGER,
      priority_rank INTEGER, content_hash TEXT, is_selected BOOLEAN
    )
    """,
    """
    CREATE TABLE contrib_authors (
      group_key TEXT, author_pos INTEGER, author_name TEXT, year INTEGER,
      n_authors INTEGER, share_num HUGEINT, share_den HUGEINT,
      affil_refs_json TEXT, located_entities_json TEXT, unlocated_refs_json TEXT,
      n_distinct_located INTEGER, disposition TEXT, division_basis TEXT
    )
    """,
    """
    CREATE TABLE contrib_share_targets (
      group_key TEXT, author_pos INTEGER, target_kind TEXT, entity_id TEXT,
      share_num HUGEINT, share_den HUGEINT
    )
    """,
    """
    CREATE TABLE contrib_institution_year (
      entity_id TEXT, year INTEGER, total_num HUGEINT, total_den HUGEINT,
      n_shares INTEGER, n_papers INTEGER
    )
    """,
    """
    CREATE TABLE contrib_unassigned_year (
      year INTEGER, total_num HUGEINT, total_den HUGEINT, n_authors INTEGER
    )
    """,
    "CREATE TABLE contrib_meta (k TEXT PRIMARY KEY, v TEXT)",
]


def open_out_db(path):
    return duckdb.connect(str(path))


def attach_er_readonly(con, er_db_path, alias="er"):
    """以只读方式挂载机构解析库；本作业对它只读不写。"""
    esc = str(er_db_path).replace("'", "''")
    con.execute(f"ATTACH '{esc}' AS {alias} (READ_ONLY)")


def available_rounds(con, alias="er"):
    return [r[0] for r in con.execute(
        f"SELECT DISTINCT round_no FROM {alias}.entity_members ORDER BY round_no").fetchall()]


def load_entity_map(con, round_no: int, alias="er") -> dict:
    """读取指定轮次的归并结果 record_key -> entity_id（只读对照）。"""
    rows = con.execute(
        f"SELECT record_key, entity_id FROM {alias}.entity_members WHERE round_no = ?",
        [round_no]).fetchall()
    if not rows:
        raise ValueError(f"机构库中没有 round_no={round_no} 的归并结果"
                         f"（可用轮次: {available_rounds(con, alias)}）")
    return dict(rows)


def reset_tables(con):
    """本作业库单次运行语义：重跑先清空全部 contrib_ 表（幂等）。"""
    for t in TABLES:
        con.execute(f"DROP TABLE IF EXISTS {t}")
    for stmt in DDL:
        con.execute(stmt)


def _em(con, sql, rows):
    rows = list(rows)
    if rows:
        con.executemany(sql, rows)


def write_run(con, *, meta: dict, lines: list, groups: list, account) -> None:
    """把一次运行的全部结果落库。"""
    _em(con, "INSERT INTO contrib_meta VALUES (?, ?)",
        [(k, str(v)) for k, v in sorted(meta.items())])

    _em(con, "INSERT INTO contrib_lines VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(l.line_no, l.status, l.reject_reason, l.supplier, l.paper_id,
          l.doi_raw, l.doi_norm, l.year_raw, l.year if l.status == "ok" else None,
          len(l.authors), l.raw) for l in lines])

    _em(con, "INSERT INTO contrib_groups VALUES (?,?,?,?,?,?,?,?,?)",
        [(g.group_key, g.group_kind, g.status, len(g.versions),
          g.selected.line.supplier if g.selected else None,
          g.selected.line.paper_id if g.selected else None,
          g.selected.line.year if g.selected else None,
          g.select_reason,
          json.dumps(g.version_diffs, ensure_ascii=False, sort_keys=True))
         for g in groups])

    _em(con, "INSERT INTO contrib_group_members VALUES (?,?,?,?,?,?,?)",
        [(g.group_key, v.line.supplier, v.line.paper_id, v.line.line_no,
          v.rank, v.content_hash, v.is_selected)
         for g in groups for v in g.versions])

    _em(con, "INSERT INTO contrib_authors VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(a.group_key, a.author_pos, a.author_name, a.year, a.n_authors,
          a.author_share.numerator, a.author_share.denominator,
          json.dumps(a.affil_refs, ensure_ascii=False),
          json.dumps(a.located_entities, ensure_ascii=False),
          json.dumps(a.unlocated_refs, ensure_ascii=False),
          len(a.located_entities), a.disposition, a.division_basis)
         for a in account.authors])

    _em(con, "INSERT INTO contrib_share_targets VALUES (?,?,?,?,?,?)",
        [(a.group_key, a.author_pos, t.target_kind, t.entity_id,
          t.share.numerator, t.share.denominator)
         for a in account.authors for t in a.targets])

    _em(con, "INSERT INTO contrib_institution_year VALUES (?,?,?,?,?,?)",
        [(eid, year, slot[0].numerator, slot[0].denominator, slot[1], len(slot[2]))
         for (eid, year), slot in sorted(account.institution_year.items())])

    _em(con, "INSERT INTO contrib_unassigned_year VALUES (?,?,?,?)",
        [(year, slot[0].numerator, slot[0].denominator, slot[1])
         for year, slot in sorted(account.unassigned_year.items())])


def export_csvs(con, outdir) -> Path:
    """导出交付表：逐位作者分摊明细、机构×年份汇总、待归属、去重来源、隔离与坏行。"""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    def cp(sql, name):
        con.execute(f"COPY ({sql}) TO '{out / name}' (HEADER, DELIMITER ',')")

    # 逐位作者分摊明细（含去重前后机构列表与每次除法依据）
    cp("""
      SELECT a.group_key, a.year, g.selected_supplier, g.selected_paper_id,
             substring(g.group_key, length(g.group_kind) + 3) AS dedup_key,
             a.author_pos, a.author_name,
             a.share_num AS author_share_num, a.share_den AS author_share_den,
             a.affil_refs_json AS affil_refs_before_dedup,
             a.located_entities_json AS located_entities_after_dedup,
             a.unlocated_refs_json AS unlocated_refs,
             t.target_kind, t.entity_id,
             t.share_num, t.share_den,
             t.share_num || '/' || t.share_den AS share_text,
             a.division_basis
      FROM contrib_authors a
      JOIN contrib_share_targets t USING (group_key, author_pos)
      JOIN contrib_groups g USING (group_key)
      ORDER BY a.group_key, a.author_pos, t.target_kind, t.entity_id
    """, "contrib_author_details.csv")

    # 按机构、年份汇总的贡献量
    cp("""
      SELECT entity_id, year, total_num, total_den,
             total_num || '/' || total_den AS total_text,
             ROUND(total_num * 1.0 / total_den, 6) AS total_decimal_display,
             n_shares, n_papers
      FROM contrib_institution_year ORDER BY year, entity_id
    """, "contrib_institution_year.csv")

    cp("""
      SELECT year, total_num, total_den,
             total_num || '/' || total_den AS total_text,
             ROUND(total_num * 1.0 / total_den, 6) AS total_decimal_display,
             n_authors
      FROM contrib_unassigned_year ORDER BY year
    """, "contrib_unassigned_year.csv")

    # 论文去重结果（含选取理由与版本差异）
    cp("""
      SELECT group_key, group_kind, status, n_versions,
             selected_supplier, selected_paper_id, selected_year,
             select_reason, version_diffs_json
      FROM contrib_groups ORDER BY group_key
    """, "contrib_papers.csv")

    # 去重来源：每组的全部版本
    cp("""
      SELECT group_key, supplier, paper_id, line_no, priority_rank,
             content_hash, is_selected
      FROM contrib_group_members
      ORDER BY group_key, priority_rank, supplier, paper_id
    """, "contrib_paper_versions.csv")

    cp("""
      SELECT group_key, n_versions, select_reason AS quarantine_reason, version_diffs_json
      FROM contrib_groups WHERE status = 'quarantined' ORDER BY group_key
    """, "contrib_quarantine.csv")

    cp("""
      SELECT line_no, supplier, paper_id, reject_reason, raw_json
      FROM contrib_lines WHERE status = 'rejected' ORDER BY line_no
    """, "contrib_rejected_lines.csv")

    return out
