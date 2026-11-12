"""核算作业的 DuckDB 存储、暂存/原子发布、状态日志与 CSV 导出。

设计要点
--------
* **精确分数用任意精度整数**：分子/分母列为 DuckDB ``BIGNUM``（任意精度十进制整数），
  运行期在 Python 里用 ``fractions.Fraction`` 计算。累计结果超过 int64/int128 也不会
  截断、舍入成浮点数或被拒绝。展示用小数单独放在 ``*_decimal_approx`` 列，列名和
  ``*_decimal_note`` 常量列都明确标注“仅近似展示，精确值看分子/分母”。
* **一次运行先暂存、后切换**：全部结果先写入暂存 schema（``contrib_stage_<batch>``），
  在暂存表上做独立核验、导出到临时目录并核验 CSV，最后用**一个事务**把暂存表切换为
  当前表并记下状态。任何一步失败或进程终止，原来的当前表与上一份 CSV 都保持完整。
* **状态日志**：``contrib_state`` 记录 running / committed / ready 三个阶段及批次号、
  暂存 schema、临时 CSV 目录、备份目录。下次启动先自动恢复（``recover_pending``），
  不要求研究人员手工拼库或挑 CSV。
* **CSV 目录按批次整体切换**：导出到 ``<out>.staging-<batch>``，提交数据库后再把
  整个目录换入 ``<out>``；每一步都有确定性的可续做规则，并在目录里写 ``manifest``。
* **旧格式兼容**：旧版 HUGEINT 小账目可识别、独立核验，再用同一条暂存/切换链路
  无损转换成 BIGNUM 格式并重新导出；转换前先做数据库文件备份，转换在事务中进行，
  失败则回滚，原账目保持不动。
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from decimal import Decimal, localcontext
from pathlib import Path

import duckdb

# 结果表；contrib_state 不在其中（状态表不随批次切换）
TABLES = [
    "contrib_lines", "contrib_groups", "contrib_group_members",
    "contrib_authors", "contrib_share_targets",
    "contrib_institution_year", "contrib_unassigned_year", "contrib_meta",
]

STATE_TABLE = "contrib_state"
STAGE_PREFIX = "contrib_stage_"
FORMAT_VERSION = "2"
FRACTION_FORMAT = "arbitrary_precision_decimal_integer"
DECIMAL_NOTE = "approx_display_only"

# 旧版（HUGEINT）结果表的分数列，用于识别与迁移
LEGACY_FRACTION_COLUMNS = {
    "contrib_authors": ["share_num", "share_den"],
    "contrib_share_targets": ["share_num", "share_den"],
    "contrib_institution_year": ["total_num", "total_den"],
    "contrib_unassigned_year": ["total_num", "total_den"],
}

DDL = [
    """
    CREATE TABLE {t}.contrib_lines (
      line_no INTEGER, status TEXT, reject_reason TEXT,
      supplier TEXT, paper_id TEXT, doi_raw TEXT, doi_norm TEXT,
      year_raw TEXT, year INTEGER, n_authors INTEGER, raw_json TEXT
    )
    """,
    """
    CREATE TABLE {t}.contrib_groups (
      group_key TEXT PRIMARY KEY, group_kind TEXT, status TEXT, n_versions INTEGER,
      selected_supplier TEXT, selected_paper_id TEXT, selected_year INTEGER,
      select_reason TEXT, version_diffs_json TEXT
    )
    """,
    """
    CREATE TABLE {t}.contrib_group_members (
      group_key TEXT, supplier TEXT, paper_id TEXT, line_no INTEGER,
      priority_rank INTEGER, content_hash TEXT, is_selected BOOLEAN
    )
    """,
    """
    CREATE TABLE {t}.contrib_authors (
      group_key TEXT, author_pos INTEGER, author_name TEXT, year INTEGER,
      n_authors INTEGER,
      share_num BIGNUM, share_den BIGNUM,
      share_text TEXT, share_decimal_approx TEXT,
      affil_refs_json TEXT, located_entities_json TEXT, unlocated_refs_json TEXT,
      n_distinct_located INTEGER, disposition TEXT, division_basis TEXT
    )
    """,
    """
    CREATE TABLE {t}.contrib_share_targets (
      group_key TEXT, author_pos INTEGER, target_kind TEXT, entity_id TEXT,
      share_num BIGNUM, share_den BIGNUM
    )
    """,
    """
    CREATE TABLE {t}.contrib_institution_year (
      entity_id TEXT, year INTEGER,
      total_num BIGNUM, total_den BIGNUM,
      total_text TEXT, total_decimal_approx TEXT, total_decimal_note TEXT,
      n_shares INTEGER, n_papers INTEGER
    )
    """,
    """
    CREATE TABLE {t}.contrib_unassigned_year (
      year INTEGER,
      total_num BIGNUM, total_den BIGNUM,
      total_text TEXT, total_decimal_approx TEXT, total_decimal_note TEXT,
      n_authors INTEGER
    )
    """,
    """
    CREATE TABLE {t}.contrib_meta (
      k TEXT PRIMARY KEY, v TEXT
    )
    """,
]

STATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
  id INTEGER PRIMARY KEY,
  status TEXT,                 -- running | committed | ready | failed
  batch_id TEXT,               -- 正在运行/已提交/当前的批次
  previous_batch_id TEXT,      -- 开始本次运行前的成功批次（用于说明保住了哪一份）
  staging_schema TEXT,         -- 暂存 schema 名
  out_dir TEXT,                -- CSV 交付目录
  staging_csv_dir TEXT,        -- 临时 CSV 目录
  prev_csv_dir TEXT,           -- 切换时旧目录的备份名
  started_at TEXT,
  updated_at TEXT,
  message TEXT
)
"""


