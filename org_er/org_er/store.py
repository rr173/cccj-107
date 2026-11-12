"""DuckDB 存储：表结构、读写、导出。

表：
  raw_records       当前全部原始条目（修订事件作用于本表）
  records           规范化后的条目（raw + norm 并存）
  name_tokens       名称 token 倒排（国家, token -> 条目）
  candidates        阻塞候选对及规则评估结果（按轮次保留）
  edges             规则命中的边：接受/拒绝 + 证据
  entity_members    实体成员表（按轮次保留历史）
  entity_migrations 实体标识迁移映射（旧 -> 新）
  revision_log      修订事件日志
  run_stats         每轮统计（候选数、节省率等）
  meta              当前轮次等元信息
"""
from __future__ import annotations

from pathlib import Path

import duckdb

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS raw_records (
      record_key TEXT PRIMARY KEY,
      source TEXT, source_key TEXT, scenario TEXT,
      legal_name TEXT, country_code TEXT, website_host TEXT, registration_no TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS records (
      record_key TEXT PRIMARY KEY,
      source TEXT, source_key TEXT, scenario TEXT,
      legal_name_raw TEXT, country_raw TEXT, website_host_raw TEXT, registration_no_raw TEXT,
      legal_name TEXT, country_code TEXT, website_host TEXT, registration_no TEXT,
      regno_valid BOOLEAN, host_evidence BOOLEAN, host_exclusion TEXT, tokens_json TEXT
    )
    """,
    "CREATE TABLE IF NOT EXISTS name_tokens (record_key TEXT, country_code TEXT, token TEXT)",
    """
    CREATE TABLE IF NOT EXISTS candidates (
      round_no INTEGER, key_a TEXT, key_b TEXT,
      via_token BOOLEAN, via_regno BOOLEAN, via_domain BOOLEAN,
      rule_fired TEXT, score DOUBLE, notes TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS edges (
      round_no INTEGER, key_a TEXT, key_b TEXT, edge_type TEXT, score DOUBLE,
      status TEXT, redundant BOOLEAN, reason TEXT, evidence_json TEXT
    )
    """,
    "CREATE TABLE IF NOT EXISTS entity_members (round_no INTEGER, entity_id TEXT, record_key TEXT)",
    """
    CREATE TABLE IF NOT EXISTS entity_migrations (
      round_no INTEGER, record_key TEXT, old_entity_id TEXT, new_entity_id TEXT, change_type TEXT
    )
    """,
    "CREATE TABLE IF NOT EXISTS revision_log (round_no INTEGER, op TEXT, record_key TEXT, payload TEXT)",
    "CREATE TABLE IF NOT EXISTS run_stats (round_no INTEGER, k TEXT, v TEXT)",
    "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
]


def open_db(path):
    return duckdb.connect(str(path))


def executemany(con, sql, rows):
    """DuckDB 2.x 的 executemany 不接受空参数列表，统一在此判空。"""
    rows = list(rows)
    if rows:
        con.executemany(sql, rows)


def init_db(con):
    for stmt in DDL_STATEMENTS:
        con.execute(stmt)


def get_meta(con, key, default=None):
    row = con.execute("SELECT v FROM meta WHERE k = ?", [key]).fetchone()
    return row[0] if row else default


def set_meta(con, key, value):
    con.execute("DELETE FROM meta WHERE k = ?", [key])
    con.execute("INSERT INTO meta VALUES (?, ?)", [key, str(value)])


def upsert_raw(con, record_key: str, rec: dict):
    """插入或替换一条原始条目（rec 为原始字段字典）。"""
    con.execute("DELETE FROM raw_records WHERE record_key = ?", [record_key])
    con.execute(
        "INSERT INTO raw_records VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [record_key, rec.get("source", ""), rec.get("source_key", ""), rec.get("scenario", ""),
         rec.get("legal_name", ""), rec.get("country_code", ""),
         rec.get("website_host", ""), rec.get("registration_no", "")],
    )


def retract_raw(con, record_key: str):
    con.execute("DELETE FROM raw_records WHERE record_key = ?", [record_key])


def read_raw_rows(con):
    """按确定顺序读出全部原始条目。"""
    return con.execute(
        "SELECT record_key, source, source_key, scenario, legal_name, country_code, "
        "website_host, registration_no FROM raw_records ORDER BY record_key"
    ).fetchall()


def export_outputs(con, outdir, round_no: int):
    """导出交付表：实体成员表、有效匹配边（含证据）、拒绝边、候选、迁移映射、统计。"""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)

    def cp(sql, name):
        con.execute(f"COPY ({sql}) TO '{out / name}' (HEADER, DELIMITER ',')")

    cp(f"SELECT entity_id, record_key FROM entity_members WHERE round_no = {round_no} "
       f"ORDER BY entity_id, record_key", "entity_members.csv")
    cp(f"SELECT key_a, key_b, edge_type, score, redundant, evidence_json "
       f"FROM edges WHERE round_no = {round_no} AND status = 'accepted' "
       f"ORDER BY edge_type, key_a, key_b", "match_edges.csv")
    cp(f"SELECT key_a, key_b, edge_type, score, reason, evidence_json "
       f"FROM edges WHERE round_no = {round_no} AND status = 'rejected' "
       f"ORDER BY key_a, key_b", "rejected_edges.csv")
    cp(f"SELECT key_a, key_b, via_token, via_regno, via_domain, rule_fired, score, notes "
       f"FROM candidates WHERE round_no = {round_no} ORDER BY key_a, key_b", "candidates.csv")
    cp("SELECT round_no, record_key, old_entity_id, new_entity_id, change_type "
       "FROM entity_migrations ORDER BY round_no, record_key", "entity_migrations.csv")
    cp("SELECT round_no, k, v FROM run_stats ORDER BY round_no, k", "run_stats.csv")
    return out
