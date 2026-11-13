#!/usr/bin/env python3
"""稳态通量分析入口：FBA 最大生物量 + 近最优空间逐反应 FVA。

  python3 scripts/flux.py --network data/flux/demo_network.json \\
                          --conditions data/flux/demo_conditions.json \\
                          --out output/flux \\
                          --retain-fraction 1.0 [--tolerance 1e-7]
  python3 scripts/flux.py --show output/flux

退出码：

  0  全部条件完成（不可行/无界等条件级问题只给醒目警告，其他条件照常可读回）
  2  输入错误（网络/条件结构非法、参数越界、重复 id 等，带位置）
  3  求解器数值失败或有效见证残差超出声明容差
"""
import argparse
import sys

import _bootstrap  # noqa: F401  路径引导
from org_er.flux.errors import FluxError, FluxInputError, FluxSolverError
from org_er.flux.job import (read_condition_summary, read_fva_ranges,
                             read_index, run_flux_analysis)


def _show(out_dir: str) -> int:
    index = read_index(out_dir)
    print(f"运行状态: {index['run_status']}")
    print(f"网络: {index['inputs']['network_path']}")
    print(f"条件: {index['inputs']['conditions_path']}")
    print(f"参数: retain_fraction={index['parameters']['retain_fraction']} "
          f"tolerance={index['parameters']['tolerance']:g}")
    if index["warnings"]:
        print("\n警告:")
        for w in index["warnings"]:
            print(f"  ! {w}")

    print("\n条件摘要:")
    print(f"  {'condition':<18} {'FBA':<18} {'z':>10}  FVA  witnesses")
    for row in read_condition_summary(out_dir):
        z = row["z"] or "-"
        print(f"  {row['condition_id']:<18} {row['fba_status']:<18} {z:>10}  "
              f"{row['fva_performed']:<5} {row['witness_count']}")

    ranges = read_fva_ranges(out_dir)
    if ranges:
        print("\n反应范围（FVA）:")
        print(f"  {'condition':<16} {'reaction':<12} {'min':>12} {'max':>12}  category")
        for row in ranges:
            lo = row["min_value"] if row["min_value"] != "" else f"({row['min_status']})"
            hi = row["max_value"] if row["max_value"] != "" else f"({row['max_status']})"
            print(f"  {row['condition_id']:<16} {row['reaction_id']:<12} "
                  f"{str(lo):>12} {str(hi):>12}  {row['category']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="FBA + 近最优 FVA（SciPy HiGHS）")
    parser.add_argument("--network", help="网络 JSON")
    parser.add_argument("--conditions", help="条件 JSON")
    parser.add_argument("--out", help="输出目录")
    parser.add_argument("--retain-fraction", type=float,
                        help="FVA 保留的生物量比例，区间 (0, 1]")
    parser.add_argument("--tolerance", type=float, default=1e-7,
                        help="判零与见证残差容差（默认 1e-7，须为正）")
    parser.add_argument("--show", metavar="OUT_DIR", help="读回并展示已有结果")
    args = parser.parse_args(argv)

    if args.show:
        try:
            return _show(args.show)
        except (OSError, KeyError, ValueError) as exc:
            print(f"读回失败: {exc}", file=sys.stderr)
            return 2

    missing = [n for n in ("network", "conditions", "out", "retain_fraction")
               if getattr(args, n) is None]
    if missing:
        parser.error(f"缺少参数: {', '.join('--' + n.replace('_', '-') for n in missing)}")

    try:
        index = run_flux_analysis(
            args.network, args.conditions, args.out,
            retain_fraction=args.retain_fraction, tolerance=args.tolerance)
    except FluxInputError as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2
    except FluxSolverError as exc:
        print(f"数值失败: {exc}", file=sys.stderr)
        return 3
    except FluxError as exc:
        print(f"作业失败: {exc}", file=sys.stderr)
        return 3

    print(f"运行状态: {index['run_status']}，产物目录: {args.out}")
    for w in index["warnings"]:
        print(f"  ! {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