# ---------------------------------------------------------------- 连接与路径
def is_file_db(path) -> bool:
    return bool(path) and str(path) != ":memory:"


def open_out_db(path):
    if is_file_db(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def open_readonly(path):
    return duckdb.connect(str(path), read_only=True)


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


# ---------------------------------------------------------------- 格式识别
def _table_columns(con, schema="main"):
    rows = con.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_catalog = current_database() AND table_schema = ? "
        "ORDER BY table_name, ordinal_position", [schema]).fetchall()
    cols = {}
    for t, c, typ in rows:
        cols.setdefault(t, {})[c] = typ.upper()
    return cols


def _main_tables(con, schema="main"):
    return {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_catalog = current_database() AND table_schema = ? AND table_type = 'BASE TABLE'",
        [schema]).fetchall()}


def detect_format(con, schema="main") -> str:
    """返回 'empty' | 'legacy_hugeint' | 'bignum_v2' | 'mixed' | 'unknown'。

    'empty' 表示库里还没有任何 contrib 结果表（新作业库/旧结构都没有）。
    """
    cols = _table_columns(con, schema)
    if not cols:
        return "empty"
    if not (set(TABLES) & set(cols)):
        return "unknown"
    types = []
    for table, fcols in LEGACY_FRACTION_COLUMNS.items():
        for c in fcols:
            if c in cols.get(table, {}):
                types.append(cols[table][c])
    if not types:
        return "unknown"
    type_set = set(types)
    if type_set == {"BIGNUM"}:
        return "bignum_v2"
    if type_set <= {"HUGEINT", "BIGINT", "INTEGER"}:
        return "legacy_hugeint"
    if type_set & {"BIGNUM"}:
        return "mixed"
    return "unknown"


def result_schema_of(con) -> str:
    """当前成功结果所在的 schema（本版本固定 main；保留扩展点）。"""
    return "main"


# ---------------------------------------------------------------- 状态日志
def state_exists(con) -> bool:
    return bool(con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_catalog = "
        "current_database() AND table_schema = 'main' AND table_name = ?",
        [STATE_TABLE]).fetchone()[0])


def ensure_state(con):
    con.execute(STATE_DDL)
    if con.execute(f"SELECT COUNT(*) FROM {STATE_TABLE}").fetchone()[0] == 0:
        now = _now()
        con.execute(
            f"INSERT INTO {STATE_TABLE} VALUES (1, 'ready', NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, '')",
            [now, now])


