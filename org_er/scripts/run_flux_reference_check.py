#!/usr/bin/env python3
"""稳态通量分析的独立参考核验（不 import 任何 org_er.flux 生产代码）。

三层独立证据：

1. 手算期望值 EXPECTED：直接从守恒方程与约束交点推导（脚本内附推导），
   与生产输出逐条件、逐反应、逐端点比较；
2. 独立 LP 重解：用 scipy.linprog 重新构造 S·v=0 与上下界，
   自己实现 FBA/FVA 的目标与保留约束（不复用生产的 solver/job 函数），
   核对生产输出的每个有限端点；
3. 见证逐项检查：读回每个 witness，用 JSON 里的原始网络计量矩阵重算
   守恒残差、上下界违背、保留约束缺口，全部必须在声明容差内。

另验证：条件顺序打乱后数值与状态一致；坏输入被拒且退出码非零。
"""
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401
import numpy as np
from scipy.optimize import linprog

ROOT = Path(_bootstrap.ROOT)
DATA = ROOT / "data" / "flux"
SCRIPT = ROOT / "scripts" / "flux.py"
TOL = 1e-7
CHECKS = 0


def check(cond: bool, label: str) -> None:
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError(f"核验失败: {label}")
    print(f"  ok [{CHECKS:3d}] {label}")


def approx(a, b, tol=TOL) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


# ---------------------------------------------------------------- 手算期望
# 主网络守恒（内部 C,M,X,P,Y1,Y2）：
#   EX_C=syn1+syn2；syn1=iso+p1+EX_M；syn2+iso=p2+EX_X；p1+p2=GROWTH；cyc1=cyc2
# 相加恒等式：GROWTH+EX_M+EX_X = EX_C <= 摄取上限；排出不产 P，故最优时排出为 0。
# iso∈[-8,8]，cyc1=cyc2∈[0,3]，syn/p/排出均 >=0。
EXPECTED = {
    "baseline": {
        "z": 10.0,
        "retain_1": {
            "EX_C": (10.0, 10.0), "syn1": (0.0, 10.0), "syn2": (0.0, 10.0),
            "iso": (-8.0, 8.0), "p1": (0.0, 10.0), "p2": (0.0, 10.0),
            "GROWTH": (10.0, 10.0), "EX_M": (0.0, 0.0), "EX_X": (0.0, 0.0),
            "cyc1": (0.0, 3.0), "cyc2": (0.0, 3.0)},
        "retain_half": {  # GROWTH>=5：排出至多 5，EX_C∈[5,10]
            "EX_C": (5.0, 10.0), "GROWTH": (5.0, 10.0),
            "EX_M": (0.0, 5.0), "EX_X": (0.0, 5.0)},
    },
    "carbon_limited": {
        "z": 4.0,
        "retain_1": {
            "EX_C": (4.0, 4.0), "syn1": (0.0, 4.0), "syn2": (0.0, 4.0),
            "iso": (-4.0, 4.0), "p1": (0.0, 4.0), "p2": (0.0, 4.0),
            "GROWTH": (4.0, 4.0), "EX_M": (0.0, 0.0), "EX_X": (0.0, 0.0),
            "cyc1": (0.0, 3.0), "cyc2": (0.0, 3.0)},
        "retain_half": {  # GROWTH>=2：EX_C∈[2,4]，排出至多 2
            "EX_C": (2.0, 4.0), "GROWTH": (2.0, 4.0),
            "EX_M": (0.0, 2.0), "EX_X": (0.0, 2.0)},
    },
    "route1_knockout": {
        # syn1=0 -> M 式：iso+p1+EX_M=0，最优 EX_M=0 且 iso∈[-8,0]
        "z": 10.0,
        "retain_1": {
            "EX_C": (10.0, 10.0), "syn1": (0.0, 0.0), "syn2": (10.0, 10.0),
            "iso": (-8.0, 0.0), "p1": (0.0, 8.0), "p2": (2.0, 10.0),
            "GROWTH": (10.0, 10.0), "EX_M": (0.0, 0.0), "EX_X": (0.0, 0.0),
            "cyc1": (0.0, 3.0), "cyc2": (0.0, 3.0)},
    },
    "route2_knockout": {
        # syn2=0 -> X 式：iso=p2+EX_X，最优 EX_X=0 且 iso∈[0,8]
        "z": 10.0,
        "retain_1": {
            "EX_C": (10.0, 10.0), "syn1": (10.0, 10.0), "syn2": (0.0, 0.0),
            "iso": (0.0, 8.0), "p1": (2.0, 10.0), "p2": (0.0, 8.0),
            "GROWTH": (10.0, 10.0), "EX_M": (0.0, 0.0), "EX_X": (0.0, 0.0),
            "cyc1": (0.0, 3.0), "cyc2": (0.0, 3.0)},
    },
    "no_biomass": {
        # z=0 但系统可行；GROWTH=p1=p2=0，碳可从 EX_M/EX_X 排出，iso 双向
        "z": 0.0,
        "retain_1": {
            "EX_C": (0.0, 10.0), "syn1": (0.0, 10.0), "syn2": (0.0, 10.0),
            "iso": (-8.0, 8.0), "p1": (0.0, 0.0), "p2": (0.0, 0.0),
            "GROWTH": (0.0, 0.0), "EX_M": (0.0, 10.0), "EX_X": (0.0, 10.0),
            "cyc1": (0.0, 3.0), "cyc2": (0.0, 3.0)},
    },
    "forced_infeasible": {"z": None, "infeasible": True},
}

