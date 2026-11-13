#!/usr/bin/env python3
"""离线 LC-MS 目标离子提取命令：mzML 批次 + 目标表 -> 面积汇总与扫描明细。

  python3 scripts/lcms.py --manifest data/lcms/manifest.json \
                          --targets  data/lcms/targets.json \
                          --out      output/lcms --max-gap-seconds 5
  python3 scripts/lcms.py --out output/lcms --show                 # 读回汇总
  python3 scripts/lcms.py --out output/lcms --locate S1 --spectrum 4

只做 mzML 解码、目标离子色谱与时间窗积分，不借助厂商软件、不做化合物自动
鉴定。批内有样本失败时以非零状态结束，但正常样本照常给出结果。
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from org_er.lcms.errors import LcmsError
from org_er.lcms.job import (locate_scan, read_detail, read_diagnostics,
                             read_index, read_summary, run_batch)


def _print_index(index, diagnostics):
    print(f"批次 {index['batch_id']}: 清单 {index['n_samples_listed']} 个样本，"
          f"完成 {index['n_completed']}，诊断 {index['n_diagnostics']}，"
          f"max_gap={index['max_gap_seconds']}s")
    for s in index["samples"]:
        print(f"  OK  {s['sample_key']:<10} MS1={s['n_ms1_scans']:<4} "
              f"按极性={s['n_ms1_by_polarity']} 明细={s['detail_file']}")
    for d in diagnostics:
        print(f"  BAD {d['sample_key']:<10} [{d['stage']}] {d['reason']}")
    print("产物: summary.csv  details/*.csv  diagnostics.json  index.json")
    print("注意: 面积为信号积分（强度×秒），不是已校准的浓度")


def main():
    ap = argparse.ArgumentParser(description="离线 LC-MS 目标离子提取（mzML -> 峰面积）")
    ap.add_argument("--manifest", help="文件清单 JSON/CSV/TSV（sample_key + mzml_path，路径相对清单）")
    ap.add_argument("--targets", help="目标表 JSON/CSV/TSV（target_id/mz/ppm/polarity/窗口/background）")
    ap.add_argument("--out", required=True, help="产物目录")
    ap.add_argument("--max-gap-seconds", type=float, default=5.0,
                    help="相邻扫描可连线的最大时间差（秒，默认 5）；更大间隙不积分、不外推")
    ap.add_argument("--show", action="store_true", help="只读回模式：打印批次汇总与诊断")
    ap.add_argument("--locate", help="读回模式：指定样本键")
    ap.add_argument("--spectrum", type=int, help="--locate 用：定位指定 spectrum 序号的明细行")
    ap.add_argument("--detail", help="读回模式：打印指定样本的全部时间-强度明细")
    args = ap.parse_args()

    if args.show or args.locate or args.detail:
        try:
            if args.show:
                index = read_index(args.out)
                diagnostics = read_diagnostics(args.out)
                _print_index(index, diagnostics)
                print("\nsummary.csv:")
                for row in read_summary(args.out):
                    area = row["area"] if row["area"] != "" else "(空/absent)"
                    print(f"  {row['sample_key']:<10} {row['target_id']:<6} "
                          f"{row['polarity']:<8} area={area:<14} "
                          f"cover={row['covered_seconds']}s/{row['window_seconds']}s "
                          f"({float(row['coverage_ratio']) * 100:.2f}%) {row['status']}")
                return 0
            if args.locate:
                if args.spectrum is None:
                    ap.error("--locate 需要同时给 --spectrum")
                rows = locate_scan(args.out, args.locate, args.spectrum)
                if not rows:
                    raise SystemExit(
                        f"样本 {args.locate!r} 中找不到 spectrum[{args.spectrum}]"
                        "（注意：失败样本不会产出明细，见 diagnostics.json）")
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            rows = read_detail(args.out, args.detail)
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        except LcmsError as e:
            raise SystemExit(f"读回失败: {e}")

    if not args.manifest or not args.targets:
        ap.error("批处理需要 --manifest 与 --targets（读回请用 --show/--locate/--detail）")

    try:
        index = run_batch(args.manifest, args.targets, args.out,
                          max_gap_seconds=args.max_gap_seconds)
    except LcmsError as e:
        print(f"[作业失败] {e}", file=sys.stderr)
        return 2

    diagnostics = read_diagnostics(args.out)
    _print_index(index, diagnostics)
    if diagnostics:
        print(f"\n{len(diagnostics)} 个样本失败，详见 {args.out}/diagnostics.json",
              file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