def get_state(con) -> dict:
    keys = ["status", "batch_id", "previous_batch_id", "staging_schema", "out_dir",
            "staging_csv_dir", "prev_csv_dir", "started_at", "updated_at", "message"]
    if not state_exists(con):
        # 旧版作业库没有状态表：说明没有未完成的运行
        return dict(zip(keys, ["ready"] + [None] * 8 + [""]))
    row = con.execute(
        f"SELECT status, batch_id, previous_batch_id, staging_schema, out_dir, "
        f"staging_csv_dir, prev_csv_dir, started_at, updated_at, message FROM {STATE_TABLE} "
        f"WHERE id = 1").fetchone()
    return dict(zip(keys, row))


def set_state(con, **kw):
    ensure_state(con)
    allowed = ["status", "batch_id", "previous_batch_id", "staging_schema", "out_dir",
               "staging_csv_dir", "prev_csv_dir", "started_at", "message"]
    sets, vals = [], []
    for k, v in kw.items():
        if k not in allowed:
            raise ValueError(f"未知状态字段: {k}")
        sets.append(f"{k} = ?")
        vals.append(v)
    sets.append("updated_at = ?")
    vals.append(_now())
    con.execute(f"UPDATE {STATE_TABLE} SET {', '.join(sets)} WHERE id = 1", vals)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_batch_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def get_meta(con, key, default=None, schema="main"):
    if "contrib_meta" not in _main_tables(con, schema):
        return default
    row = con.execute(f'SELECT v FROM "{schema}".contrib_meta WHERE k = ?', [key]).fetchone()
    return row[0] if row else default


# ---------------------------------------------------------------- 暂存表
def create_tables(con, schema: str):
    con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    for stmt in DDL:
        # DuckDB 的未加引号 schema 名会被当作“默认 schema”引用而报错，必须显式加引号
        con.execute(stmt.format(t=f'"{schema}"'))


def create_current_tables(con):
    for stmt in DDL:
        con.execute(stmt.format(t='"main"'))


def drop_stage(con, schema: str):
    con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def drop_all_stage_schemas(con):
    names = [r[0] for r in con.execute(
        "SELECT schema_name FROM duckdb_schemas() WHERE schema_name LIKE ?",
        [STAGE_PREFIX + "%"]).fetchall()]
    for n in names:
        con.execute(f'DROP SCHEMA IF EXISTS "{n}" CASCADE')
    return names


def swap_tables(con, stage_schema: str):
    """把暂存表切换为当前表（调用方负责事务）。

    先删旧当前表、按同一 DDL 重建，再从暂存整表复制，最后删掉暂存 schema。
    整个过程在一个事务内：任一步失败都整体回滚，旧表与旧数据照旧
    （DuckDB 的 DDL 在事务内可回滚）。列类型完全由 DDL 决定（BIGNUM）。
    """
    for t in TABLES:
        con.execute(f'DROP TABLE IF EXISTS "main"."{t}"')
    create_current_tables(con)
    for t in TABLES:
        con.execute(f'INSERT INTO "main"."{t}" SELECT * FROM "{stage_schema}"."{t}"')
    drop_stage(con, stage_schema)


# ---------------------------------------------------------------- 插入
def _em(con, sql, rows):
    rows = list(rows)
    if rows:
        con.executemany(sql, rows)


def approx_decimal(num: int, den: int) -> str:
    """展示用小数近似（与精确分子/分母分开存放，绝不回写精确列）。"""
    with localcontext() as ctx:
        ctx.prec = 40
        v = Decimal(int(num)) / Decimal(int(den))
    if v != 0 and (abs(v) >= Decimal("1e12") or abs(v) < Decimal("0.000001")):
        return f"{v:.6E}"
    return f"{v:.6f}"


def frac_text(num: int, den: int) -> str:
    return f"{int(num)}/{int(den)}"