EXPECTED_CATEGORY = {
    ("baseline", "iso"): "reversible",
    ("baseline", "EX_M"): "blocked",
    ("baseline", "EX_X"): "blocked",
    ("baseline", "cyc1"): "forward_only",
    ("route1_knockout", "syn1"): "blocked",
    ("route1_knockout", "iso"): "reverse_only",
    ("route2_knockout", "syn2"): "blocked",
    ("route2_knockout", "iso"): "forward_only",
    ("no_biomass", "iso"): "reversible",
    ("no_biomass", "GROWTH"): "blocked",
    ("no_biomass", "p1"): "blocked",
}


# ---------------------------------------------------------------- 独立 LP
def independent_build(net: dict):
    internal = [m["id"] for m in net["metabolites"] if not m["boundary"]]
    rxn_ids = [r["id"] for r in net["reactions"]]
    row = {m: i for i, m in enumerate(internal)}
    s = np.zeros((len(internal), len(rxn_ids)))
    for j, r in enumerate(net["reactions"]):
        for met, coef in r["stoichiometry"].items():
            if met in row:
                s[row[met], j] = coef
    bounds = [(r["lower"], r["upper"]) for r in net["reactions"]]
    return s, rxn_ids, bounds


def independent_fba(net, bounds):
    s, ids, _ = independent_build(net)
    c = np.zeros(len(ids))
    c[ids.index(net["biomass_reaction"])] = -1.0
    res = linprog(c=c, A_eq=s, b_eq=np.zeros(s.shape[0]),
                  bounds=bounds, method="highs")
    return res


def independent_fva(net, bounds, target, direction, fraction, z):
    s, ids, _ = independent_build(net)
    n = len(ids)
    c = np.zeros(n)
    j = ids.index(target)
    c[j] = 1.0 if direction == "min" else -1.0
    a_ub = np.zeros((1, n))
    a_ub[0, ids.index(net["biomass_reaction"])] = -1.0
    res = linprog(c=c, A_ub=a_ub, b_ub=[-fraction * z],
                  A_eq=s, b_eq=np.zeros(s.shape[0]),
                  bounds=bounds, method="highs")
    return res


def effective_bounds(net: dict, cond: dict):
    ko = set(cond.get("knockouts", []))
    out = []
    for r in net["reactions"]:
        rid = r["id"]
        if rid in ko:
            out.append((0.0, 0.0))
        elif rid in cond.get("replacements", {}):
            rep = cond["replacements"][rid]
            out.append((rep["lower"], rep["upper"]))
        else:
            out.append((r["lower"], r["upper"]))
    return out


# ---------------------------------------------------------------- 运行入口
def run_cli(net_path, cond_path, out_dir, fraction):
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--network", str(net_path),
         "--conditions", str(cond_path), "--out", str(out_dir),
         "--retain-fraction", str(fraction)],
        capture_output=True, text=True)
    return proc


def load_out(out_dir):
    with open(out_dir / "index.json", encoding="utf-8") as fh:
        index = json.load(fh)
    with open(out_dir / "fva_ranges.csv", encoding="utf-8") as fh:
        ranges = list(csv.DictReader(fh))
    with open(out_dir / "condition_summary.csv", encoding="utf-8") as fh:
        summary = list(csv.DictReader(fh))
    return index, ranges, summary


