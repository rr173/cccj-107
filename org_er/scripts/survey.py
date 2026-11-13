#!/usr/bin/env python3
"""复杂抽样问卷估计：抽样设计 + 应答调整 + 边际校准 + 分层删一 PSU jackknife。

  python3 scripts/survey.py --data data/survey/survey.csv \\
                            --design data/survey/design.json \\
                            --out output/survey

退出码：

  0  分析完成（无复制失败）
  0  分析完成，但有复制失败（点估计可算、部分方差留空）——仍为 0，并在输出中醒目标记
  2  数据或设计非法（身份重复、设计列缺失、非法权重、层/PSU 结构或校准目标矛盾）
  3  全样本权重链失败（无应答调整格、校准无样本支持/零分母/边际冲突/超迭代）

复制失败时命令仍发布已完成的点估计与复制诊断，并打印失败明细（不静默）。
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from org_er.survey.errors import (SurveyDataError, SurveyDesignError, SurveyError)
from org_er.survey.job import SurveyRunFailed, read_diagnostics, run_survey


def _print_estimates(rows: list[dict]) -> None:
    print(f"{'scope':<8} {'domain':<16} {'metric':<15} {'estimate':>14} "
          f"{'se':>12} {'ci95':>26}  status")
    for r in rows:
        est = r["estimate"] or "-"
        se = r["se"] or "-"
        ci = ("-" if not r["ci95_low"] and not r["ci95_high"]
              else f"[{r['ci95_low'] or '-'}, {r['ci95_high'] or '-'}]")
        status = r["variance_status"]
        if r["estimate_status"] != "ok":
            status = "estimate:" + r["estimate_status"]
        print(f"{r['scope']:<8} {r['domain']:<16} {r['metric']:<15} {est:>14} "
              f"{se:>12} {ci:>26}  {status}")


def _show(out_dir: str) -> int:
    diag = read_diagnostics(out_dir)
    from org_er.survey.job import read_estimates
    print(f"运行状态: {diag['run_status']}")
    rows = read_estimates(out_dir)
    _print_estimates(rows)
    if diag["replicate_failures"]:
        print("\n复制失败明细（相关估计量的方差/区间已留空）:")
        for f in diag["replicate_failures"]:
            print(f"  {f['stratum']} / {f['psu']} [{f['stage']}]: {f['reason']}")
    missing_var = [r for r in rows if r["variance_status"] != "ok"]
    if missing_var:
        print("\n方差不可用（按规则留空）:")
        seen = set()
        for r in missing_var:
            key = (r["scope"], r["domain"], r["variance_reason"])
            if key not in seen:
                seen.add(key)
                print(f"  {r['scope']}/{r['domain']}: {r['variance_reason']}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="复杂抽样问卷估计（应答调整 + 边际校准 + 删一 PSU jackknife）")
    ap.add_argument("--data", help="数据 CSV（每行一名被抽中者，含未应答者）")
    ap.add_argument("--design", help="设计 JSON（必入层、校准列/类别/已知人数、容差）")
    ap.add_argument("--out", help="输出目录（默认 output/survey）")
    ap.add_argument("--max-iterations", type=int,
                    help="覆盖设计中的 IPF 最大轮数（正整数）")
    ap.add_argument("--tolerance", type=float,
                    help="覆盖设计中的边际相对误差容差（正有限数）")
    ap.add_argument("--show", metavar="OUT_DIR",
                    help="读回并打印已有结果的估计表与诊断")
    args = ap.parse_args(argv)

    if args.show:
        try:
            return _show(args.show)
        except (OSError, KeyError, json.JSONDecodeError) as e:
            print(f"读回失败: {e}", file=sys.stderr)
            return 2

    if not args.data or not args.design:
        ap.error("分析模式需要 --data 与 --design（读回已有结果请用 --show）")
    out = args.out or str(_bootstrap.ROOT / "output" / "survey")

    try:
        index = run_survey(args.data, args.design, out,
                           max_iterations=args.max_iterations,
                           tolerance=args.tolerance)
    except SurveyRunFailed as e:
        print(f"[全样本链路失败] {e.stage}: {e}", file=sys.stderr)
        if e.failed_dir:
            print(f"失败诊断: {e.failed_dir}", file=sys.stderr)
            print("上一次成功结果目录未被覆盖（如有）。", file=sys.stderr)
        return 3
    except (SurveyDataError, SurveyDesignError) as e:
        print(f"[输入非法] {e}", file=sys.stderr)
        return 2
    except SurveyError as e:
        print(f"[作业失败] {e}", file=sys.stderr)
        return 2

    s = index["structure"]
    print(f"批次 {index['batch_id']}：{s['n_people']} 名被抽中者，"
          f"{s['n_respondents']} 名应答者；{s['n_strata']} 层 / {s['n_psus']} PSU；"
          f"必入层 {s['must_include_strata'] or '无'}")
    print(f"调整格 {index['adjustment']['n_cells']} 个；"
          f"校准 {index['calibration']['iterations_used']} 轮收敛，"
          f"最终最大相对误差 {index['calibration']['max_final_relative_error']:.3e}；"
          f"删除复制 {index['n_replicates']} 个（失败 {index['n_replicates_failed']}）")
    for col in index["calibration_columns"]:
        print(f"  校准列 {col['name']}: " +
              ", ".join(f"{c}={t:g}" for c, t in zip(col["categories"],
                                                     col["targets"])))
    from org_er.survey.job import read_estimates
    rows = read_estimates(out)
    print()
    _print_estimates([r for r in rows if r["metric"] in ("mean", "weighted_count")])
    print(f"\n产物: {out}/estimates.csv person_weights.csv replicates.csv "
          f"replicate_estimates.csv diagnostics.json")
    if index["run_status"] == "replicate_failures":
        print(f"\n[注意] {index['n_replicates_failed']} 个删除复制失败；"
              "相关估计量的方差与区间已留空（详见 diagnostics.json）。")
        for f in index["replicate_failures"]:
            print(f"  {f['stratum']} / {f['psu']} [{f['stage']}]: {f['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