def write_tables(con, schema: str, *, meta: dict, lines: list, groups: list, account) -> None:
    """把一次运行的全部结果写入暂存 schema（不提交）。"""
    t = f'"{schema}"'
    _em(con, f"INSERT INTO {t}.contrib_meta VALUES (?, ?)",
        [(k, str(v)) for k, v in sorted(meta.items())])

    _em(con, f"INSERT INTO {t}.contrib_lines VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(l.line_no, l.status, l.reject_reason, l.supplier, l.paper_id,
          l.doi_raw, l.doi_norm, l.year_raw, l.year if l.status == "ok" else None,
          len(l.authors), l.raw) for l in lines])

    _em(con, f"INSERT INTO {t}.contrib_groups VALUES (?,?,?,?,?,?,?,?,?)",
        [(g.group_key, g.group_kind, g.status, len(g.versions),
          g.selected.line.supplier if g.selected else None,
          g.selected.line.paper_id if g.selected else None,
          g.selected.line.year if g.selected else None,
          g.select_reason,
          json.dumps(g.version_diffs, ensure_ascii=False, sort_keys=True))
         for g in groups])

    _em(con, f"INSERT INTO {t}.contrib_group_members VALUES (?,?,?,?,?,?,?)",
        [(g.group_key, v.line.supplier, v.line.paper_id, v.line.line_no,
          v.rank, v.content_hash, v.is_selected)
         for g in groups for v in g.versions])

    _em(con, f"INSERT INTO {t}.contrib_authors VALUES "
             f"(?,?,?,?,?,?::BIGNUM,?::BIGNUM,?,?,?,?,?,?,?,?)",
        [(a.group_key, a.author_pos, a.author_name, a.year, a.n_authors,
          str(a.author_share.numerator), str(a.author_share.denominator),
          frac_text(a.author_share.numerator, a.author_share.denominator),
          approx_decimal(a.author_share.numerator, a.author_share.denominator),
          json.dumps(a.affil_refs, ensure_ascii=False),
          json.dumps(a.located_entities, ensure_ascii=False),
          json.dumps(a.unlocated_refs, ensure_ascii=False),
          len(a.located_entities), a.disposition, a.division_basis)
         for a in account.authors])

    fault_point("mid_write")  # 测试注入：写明细中途进程终止（事务未提交）

    _em(con, f"INSERT INTO {t}.contrib_share_targets VALUES (?,?,?,?,?::BIGNUM,?::BIGNUM)",
        [(a.group_key, a.author_pos, tgt.target_kind, tgt.entity_id,
          str(tgt.share.numerator), str(tgt.share.denominator))
         for a in account.authors for tgt in a.targets])

    _em(con, f"INSERT INTO {t}.contrib_institution_year VALUES (?,?,?::BIGNUM,?::BIGNUM,?,?,?,?,?)",
        [(eid, year, str(slot[0].numerator), str(slot[0].denominator),
          frac_text(slot[0].numerator, slot[0].denominator),
          approx_decimal(slot[0].numerator, slot[0].denominator), DECIMAL_NOTE,
          slot[1], len(slot[2]))
         for (eid, year), slot in sorted(account.institution_year.items())])

    _em(con, f"INSERT INTO {t}.contrib_unassigned_year VALUES (?,?::BIGNUM,?::BIGNUM,?,?,?,?)",
        [(year, str(slot[0].numerator), str(slot[0].denominator),
          frac_text(slot[0].numerator, slot[0].denominator),
          approx_decimal(slot[0].numerator, slot[0].denominator), DECIMAL_NOTE,
          slot[1])
         for year, slot in sorted(account.unassigned_year.items())])