def main() -> int:
    net = json.load(open(DATA / "demo_network.json", encoding="utf-8"))
    conds = json.load(open(DATA / "demo_conditions.json", encoding="utf-8"))
    cond_by_id = {c["id"]: c for c in conds["conditions"]}

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        out10 = tmp / "r10"
        out05 = tmp / "r05"
        p10 = run_cli(DATA / "demo_network.json",
                      DATA / "demo_conditions.json", out10, 1.0)
        check(p10.returncode == 0, "retain=1.0 运行退出码为 0（条件级警告不算失败）")
        p05 = run_cli(DATA / "demo_network.json",
                      DATA / "demo_conditions.json", out05, 0.5)
        check(p05.returncode == 0, "retain=0.5 运行退出码为 0")

        index, ranges, summary = load_out(out10)
        index05, ranges05, _ = load_out(out05)

        # ---- 1) 手算 z 与 FBA 状态
        summ = {r["condition_id"]: r for r in summary}
        for cid, exp in EXPECTED.items():
            row = summ[cid]
            if exp.get("infeasible"):
                check(row["fba_status"] == "infeasible",
                      f"{cid}: 手算不可行，输出 infeasible")
                check(row["z"] == "", f"{cid}: 不可行不填 0，z 留空")
                check(row["fva_performed"] == "False",
                      f"{cid}: 不可行不做 FVA")
            else:
                check(row["fba_status"] == "optimal",
                      f"{cid}: FBA 状态 optimal")
                check(approx(float(row["z"]), exp["z"]),
                      f"{cid}: 手算 z*={exp['z']}，输出 {row['z']}")

        # ---- 2) retain=1.0 逐反应手算范围与分类
        rng = {(r["condition_id"], r["reaction_id"]): r for r in ranges}
        for cid, exp in EXPECTED.items():
            if exp.get("infeasible"):
                continue
            for rid, (lo, hi) in exp["retain_1"].items():
                row = rng[(cid, rid)]
                check(approx(float(row["min_value"]), lo),
                      f"[r=1.0] {cid}/{rid} 手算 min={lo}，输出 {row['min_value']}")
                check(approx(float(row["max_value"]), hi),
                      f"[r=1.0] {cid}/{rid} 手算 max={hi}，输出 {row['max_value']}")
                check(row["min_status"] == "optimal" and row["max_status"] == "optimal",
                      f"[r=1.0] {cid}/{rid} 两端点状态均 optimal")
        for (cid, rid), cat in EXPECTED_CATEGORY.items():
            check(rng[(cid, rid)]["category"] == cat,
                f"分类 {cid}/{rid} = {cat}，输出 {rng[(cid, rid)]['category']}")

        # ---- 3) retain=0.5 范围放宽（只核对手算交点子集）
        rng05 = {(r["condition_id"], r["reaction_id"]): r for r in ranges05}
        for cid, exp in EXPECTED.items():
            half = exp.get("retain_half")
            if not half:
                continue
            for rid, (lo, hi) in half.items():
                row = rng05[(cid, rid)]
                check(approx(float(row["min_value"]), lo)
                      and approx(float(row["max_value"]), hi),
                      f"[r=0.5] {cid}/{rid} 手算 [{lo},{hi}]，"
                      f"输出 [{row['min_value']},{row['max_value']}]")
        # 显式断言：降低 retain 后范围不窄于 retain=1.0
        for key in rng05:
            a, b = rng[key], rng05[key]
            if a["min_status"] == "optimal":
                check(float(b["min_value"]) <= float(a["min_value"]) + TOL
                      and float(b["max_value"]) >= float(a["max_value"]) - TOL,
                      f"放宽保留比例后 {key} 范围不缩小")

        # ---- 4) 独立 LP 重解每个有限端点（不走生产 solver）
        for cid, cond in cond_by_id.items():
            if EXPECTED[cid].get("infeasible"):
                res = independent_fba(net, effective_bounds(net, cond))
                check(res.status == 2, f"独立 LP 确认 {cid} 不可行")
                continue
            bounds = effective_bounds(net, cond)
            fba = independent_fba(net, bounds)
            z = -fba.fun
            check(approx(z, EXPECTED[cid]["z"]),
                  f"独立 LP FBA {cid}: z={z} == 手算 {EXPECTED[cid]['z']}")
            for rid in [r["id"] for r in net["reactions"]]:
                for direction in ("min", "max"):
                    ep = independent_fva(net, bounds, rid, direction, 1.0, z)
                    row = rng[(cid, rid)]
                    val = ep.x[[r["id"] for r in net["reactions"]].index(rid)]
                    col = f"{direction}_value"
                    check(approx(val, float(row[col])),
                          f"独立 LP {cid}/{rid}/{direction}={val} "
                          f"== 生产输出 {row[col]}")

        # ---- 5) 见证逐项检查（用 JSON 原网络重建矩阵，独立代回）
        s_mat, ids, base_bounds = independent_build(net)
        n_wit = 0
        for cond_dir in sorted((out10 / "conditions").iterdir()):
            cid = cond_dir.name
            bounds = effective_bounds(net, cond_by_id[cid])
            fba_w = cond_dir / "witnesses" / "fba.json"
            fva_w = cond_dir / "witnesses" / "fva.jsonl"
            if fba_w.exists():
                n_wit += 1
                w = json.load(open(fba_w, encoding="utf-8"))
                v = np.array([w["flux"][rid] for rid in ids])
                check(np.max(np.abs(s_mat @ v)) <= TOL,
                      f"见证 {cid}/fba 守恒残差 <= 容差")
                for j, (lo, hi) in enumerate(bounds):
                    check((lo is None or v[j] >= lo - TOL)
                          and (hi is None or v[j] <= hi + TOL),
                          f"见证 {cid}/fba 的 {ids[j]} 在有效上下界内")
                check(w["diagnostics"]["valid"] is True,
                      f"见证 {cid}/fba 标记 valid")
            if fva_w.exists():
                z = EXPECTED[cid]["z"]
                for line in open(fva_w, encoding="utf-8"):
                    w = json.loads(line)
                    n_wit += 1
                    v = np.array([w["flux"][rid] for rid in ids])
                    bal = np.max(np.abs(s_mat @ v))
                    check(bal <= TOL,
                          f"见证 {cid}/fva {w['reaction_id']}/{w['direction']} 守恒")
                    bm = v[ids.index(net["biomass_reaction"])]
                    check(bm >= 1.0 * z - TOL,
                          f"见证 {cid}/fva 满足生物量保留 v_bm={bm} >= z={z}")
                    for j, (lo, hi) in enumerate(bounds):
                        check((lo is None or v[j] >= lo - TOL)
                              and (hi is None or v[j] <= hi + TOL),
                              f"见证 {cid}/fva 的 {ids[j]} 界内")
                    # 见证必须真的 extremize 了目标反应（与独立 LP 端点一致）
                    ep = independent_fva(
                        net, bounds, w["reaction_id"], w["direction"], 1.0, z)
                    exp_val = ep.x[ids.index(w["reaction_id"])]
                    check(approx(v[ids.index(w["reaction_id"])], exp_val, 1e-6),
                          f"见证 {cid}/{w['reaction_id']}/{w['direction']} "
                          f"确实取到独立 LP 端点")
                    check(w["diagnostics"]["valid"] is True,
                          f"见证 {cid}/fva 标记 valid")
        n_feasible = sum(1 for c in EXPECTED.values() if not c.get("infeasible"))
        check(n_wit == n_feasible * (1 + 2 * len(ids)),
              f"见证总数 {n_wit} = {n_feasible} 个可行条件 × "
              f"(1 个 FBA + 2×{len(ids)} 个 FVA 端点)")

        # ---- 6) 条件顺序打乱：数值与状态一致
        shuffled = dict(conds)
        shuffled["conditions"] = list(reversed(conds["conditions"]))
        shuf_path = tmp / "shuffled_conditions.json"
        json.dump(shuffled, open(shuf_path, "w", encoding="utf-8"))
        out_shuf = tmp / "shuf"
        ps = run_cli(DATA / "demo_network.json", shuf_path, out_shuf, 1.0)
        check(ps.returncode == 0, "乱序条件运行退出码 0")
        _, ranges_s, summary_s = load_out(out_shuf)
        keyf = lambda r: (r["condition_id"], r["reaction_id"])
        for a, b in zip(sorted(ranges, key=keyf),
                        sorted(ranges_s, key=keyf)):
            check(a == b, f"乱序后范围一致: {keyf(a)}")
        for a, b in zip(sorted(summary, key=lambda r: r["condition_id"]),
                        sorted(summary_s, key=lambda r: r["condition_id"])):
            # order 记录输入位置，乱序后允许不同；其余字段必须逐项一致
            a2 = {k: v for k, v in a.items() if k != "order"}
            b2 = {k: v for k, v in b.items() if k != "order"}
            check(a2 == b2, f"乱序后条件摘要一致（除 order 外）: {a['condition_id']}")
            check(int(b["order"]) == len(summary_s) - 1 - int(a["order"]),
                  f"乱序后 {a['condition_id']} 的 order 按输入位置重排")

        # ---- 7) index 记录实际输入与参数
        check(index["inputs"]["network_sha256"] == index05["inputs"]["network_sha256"],
              "index 带输入 sha256")
        check(index["parameters"]["retain_fraction"] == 1.0
              and index05["parameters"]["retain_fraction"] == 0.5,
              "index 带实际 retain_fraction")
        check(index["parameters"]["tolerance"] == TOL,
              "index 带实际判零容差")

    # ---------------------------------------------------------------- 无界网络
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 7a) 目标无界
        p = run_cli(DATA / "unbounded_objective_network.json",
                    DATA / "unbounded_objective_conditions.json",
                    tmp / "ub", 1.0)
        check(p.returncode == 0, "无界目标网络退出码仍为 0（条件级状态）")
        idx, rng, summ = load_out(tmp / "ub")
        row = {r["condition_id"]: r for r in summ}
        check(row["obj_unbounded"]["fba_status"] == "unbounded",
              "无界目标状态为 unbounded 而非填 0")
        check(row["obj_unbounded"]["fva_performed"] == "False",
              "无界目标不做依赖有限 z 的 FVA")
        check(approx(float(row["capped"]["z"]), 7.0),
              "同一网络加上限后 z=7（证明无界源于缺失上界）")
        wit_dir = tmp / "ub" / "conditions" / "obj_unbounded" / "witnesses"
        check(not wit_dir.exists() or not any(wit_dir.iterdir()),
              "无界条件不产生伪造见证（witnesses 目录为空或不存在）")

        # 7b) 有限目标 + 无界循环
        run_cli(DATA / "unbounded_cycle_network.json",
                DATA / "unbounded_cycle_conditions.json",
                tmp / "cyc", 1.0)
        idx, rng, summ = load_out(tmp / "cyc")
        check(approx(float(summ[0]["z"]), 5.0), "循环网络目标有限 z=5")
        for r in rng:
            if r["reaction_id"] in ("lp", "lm"):
                check(r["min_status"] == "unbounded"
                      and r["max_status"] == "unbounded",
                      f"循环反应 {r['reaction_id']} 两端点均无界")
                check(r["category"] == "unbounded_both",
                      f"循环反应 {r['reaction_id']} 分类 unbounded_both")
                check(r["min_value"] == "" and r["max_value"] == "",
                      "无界端点不偷加大上限、不填数字")
            else:
                check(r["min_status"] == r["max_status"] == "optimal",
                      f"主通路反应 {r['reaction_id']} 端点照常算出")

    # ---------------------------------------------------------------- 坏输入
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        bad_net_cases = [
            ("bad_unknown_metabolite.json", "不存在的代谢物"),
            ("bad_bounds.json", "lower>upper"),
            ("bad_biomass_lower.json", "biomass 负下界"),
        ]
        for fname, label in bad_net_cases:
            p = run_cli(DATA / fname, DATA / "demo_conditions.json",
                        tmp / "x", 1.0)
            check(p.returncode == 2, f"坏网络（{label}）退出码 2")
            check(not (tmp / "x").exists(), "坏输入不发布产物目录")
        p = run_cli(DATA / "demo_network.json", DATA / "bad_conditions.json",
                    tmp / "y", 1.0)
        check(p.returncode == 2, "坏条件文件退出码 2")
        err = p.stderr
        check("条件" in err and ("conflict" in err or "ghost" in err or "dup" in err),
              f"坏条件报错带条件 id 与位置: {err.strip()}")
        # 三类坏条件分别单独成文件时都必须被定位
        for cid, reason in [("conflict", "同时替换又敲除"),
                            ("ghost", "未知反应"),
                            ("dup", "重复")]:
            raw = json.load(open(DATA / "bad_conditions.json", encoding="utf-8"))
            if cid == "dup":
                # 重复必须保留两个同 id 条件才能复现
                picked_list = [c for c in raw["conditions"] if c["id"] == "dup"]
            else:
                picked_list = [next(c for c in raw["conditions"] if c["id"] == cid)]
            f = tmp / f"bad_{cid}.json"
            json.dump({"conditions": picked_list}, open(f, "w", encoding="utf-8"))
            pp = run_cli(DATA / "demo_network.json", f, tmp / f"z_{cid}", 1.0)
            check(pp.returncode == 2 and cid in pp.stderr,
                  f"坏条件 {cid}（{reason}）出现时退出码 2 且带 id 定位")

    print(f"\n全部 {CHECKS} 项独立核验通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
