"""核算作业编排：读机构归并结果（只读）+ 论文目录 JSONL -> 明细与汇总落库、导出。

运行方式见 scripts/contrib.py。一次运行 = 一个作业库（可重复运行，幂等覆盖）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import store as cstore
from .account import account_groups
from .dedup import dedup_lines
from .parse import parse_catalog


@dataclass
class JobSummary:
    n_lines: int = 0
    n_ok: int = 0
    n_rejected: int = 0
    n_groups: int = 0
    n_counted: int = 0
    n_quarantined: int = 0
    n_author_rows: int = 0
    institution_year: dict = field(default_factory=dict)   # (entity_id, year) -> "num/den"
    unassigned_year: dict = field(default_factory=dict)    # year -> "num/den"
    reject_reasons: dict = field(default_factory=dict)     # reason_prefix -> count


def run_job(*, er_db: str, round_no: int, catalog: str, priority: list,
            out_db: str, out_dir: str = None, con=None):
    """执行核算作业，返回 (summary, 作业库连接)。

    er_db     机构归并结果所在的 DuckDB 库（只读挂载，绝不写入）
    round_no  使用哪一轮归并结果
    catalog   论文目录 JSONL
    priority  供应方优先级（高 -> 低）；未列入的供应方并列最低
    out_db    作业库路径（':memory:' 或文件）；con 传入时复用该连接
    out_dir   给定时导出 CSV 交付表
    """
    con = con or cstore.open_out_db(out_db)
    cstore.reset_tables(con)
    cstore.attach_er_readonly(con, er_db)

    entity_map = cstore.load_entity_map(con, round_no)  # 指定轮次不存在时抛错并列出可用轮次

    lines = parse_catalog(catalog)
    ok_lines = [l for l in lines if l.status == "ok"]
    groups = dedup_lines(ok_lines, priority)
    account = account_groups(groups, entity_map)

    meta = {
        "er_db": str(er_db), "round_no": round_no, "catalog": str(catalog),
        "priority": ",".join(priority), "n_entity_keys": len(entity_map),
    }
    cstore.write_run(con, meta=meta, lines=lines, groups=groups, account=account)
    if out_dir:
        cstore.export_csvs(con, out_dir)

    summary = JobSummary(
        n_lines=len(lines),
        n_ok=len(ok_lines),
        n_rejected=len(lines) - len(ok_lines),
        n_groups=len(groups),
        n_counted=sum(1 for g in groups if g.status == "counted"),
        n_quarantined=sum(1 for g in groups if g.status == "quarantined"),
        n_author_rows=len(account.authors),
        institution_year={k: f"{v[0].numerator}/{v[0].denominator}"
                          for k, v in sorted(account.institution_year.items())},
        unassigned_year={y: f"{v[0].numerator}/{v[0].denominator}"
                         for y, v in sorted(account.unassigned_year.items())},
    )
    for l in lines:
        if l.status == "rejected":
            key = l.reject_reason.split(":", 1)[0]
            summary.reject_reasons[key] = summary.reject_reasons.get(key, 0) + 1
    return summary, con