# ---------------------------------------------------------------- 旧格式迁移
LEGACY_COPY_SQL = {
    "contrib_lines": "SELECT * FROM main.contrib_lines",
    "contrib_groups": "SELECT * FROM main.contrib_groups",
    "contrib_group_members": "SELECT * FROM main.contrib_group_members",
    "contrib_share_targets": (
        "SELECT group_key, author_pos, target_kind, entity_id, "
        "cast(share_num as BIGNUM), cast(share_den as BIGNUM) FROM main.contrib_share_targets"),
    "contrib_authors": (
        "SELECT group_key, author_pos, author_name, year, n_authors, "
        "cast(share_num as BIGNUM), cast(share_den as BIGNUM), "
        "cast(share_num as varchar) || '/' || cast(share_den as varchar), NULL, "
        "affil_refs_json, located_entities_json, unlocated_refs_json, "
        "n_distinct_located, disposition, division_basis FROM main.contrib_authors"),
    "contrib_institution_year": (
        "SELECT entity_id, year, cast(total_num as BIGNUM), cast(total_den as BIGNUM), "
        "cast(total_num as varchar) || '/' || cast(total_den as varchar), NULL, NULL, "
        "n_shares, n_papers FROM main.contrib_institution_year"),
    "contrib_unassigned_year": (
        "SELECT year, cast(total_num as BIGNUM), cast(total_den as BIGNUM), "
        "cast(total_num as varchar) || '/' || cast(total_den as varchar), NULL, NULL, "
        "n_authors FROM main.contrib_unassigned_year"),
}


def build_stage_from_legacy(con, stage_schema: str, *, batch_id: str, extra_meta: dict):
    """把旧版 HUGEINT 账目无损复制到暂存表（BIGNUM + 展示列），并补迁移元数据。"""
    create_tables(con, stage_schema)
    t = f'"{stage_schema}"'
    for tbl, sql in LEGACY_COPY_SQL.items():
        con.execute(f'INSERT INTO {t}.{tbl} {sql}')
    con.execute(f'INSERT INTO {t}.contrib_meta SELECT k, v FROM main.contrib_meta')

    # 展示列由精确分数在 Python 里计算，不经过浮点精确列
    for tbl, keys in (
            ("contrib_authors", ("group_key", "author_pos", "share_num", "share_den")),
            ("contrib_institution_year", ("entity_id", "year", "total_num", "total_den")),
            ("contrib_unassigned_year", ("year", "total_num", "total_den"))):
        rows = con.execute(
            f'SELECT {", ".join(keys)} FROM {t}.{tbl}').fetchall()
        upd = []
        for row in rows:
            num, den = int(row[-2]), int(row[-1])
            if den == 0:
                raise ValueError(f"{tbl} 中存在分母为 0 的分数")
            conds = keys[:-2]
            where = " AND ".join(f"{k} = ?" for k in conds)
            if tbl == "contrib_authors":
                upd.append((frac_text(num, den), approx_decimal(num, den),
                            *row[:-2]))
            else:
                upd.append((frac_text(num, den), approx_decimal(num, den),
                            DECIMAL_NOTE, *row[:-2]))
        if tbl == "contrib_authors":
            _em(con, f"UPDATE {t}.contrib_authors SET share_text = ?, "
                     f"share_decimal_approx = ? WHERE group_key = ? AND author_pos = ?", upd)
        elif tbl == "contrib_institution_year":
            _em(con, f"UPDATE {t}.contrib_institution_year SET total_text = ?, "
                     f"total_decimal_approx = ?, total_decimal_note = ? "
                     f"WHERE entity_id = ? AND year = ?", upd)
        else:
            _em(con, f"UPDATE {t}.contrib_unassigned_year SET total_text = ?, "
                     f"total_decimal_approx = ?, total_decimal_note = ? WHERE year = ?", upd)

    new_meta = {
        "format_version": FORMAT_VERSION,
        "fraction_format": FRACTION_FORMAT,
        "batch_id": batch_id,
        "migrated_from": "legacy_hugeint",
        "migrated_at": _now(),
    }
    new_meta.update(extra_meta)
    _em(con, f"INSERT INTO {t}.contrib_meta VALUES (?, ?)",
        [(k, str(v)) for k, v in sorted(new_meta.items())])


