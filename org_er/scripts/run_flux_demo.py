#!/usr/bin/env python3
"""稳态通量分析端到端演示：跑三套网络、两种保留比例，打印关键结论。

  python3 scripts/run_flux_demo.py

覆盖：限量碳源、两条替代合成路线（含各自被切断的替代最优解）、
可逆反应、无关内部循环、切断全部生物量供给（z=0 仍可行）、
故意不可行、目标无界、有限目标下循环通量无界。
独立的手算/独立 LP/见证核验在 run_flux_reference_check.py。
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

ROOT = Path(_bootstrap.ROOT)
DATA = ROOT / "data" / "flux"
SCRIPT = ROOT / "scripts" / "flux.py"
ASSERTS = 0


def expect(cond: bool, label: str) -> None:
    global ASSERTS
    ASSERTS += 1
    if not cond:
        raise AssertionError(label)
    print(f"  ok  {label}")


def run(net, conds, out, fraction):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--network", str(DATA / net),
         "--conditions", str(DATA / conds), "--out", str(out),
         "--retain-fraction", str(fraction)],
        capture_output=True, text=True)


def rows(out, name):
    with open(out / name, encoding="utf-8") as fh:
        import csv
        return list(csv.DictReader(fh))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        print("== 1. 主网络 retain=1.0（近最优空间） ==")
        p = run("demo_network.json", "demo_conditions.json",
                tmp / "r10", 1.0)
        expect(p.returncode == 0, "条件级不可行只警告，退出码仍为 0")
        summ = {r["condition_id"]: r for r in rows(tmp / "r10",
                                                   "condition_summary.csv")}
        expect(summ["baseline"]["z"] == "10.0", "基线最大生物量 z*=10")
        expect(summ["carbon_limited"]["z"] == "4.0", "限碳后 z*=4")
        expect(summ["route1_knockout"]["z"] == "10.0"
               and summ["route2_knockout"]["z"] == "10.0",
               "切断任一条替代路线，另一条仍达 z*=10（替代最优解）")
        expect(summ["no_biomass"]["z"] == "0.0"
               and summ["no_biomass"]["fva_performed"] == "True",
               "z=0 但可行：继续做 FVA，不当作不可行")
        expect(summ["forced_infeasible"]["fba_status"] == "infeasible"
               and summ["forced_infeasible"]["z"] == "",
               "不可行条件状态与数值分开存，z 留空不填 0")

        rng = {(r["condition_id"], r["reaction_id"]): r
               for r in rows(tmp / "r10", "fva_ranges.csv")}
        expect(rng[("baseline", "iso")]["category"] == "reversible",
               "基线 iso 能正能负（-8..8）")
        expect(rng[("no_biomass", "iso")]["category"] == "reversible",
               "无生物量时 iso 仍可逆，主通路反应 blocked")
        expect(rng[("route1_knockout", "syn1")]["category"] == "blocked",
               "被敲除的 syn1 判 blocked")
        expect(rng[("route1_knockout", "iso")]["category"] == "reverse_only",
               "单向活动与可逆活动区分开")
        expect(rng[("baseline", "cyc1")]["category"] == "forward_only",
               "无关内部循环独立保持 0..3")

        print("\n== 2. 主网络 retain=0.5（范围应放宽） ==")
        run("demo_network.json", "demo_conditions.json", tmp / "r05", 0.5)
        rng05 = {(r["condition_id"], r["reaction_id"]): r
                 for r in rows(tmp / "r05", "fva_ranges.csv")}
        g10 = rng[("baseline", "GROWTH")]
        g05 = rng05[("baseline", "GROWTH")]
        expect(g10["min_value"] == "10.0" and g05["min_value"] == "5.0",
               f"GROWTH 范围由 [{g10['min_value']},{g10['max_value']}] "
               f"放宽到 [{g05['min_value']},{g05['max_value']}]")

        print("\n== 3. 目标无界网络 ==")
        run("unbounded_objective_network.json",
            "unbounded_objective_conditions.json", tmp / "ub", 1.0)
        s = {r["condition_id"]: r for r in rows(tmp / "ub",
                                                "condition_summary.csv")}
        expect(s["obj_unbounded"]["fba_status"] == "unbounded"
               and s["obj_unbounded"]["fva_performed"] == "False",
               "目标无界：状态记 unbounded，不做依赖有限 z 的 FVA")
        expect(s["capped"]["z"] == "7.0",
               "同一网络补上摄取上限后 z=7，证明无界来自缺失上界")

        print("\n== 4. 有限目标 + 循环通量无界 ==")
        run("unbounded_cycle_network.json",
            "unbounded_cycle_conditions.json", tmp / "cyc", 1.0)
        cyc = rows(tmp / "cyc", "fva_ranges.csv")
        lp_row = next(r for r in cyc if r["reaction_id"] == "lp")
        expect(lp_row["min_status"] == "unbounded"
               and lp_row["max_status"] == "unbounded"
               and lp_row["category"] == "unbounded_both",
               "目标有限 z=5，循环反应 lp/lm 两端点分别记无界，不偷加上限")
        feed = next(r for r in cyc if r["reaction_id"] == "feed")
        expect(feed["min_status"] == "optimal" and feed["min_value"] == "5.0",
               "无界端点不影响其他反应的有限端点照常算出")

        print("\n== 5. 重跑保护：上一次结果不被摧毁 ==")
        expect((tmp / "r10" / "index.json").exists(), "首次结果已发布")
        run("demo_network.json", "demo_conditions.json", tmp / "r10", 1.0)
        expect((tmp / "r10" / "index.json").exists()
               and (tmp / ".r10.prev").exists(),
               "重跑成功后新结果原子换入，旧结果整体退为 .r10.prev")
        prev_index = json.load(
            open(tmp / ".r10.prev" / "index.json", encoding="utf-8"))
        expect(prev_index["parameters"]["retain_fraction"] == 1.0,
               "旧结果内容完整可读")

        print("\n== 6. 输入与参数可追溯 ==")
        index = json.load(open(tmp / "r10" / "index.json", encoding="utf-8"))
        expect(index["inputs"]["network_sha256"] and
               index["inputs"]["conditions_sha256"],
               "index 记录两个输入文件的 sha256")
        expect(index["parameters"]["retain_fraction"] == 1.0,
               "index 记录实际 retain_fraction")
        w = json.load(open(tmp / "r10" / "conditions" / "baseline"
                           / "witnesses" / "fba.json", encoding="utf-8"))
        expect(w["diagnostics"]["valid"] is True
               and w["diagnostics"]["max_balance_residual"] == 0.0,
               "见证带守恒/上下界残差诊断且标记有效")

    print(f"\n演示完成：{ASSERTS} 项断言全过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
