#!/usr/bin/env python3
"""论文署名贡献核算作业（独立命令）。

在既有机构归并结果（只读对照）之上核算论文目录的署名贡献：

  python3 scripts/contrib.py \
      --er-db output/er.duckdb --round 3 \
      --catalog data/papers.jsonl \
      --priority crossref,scopus,local_ir \
      --out-db output/contrib.duckdb --out output/contrib --verify

机构库只以 READ_ONLY 方式挂载，本作业不改动其中任何内容；
核算结果（逐位作者分摊明细、机构×年份汇总、待归属、去重来源、隔离与坏行）
写入独立的作业库并导出 CSV。
"""
import argparse

import _bootstrap  # noqa: F401
from org_er.contrib.job import run_job
from org_er.contrib.verify import verify_run


def main():
    ap = argparse.ArgumentParser(description="论文署名贡献核算作业")
    ap.add_argument("--er-db", required=True, help="机构归并结果所在的 DuckDB 库（只读）")
    ap.add_argument("--round", type=int, required=True, help="使用哪一轮归并结果")
    ap.add_argument("--catalog", required=True, help="论文目录 JSONL")
    ap.add_argument("--priority", default="",
                    help="供应方优先级，逗号分隔（高->低）；未列入的并列最低")
    ap.add_argument("--out-db", default="output/contrib.duckdb", help="作业库路径")
    ap.add_argument("--out", default="output/contrib", help="CSV 交付表目录")
    ap.add_argument("--verify", action="store_true", help="运行后执行独立核验")
    args = ap.parse_args()

    priority = [s.strip() for s in args.priority.split(",") if s.strip()]

    try:
        summary, con = run_job(er_db=args.er_db, round_no=args.round, catalog=args.catalog,
                               priority=priority, out_db=args.out_db, out_dir=args.out)
    except (ValueError, FileNotFoundError) as e:
        raise SystemExit(f"作业失败: {e}")

    print(f"目录行 {summary.n_lines}：有效 {summary.n_ok}，坏行 {summary.n_rejected} "
          f"{dict(sorted(summary.reject_reasons.items())) or ''}")
    print(f"去重分组 {summary.n_groups}：计入 {summary.n_counted}，隔离 {summary.n_quarantined}")
    print(f"作者分摊明细 {summary.n_author_rows} 行")
    print("机构×年份贡献量（精确分数）：")
    for (eid, year), total in summary.institution_year.items():
        print(f"    {year}  {eid}  {total}")
    print("待归属（按年）：")
    for year, total in summary.unassigned_year.items():
        print(f"    {year}  {total}")
    print(f"作业库: {args.out_db}；交付表: {args.out}/")

    if args.verify:
        checks = verify_run(con)
        for name, c in checks.items():
            if name == "ok":
                continue
            print(f"    [{'PASS' if c['ok'] else 'FAIL'}] {name}: {c['detail']}")
        print("独立核验:", "PASS" if checks["ok"] else "FAIL")
        if not checks["ok"]:
            raise SystemExit(1)
    con.close()


if __name__ == "__main__":
    main()