def frac_compare(con, table: str, num_col: str, den_col: str, expected: dict,
                 schema="main") -> list:
    """在 DuckDB 内用 BIGNUM 精确比较分数（不经浮点），返回不一致项（空 = 全部一致）。

    分子/分母先转成十进制字符串，再在 Python 大整数中比对；即使年度累计超过
    int128 也能精确核对。
    """
    from fractions import Fraction as _F
    s = f'"{schema}"'
    mism = []
    for key, frac in expected.items():
        where = " AND ".join(f"{k} = ?" for k in key)
        row = con.execute(
            f"SELECT cast({num_col} as varchar), cast({den_col} as varchar) "
            f"FROM {s}.{table} WHERE {where}", list(key)).fetchone()
        if row is None:
            mism.append((key, None))
            continue
        if _F(int(row[0]), int(row[1])) != frac:
            mism.append((key, (row[0], row[1]), frac))
    return mism


def backup_db(path) -> Path | None:
    """迁移前备份整个作业库文件；失败即中止（原账目保持）。"""
    if not is_file_db(path) or not Path(path).exists():
        return None
    bak = Path(f"{path}.pre-migration-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.bak")
    shutil.copy2(path, bak)
    return bak


# ---------------------------------------------------------------- 导出与发布
CSV_EXPORTS = [
    ("contrib_author_details.csv", """
      SELECT a.group_key, a.year, g.selected_supplier, g.selected_paper_id,
             substring(g.group_key, length(g.group_kind) + 3) AS dedup_key,
             a.author_pos, a.author_name,
             a.share_num AS author_share_num, a.share_den AS author_share_den,
             a.share_text AS author_share_text,
             a.share_decimal_approx AS author_share_decimal_approx,
             a.affil_refs_json AS affil_refs_before_dedup,
             a.located_entities_json AS located_entities_after_dedup,
             a.unlocated_refs_json AS unlocated_refs,
             t.target_kind, t.entity_id,
             t.share_num AS target_share_num, t.share_den AS target_share_den,
             t.share_num::varchar || '/' || t.share_den::varchar AS share_text,
             a.division_basis
      FROM {s}.contrib_authors a
      JOIN {s}.contrib_share_targets t USING (group_key, author_pos)
      JOIN {s}.contrib_groups g USING (group_key)
      ORDER BY a.group_key, a.author_pos, t.target_kind, t.entity_id
    """),
    ("contrib_institution_year.csv", """
      SELECT entity_id, year, total_num, total_den,
             total_text,
             total_decimal_approx,
             total_decimal_note,
             n_shares, n_papers
      FROM {s}.contrib_institution_year ORDER BY year, entity_id
    """),
    ("contrib_unassigned_year.csv", """
      SELECT year, total_num, total_den,
             total_text,
             total_decimal_approx,
             total_decimal_note,
             n_authors
      FROM {s}.contrib_unassigned_year ORDER BY year
    """),
    ("contrib_papers.csv", """
      SELECT group_key, group_kind, status, n_versions,
             selected_supplier, selected_paper_id, selected_year,
             select_reason, version_diffs_json
      FROM {s}.contrib_groups ORDER BY group_key
    """),
    ("contrib_paper_versions.csv", """
      SELECT group_key, supplier, paper_id, line_no, priority_rank,
             content_hash, is_selected
      FROM {s}.contrib_group_members
      ORDER BY group_key, priority_rank, supplier, paper_id
    """),
    ("contrib_quarantine.csv", """
      SELECT group_key, n_versions, select_reason AS quarantine_reason, version_diffs_json
      FROM {s}.contrib_groups WHERE status = 'quarantined' ORDER BY group_key
    """),
    ("contrib_rejected_lines.csv", """
      SELECT line_no, supplier, paper_id, reject_reason, raw_json
      FROM {s}.contrib_lines WHERE status = 'rejected' ORDER BY line_no
    """),
]

MANIFEST_FILE = "contrib_manifest.json"


