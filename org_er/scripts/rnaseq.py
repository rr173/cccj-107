#!/usr/bin/env python3
"""RNA-seq 片段计数命令：GTF + 按 QNAME 排序的 SAM/BAM -> gene×sample 整数计数矩阵。

  python3 scripts/rnaseq.py --gtf data/rnaseq/demo.gtf \
                            --samples data/rnaseq/samples.json \
                            --out output/rnaseq --min-mapq 10
  python3 scripts/rnaseq.py --explain --out output/rnaseq --sample s1 --qname fragE

输入文件未按 QNAME 字节序排列、BAM 读取失败或记录结构损坏时，以非零状态结束，
不隐式重排、不输出看似完成的矩阵。
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from org_er.rnaseq.counting import AlignmentReadError, CountingError, InputOrderError
from org_er.rnaseq.outputs import read_counts, read_fragment, run


def main():
    ap = argparse.ArgumentParser(description="按外显子归属的片段级基因计数")
    ap.add_argument("--gtf", help="GTF 注释（exon 行带 gene_id；1-based 包含坐标）")
    ap.add_argument("--samples", help="样本表 JSON 或 TSV（sample/alignment/library）")
    ap.add_argument("--out", required=True, help="产物目录（counts.csv / summary.csv / fragments/）")
    ap.add_argument("--min-mapq", type=int, default=10, help="MAPQ 下限，默认 10")
    ap.add_argument("--explain", action="store_true",
                    help="只读回模式：查 --sample 中 --qname 的逐片段判定记录")
    ap.add_argument("--sample", help="--explain 用：样本名")
    ap.add_argument("--qname", help="--explain 用：片段 QNAME")
    args = ap.parse_args()

    if args.explain:
        if not args.sample or not args.qname:
            ap.error("--explain 需要同时给出 --sample 与 --qname")
        try:
            row = read_fragment(args.out, args.sample, args.qname)
        except CountingError as e:
            raise SystemExit(f"查询失败: {e}")
        if row is None:
            raise SystemExit(f"样本 {args.sample!r} 中找不到 QNAME {args.qname!r}")
        print(json.dumps(row, ensure_ascii=False, indent=2))
        return 0

    if not args.gtf or not args.samples:
        ap.error("计数模式需要 --gtf 与 --samples（查片段请用 --explain）")

    try:
        info = run(args.gtf, args.samples, args.out, min_mapq=args.min_mapq)
    except InputOrderError as e:
        print(f"[排序错误] {e}", file=sys.stderr)
        return 3
    except AlignmentReadError as e:
        print(f"[输入读取错误] {e}", file=sys.stderr)
        return 4
    except CountingError as e:
        print(f"[作业失败] {e}", file=sys.stderr)
        return 2

    print(f"基因 {info['n_genes']} 个，样本 {len(info['samples'])} 个，min_mapq={info['min_mapq']}")
    for s in info["samples"]:
        print(f"  {s['sample']:<12} 组={s['n_qname_groups']:<4} "
              f"assigned={s['matrix_sum']:<4} 库={s['library']}  "
              f"矩阵总和==assigned: {s['matrix_sum_equals_assigned']}")
    gc = info["global_check"]
    print(f"全局校验：矩阵总和 {gc['matrix_sum']} == assigned 合计 {gc['assigned_total']} "
          f"-> {'OK' if gc['ok'] else 'BAD'}")
    print(f"产物: {args.out}/counts.csv  summary.csv  fragments/*.jsonl  run.json")
    return 0 if gc["ok"] else 5


if __name__ == "__main__":
    sys.exit(main())
