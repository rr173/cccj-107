#!/usr/bin/env python3
"""论文署名贡献核算作业（独立命令）。

在既有机构归并结果（只读对照）之上核算论文目录的署名贡献：

  python3 scripts/contrib.py \
      --er-db output/er.duckdb --round 3 \
      --catalog data/papers.jsonl \
      --priority crossref,scopus,local_ir \
      --out-db output/contrib.duckdb --out output/contrib --verify

两种用法：
  * 给 --er-db + --round + --catalog：执行核算，新结果整套核验后才成为当前结果；
  * 只给 --out-db（可加 --out）：识别并核验已有账目；若是旧版 HUGEINT 小账目，
    无损转换为新格式并重新导出（转换前备份数据库，失败时原账目保留）。

机构库只以 READ_ONLY 方式挂载，本作业不改动其中任何内容。
"""
import argparse

import _bootstrap  # noqa: F401
from org_er.contrib.job import reuse_existing, run_job
from org_er.contrib.verify import verify_run


def _print_checks(checks, label="独立核验"):
    for name, c in checks.items():
        if name == "ok":
            continue
        print(f"    [{'PASS' if c['ok'] else 'FAIL'}] {name}: {c['detail']}")
    print(f"{label}:", "PASS" if checks["ok"] else "FAIL")
    return checks["ok"]


def main():
    ap = argparse.ArgumentParser(description="论文署名贡献核算作业")
    ap.add_argument("--er-db", help="机构归并结果所在的 DuckDB 库（只读）；给定时执行核算")
    ap.add_argument("--round", type=int, help="使用哪一轮归并结果")
    ap.add_argument("--catalog", help="论文目录 JSONL")
    ap.add_argument("--priority", default="",
                    help="供应方优先级，逗号分隔（高->低）；未列入的并列最低")
    ap.add_argument("--out-db", default="output/contrib.duckdb", help="作业库路径")
    ap.add_argument("--out", default="output/contrib", help="CSV 交付表目录")
    ap.add_argument("--verify", action="store_true", help="运行后执行独立核验")
    ap.add_argument("--reuse-existing", action="store_true",
                    help="不重算目录：识别/核验已有账目并重新导出（自动兼容旧 HUGEINT 格式）")
    args = ap.parse_args()

    priority = [s.strip() for s in args.priority.split(",") if s.strip()]

    if args.reuse_existing or not args.er_db:
        if args.reuse_existing and args.er_db:
            ap.error("--reuse-existing 不重算目录，不能同时给 --er-db（要重算请去掉该开关）")
        try:
            info, con = reuse_existing(out_db=args.out_db, out_dir=args.out, verify=True)
        except Exception as e:
            raise SystemExit(f"作业失败（原账目保持不动）: {e}")
        print(f"识别格式: {info['detected_format']}；批次: {info['batch_id']}"
              f"{'（旧 HUGEINT 账目已无损转换）' if info['migrated'] else ''}")
        if info.get("backup"):
            print(f"转换前数据库备份: {info['backup']}")
        rec = info.get("recovered") or {}
        if rec.get("action") not in (None, "none"):
            print(f"启动恢复: {rec.get('action')}；保留/采用批次 {rec.get('kept')}")
        if info["verified_before"] is not None and info["migrated"]:
            print("转换前旧账目独立核验:", "PASS" if info["verified_before"]["ok"] else "FAIL")
        if info["verified_after"] is not None:
            if not _print_checks(info["verified_after"], "当前账目独立核验"):
                raise SystemExit(1)
        if info.get("export") is not None and not info["export"]["ok"]:
            raise SystemExit(f"导出核验失败: {info['export']['problems'][:3]}")
        print(f"作业库: {args.out_db}；交付表: {args.out}/")
        con.close()
        return

    if args.round is None or not args.catalog:
        ap.error("执行核算需要 --er-db、--round 与 --catalog")

    try:
        summary, con = run_job(er_db=args.er_db, round_no=args.round, catalog=args.catalog,
                               priority=priority, out_db=args.out_db, out_dir=args.out)
    except Exception as e:  # 写入/导出/核验任何一步失败：上一份成功结果保持不动
        raise SystemExit(
            f"作业失败: {e}\n"
            f"上一份成功结果与交付表未被替换；未完成数据已记在状态日志里，"
            f"重新运行或 --reuse-existing 会先自动恢复。")

    print(f"目录行 {summary.n_lines}：有效 {summary.n_ok}，坏行 {summary.n_rejected} "
          f"{dict(sorted(summary.reject_reasons.items())) or ''}")
    print(f"去重分组 {summary.n_groups}：计入 {summary.n_counted}，隔离 {summary.n_quarantined}")
    print(f"作者分摊明细 {summary.n_author_rows} 行；批次 {summary.batch_id}")
    print("机构×年份贡献量（精确分数）：")
    for (eid, year), total in summary.institution_year.items():
        num, _, den = total.partition("/")
        print(f"    {year}  {eid}  {total}  （展示约 {float(int(num) / int(den)):.6f}）")
    print("待归属（按年）：")
    for year, total in summary.unassigned_year.items():
        num, _, den = total.partition("/")
        print(f"    {year}  {total}  （展示约 {float(int(num) / int(den)):.6f}）")
    print(f"作业库: {args.out_db}；交付表: {args.out}/")

    if args.verify:
        checks = verify_run(con)
        if not _print_checks(checks):
            raise SystemExit(1)
    con.close()


if __name__ == "__main__":
    main()