def export_csvs(con, outdir, *, schema="main", batch_id=None) -> Path:
    """从指定 schema 导出整套 CSV 到 outdir，最后写 manifest（整套完成的标志）。"""
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    s = f'"{schema}"'
    for i, (name, sql) in enumerate(CSV_EXPORTS):
        path = (out / name).as_posix().replace("'", "''")
        con.execute(f"COPY ({sql.format(s=s)}) TO '{path}' (HEADER, DELIMITER ',')")
        if i == 1:
            fault_point("mid_export")
    manifest = {
        "batch_id": batch_id,
        "format_version": FORMAT_VERSION,
        "frac_format": FRACTION_FORMAT,
        "exact_columns": [
            "author_share_num / author_share_den / target_share_num / target_share_den",
            "total_num / total_den",
        ],
        "decimal_columns_are_approximate": True,
        "decimal_note": DECIMAL_NOTE,
        "exported_at": _now(),
    }
    (out / MANIFEST_FILE).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return out


def read_manifest(outdir):
    p = Path(outdir) / MANIFEST_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _rmtree(p):
    shutil.rmtree(p, ignore_errors=True)


def cleanup_staging_artifacts(con, *, out_dir=None, keep_csv_dir=None):
    """清掉可辨认的未完成暂存 schema 与临时 CSV 目录（不触碰当前结果）。"""
    removed = {"schemas": drop_all_stage_schemas(con), "dirs": []}
    if out_dir:
        out = Path(out_dir)
        pattern = out.name + ".staging-*"
        for d in sorted(out.parent.glob(pattern)):
            if keep_csv_dir and Path(keep_csv_dir) == d:
                continue
            _rmtree(d)
            removed["dirs"].append(str(d))
    return removed


def publish_csvs(con, *, out_dir, staging_dir, prev_dir, batch_id, schema="main") -> None:
    """把临时 CSV 目录整体换入 out_dir；每个中断窗口都可按确定性规则续做。

    规则：
      * staging 目录存在：若 out 已经存在（旧结果），先改名为 prev，再把 staging 换入；
      * staging 目录不存在：若 out 已带同一批次 manifest，说明已发布，仅清 prev；
        否则从数据库当前表重新导出完整一套再换入；
      * prev 仅在 out 已是完整新结果后删除。
    """
    out = Path(out_dir)
    stage = Path(staging_dir)
    prev = Path(prev_dir)

    if not stage.exists():
        manifest = read_manifest(out) if out.exists() else None
        if manifest and batch_id and manifest.get("batch_id") == batch_id:
            _rmtree(prev)
            return
        # prev 里可能是上次中断时留下的完整新结果（临时目录已被移走）：
        # 优先把它作为 staging 恢复，避免重导出一套可能与已提交批次不一致的内容
        if prev.exists() and read_manifest(prev) and \
                read_manifest(prev).get("batch_id") == batch_id:
            stage = prev
        else:
            # 临时目录丢了（例如被清理/进程在导出后、发布前终止）：从已提交的当前表重建
            stage = out.parent / f"{out.name}.rebuild-{batch_id or 'current'}"
            _rmtree(stage)
            export_csvs(con, stage, schema=schema, batch_id=batch_id)

    if stage.exists() and prev.exists() and stage.resolve() == prev.resolve():
        # 上次中断时 out 已改名为 prev、新目录尚未换入：直接换回即可
        fault_point("before_publish")
        stage.rename(out)
        return

    if out.exists():
        _rmtree(prev)
        # 故障注入点放在破坏性改名之前：此刻 out 仍是上一份完整结果
        fault_point("before_publish")
        out.rename(prev)
    else:
        fault_point("before_publish")
    stage.rename(out)
    _rmtree(prev)


# ---------------------------------------------------------------- 故障注入（仅测试）
def fault_point(point: str) -> None:
    """测试用故障注入：ORG_ER_CONTRIB_FAULT='<point>:crash' 进程立即终止；
    '<point>:error' 抛出模拟写入/导出失败。正常运行时环境变量未设置，是空操作。"""
    spec = os.environ.get("ORG_ER_CONTRIB_FAULT", "")
    if not spec:
        return
    p, _, mode = spec.partition(":")
    if p != point:
        return
    if mode == "crash":
        os._exit(137)
    raise OSError(f"fault-injected failure at {point}")
