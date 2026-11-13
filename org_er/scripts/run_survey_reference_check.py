#!/usr/bin/env python3
"""独立标量参考：逐人手算权重、点估计、每次删除复制与 jackknife 方差。

**不 import 任何生产分析模块**（只用标准库 csv/json/math）：权重、应答调整、
IPF 校准、点估计、删除复制与方差全部在本文件里按定义用标量循环独立实现，
用来约束 ``org_er.survey`` 的生产结果。参考实现刻意用最直白的写法（不追求性能）。

用法:
  python3 scripts/run_survey_reference_check.py [output/survey]

核对内容：
  1. 逐人基础 / 应答调整后 / 校准后权重；
  2. 全体与每个 domain 的加权人数、outcome 总量、均值；
  3. 每个删除复制（重新做应答调整与校准）的全体/各 domain 估计；
  4. 每个估计量的 jackknife 方差、标准误与 95% 区间；
  5. 必入层不产生复制；方差只对非必入层求和；
  6. “固定最终权重删除”对照确实与重做链路不同。
"""
from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOL = 1e-9
Z = 1.96
METRICS = ("weighted_count", "outcome_total", "mean")


# ---------------------------------------------------------------- 读入
def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_inputs(data_path: Path, design_path: Path):
    design = json.loads(design_path.read_text(encoding="utf-8"))
    rows = read_csv(data_path)
    people = []
    for i, r in enumerate(rows):
        people.append({
            "unit_id": r["unit_id"],
            "stratum": r["stratum"],
            "psu": r["psu"],
            "base_weight": float(r["base_weight"]),
            "response": r["response"].strip() == "1",
            "cell": r["adjustment_cell"],
            "domain": r["domain"],
            "outcome": float(r["outcome"]) if r["response"].strip() == "1" else None,
            "cal": {c["name"]: r[c["name"]] for c in design["calibration"]["columns"]},
            "order": i,
        })
    return design, people


# ---------------------------------------------------------------- 标量权重链
def adjust(people, base):
    """标量应答调整：每格 被抽中者基础和 / 应答者基础和。"""
    selected = {}
    respondents = {}
    for p, w in zip(people, base):
        selected.setdefault(p["cell"], []).append(w)
        if p["response"]:
            respondents.setdefault(p["cell"], []).append(w)
    mult = {}
    for cell, vals in selected.items():
        if cell not in respondents:
            raise ReferenceFailure(f"调整格 {cell} 无应答者")
        mult[cell] = sum(vals) / sum(respondents[cell])
    return [w * mult[p["cell"]] if p["response"] else None
            for p, w in zip(people, base)]


def margins(people, weights, design):
    out = {}
    for c in design["calibration"]["columns"]:
        vals = {cat: 0.0 for cat in c["categories"]}
        for p, w in zip(people, weights):
            if p["response"]:
                vals[p["cal"][c["name"]]] += w
        out[c["name"]] = vals
    return out


def max_rel_error(people, weights, design):
    got = margins(people, weights, design)
    worst = 0.0
    for c in design["calibration"]["columns"]:
        for cat, target in c["categories"].items():
            g = got[c["name"]][cat]
            if target > 0:
                worst = max(worst, abs(g - target) / target)
            elif g > 0:
                worst = math.inf
    return worst


class ReferenceFailure(Exception):
    pass


def calibrate(people, weights, design):
    """标量 IPF：按列顺序轮流缩放，整轮检查收敛。"""
    tol = float(design["tolerance"])
    max_iter = int(design["max_iterations"])
    w = list(weights)
    for _ in range(max_iter):
        for c in design["calibration"]["columns"]:
            got = {cat: 0.0 for cat in c["categories"]}
            for p, wi in zip(people, w):
                if p["response"]:
                    got[p["cal"][c["name"]]] += wi
            factor = {}
            for cat, target in c["categories"].items():
                if target > 0 and got[cat] <= 0:
                    raise ReferenceFailure(f"零分母/无支持: {c['name']}={cat}")
                if target > 0:
                    factor[cat] = target / got[cat]
                else:
                    if got[cat] > 0:
                        raise ReferenceFailure(f"目标为 0 却有样本: {c['name']}={cat}")
                    factor[cat] = 0.0
            w = [wi * factor[p["cal"][c["name"]]] if (p["response"] and wi is not None)
                 else wi for p, wi in zip(people, w)]
        if max_rel_error(people, w, design) < tol:
            return w
    raise ReferenceFailure("超过迭代上限")


def estimate(people, weights, subset, domain):
    """子集 = 满足条件的 (p, w)；domain 只用于打标签。"""
    num = 0.0
    den = 0.0
    n_sel = 0
    n_resp = 0
    for p, w in subset:
        n_sel += 1
        if p["response"] and w is not None:
            n_resp += 1
            num += w * p["outcome"]
            den += w
    if n_resp == 0:
        return {"weighted_count": None, "outcome_total": None, "mean": None,
                "n_selected": n_sel, "n_respondents": 0}
    return {"weighted_count": den, "outcome_total": num, "mean": num / den,
            "n_selected": n_sel, "n_respondents": n_resp}


def all_estimates(people, weights, domains):
    out = {"__overall__": estimate(people, weights, list(zip(people, weights)),
                                  "__overall__")}
    for d in domains:
        subset = [(p, w) for p, w in zip(people, weights) if p["domain"] == d]
        out[d] = estimate(people, weights, subset, d)
    return out


def stratified_delete_plan(people, design):
    strata = {}
    for p in people:
        strata.setdefault(p["stratum"], set()).add(p["psu"])
    must = set(design.get("must_include_strata", []))
    plan = []
    for h in sorted(strata):
        if h in must:
            continue
        for psu in sorted(strata[h]):
            plan.append((h, psu, len(strata[h])))
    return plan


def run_replicate(people, base, design, h, psu, m, domains):
    """标量复制：删 PSU -> 同层基础权重乘 m/(m-1) -> 重新调整 -> 重新校准 -> 估计。"""
    s = m / (m - 1)
    keep = []
    sub_base = []
    for p, w in zip(people, base):
        if p["stratum"] == h and p["psu"] == psu:
            continue
        keep.append(p)
        sub_base.append(w * s if p["stratum"] == h else w)
    weights = adjust(keep, sub_base)
    weights = calibrate(keep, weights, design)
    return all_estimates(keep, weights, domains)


def jackknife_variances(full, replicates, plan, domains):
    """按层求和 (m-1)/m Σ(θ_r - θ)²；任何复制缺失 -> 该估计量方差 None。"""
    out = {}
    for d in ["__overall__"] + domains:
        for metric in METRICS:
            theta = full[d][metric]
            if theta is None:
                out[(d, metric)] = None
                continue
            total = 0.0
            ok = True
            for h in sorted({x[0] for x in plan}):
                m = next(x[2] for x in plan if x[0] == h)
                sq = []
                for (hh, psu, _mm), rep in zip(plan, replicates):
                    if hh != h:
                        continue
                    if rep is None or rep[d][metric] is None:
                        ok = False
                        break
                    sq.append((rep[d][metric] - theta) ** 2)
                if not ok:
                    break
                total += (m - 1) / m * sum(sq)
            out[(d, metric)] = math.sqrt(total) if ok else None
    return out


def fixed_final_weights(full_weights, people, plan, domains):
    """对照：不重做链路，只把全样本最终权重乘删除系数（错误做法）。"""
    out = {}
    for h, psu, m in plan:
        w = []
        s = m / (m - 1)
        for p, wi in zip(people, full_weights):
            if p["stratum"] == h and p["psu"] == psu:
                continue
            w.append(wi * s if (p["stratum"] == h and wi is not None) else wi)
        keep = [p for p in people if not (p["stratum"] == h and p["psu"] == psu)]
        out[(h, psu)] = all_estimates(keep, w, domains)
    return out


# ---------------------------------------------------------------- 主流程
def main(argv) -> int:
    out_dir = Path(argv[1]) if len(argv) > 1 else ROOT / "output" / "survey"
    index = json.loads((out_dir / "index.json").read_text(encoding="utf-8"))
    design, people = load_inputs(Path(index["data_path"]),
                                 Path(index["design_path"]))
    domains = sorted({p["domain"] for p in people})
    failures = []
    checks = 0

    def chk(name, ok, detail=""):
        nonlocal checks
        checks += 1
        if not ok:
            failures.append(f"{name}: {detail}")

    def close(a, b, tol=TOL):
        if a is None or b is None:
            return a is None and b is None
        return abs(a - b) <= tol * max(1.0, abs(a), abs(b))

    # 参考链路
    base = [p["base_weight"] for p in people]
    ref_adjust = adjust(people, base)
    ref_weights = calibrate(people, ref_adjust, design)
    full = all_estimates(people, ref_weights, domains)
    plan = stratified_delete_plan(people, design)
    ref_reps = []
    for h, psu, m in plan:
        try:
            ref_reps.append(run_replicate(people, base, design, h, psu, m, domains))
        except ReferenceFailure:
            ref_reps.append(None)
    ref_se = jackknife_variances(full, ref_reps, plan, domains)
    naive = fixed_final_weights(ref_weights, people, plan, domains)

    # 1) 逐人权重
    prod_people = read_csv(out_dir / "person_weights.csv")
    by_unit = {r["unit_id"]: r for r in prod_people}
    for p, w_adj, w_fin in zip(people, ref_adjust, ref_weights):
        row = by_unit[p["unit_id"]]
        chk(f"base weight {p['unit_id']}",
            close(float(row["base_weight"]), p["base_weight"]))
        got_adj = None if row["response_adjusted_weight"] == "" else float(
            row["response_adjusted_weight"])
        got_fin = None if row["calibrated_weight"] == "" else float(
            row["calibrated_weight"])
        chk(f"adjusted weight {p['unit_id']}", close(got_adj, w_adj),
            f"生产={got_adj} 参考={w_adj}")
        chk(f"calibrated weight {p['unit_id']}", close(got_fin, w_fin),
            f"生产={got_fin} 参考={w_fin}")

    # 2) 点估计
    prod_est = {}
    for r in read_csv(out_dir / "estimates.csv"):
        if r["estimate_status"] != "ok":
            prod_est[(r["scope"], r["domain"], r["metric"])] = None
        else:
            prod_est[(r["scope"], r["domain"], r["metric"])] = float(r["estimate"])
        prod_est[(r["scope"], r["domain"], r["metric"], "var")] = (
            None if r["variance"] == "" else float(r["variance"]))
        prod_est[(r["scope"], r["domain"], r["metric"], "se")] = (
            None if r["se"] == "" else float(r["se"]))
    for d in ["__overall__", *domains]:
        scope = "overall" if d == "__overall__" else "domain"
        for metric in METRICS:
            lab = "overall" if d == "__overall__" else d
            got = prod_est.get((scope, lab, metric))
            want = full[d][metric]
            chk(f"point estimate {scope}/{d}/{metric}", close(got, want),
                f"生产={got} 参考={want}")

    # 3) 复制估计
    prod_reps = {}
    for r in read_csv(out_dir / "replicate_estimates.csv"):
        prod_reps[(r["replicate_id"], r["scope"], r["domain"], r["metric"])] = (
            None if r["estimate"] == "" else float(r["estimate"]))
    chk("replicate count", len(plan) == len(ref_reps), f"{len(plan)} vs {len(ref_reps)}")
    for (h, psu, m), rep in zip(plan, ref_reps):
        rid = f"{h}::{psu}"
        for d in ["__overall__", *domains]:
            scope = "overall" if d == "__overall__" else "domain"
            for metric in METRICS:
                lab = "overall" if d == "__overall__" else d
                got = prod_reps.get((rid, scope, lab, metric))
                want = None if rep is None else rep[d][metric]
                chk(f"replicate {rid} {scope}/{d}/{metric}", close(got, want),
                    f"生产={got} 参考={want}")

    # 4) 方差
    for d in ["__overall__", *domains]:
        scope = "overall" if d == "__overall__" else "domain"
        for metric in METRICS:
            want_se = ref_se[(d, metric)]
            lab = "overall" if d == "__overall__" else d
            got_se = prod_est.get((scope, lab, metric, "se"))
            got_var = prod_est.get((scope, lab, metric, "var"))
            chk(f"SE {scope}/{d}/{metric}", close(got_se, want_se),
                f"生产={got_se} 参考={want_se}")
            chk(f"variance {scope}/{d}/{metric}",
                close(got_var, None if want_se is None else want_se ** 2),
                f"生产={got_var} 参考={None if want_se is None else want_se ** 2}")
            # 区间端点
            point = full[d][metric]
            if want_se is not None and point is not None:
                lo = point - Z * want_se
                hi = point + Z * want_se
                domain_label = "overall" if d == "__overall__" else d
                prow = next(r for r in read_csv(out_dir / "estimates.csv")
                            if r["scope"] == scope
                            and r["domain"] == domain_label
                            and r["metric"] == metric)
                chk(f"CI low {scope}/{d}/{metric}",
                    close(float(prow["ci95_low"]), lo))
                chk(f"CI high {scope}/{d}/{metric}",
                    close(float(prow["ci95_high"]), hi))

    # 5) 必入层没有复制，方差只加非必入层
    must = set(design.get("must_include_strata", []))
    prod_rep_rows = read_csv(out_dir / "replicates.csv")
    chk("no replicates for must-include strata",
        all(r["stratum"] not in must for r in prod_rep_rows))
    chk("one replicate per non-must-include PSU", len(prod_rep_rows) == len(plan))

    # 6) 与“固定最终权重删除”对照不同（只核对能算的复制）
    diffs = []
    for (h, psu, m), rep in zip(plan, ref_reps):
        if rep is None:
            continue
        for d in ["__overall__", *domains]:
            a = rep[d]["mean"]
            b = naive[(h, psu)][d]["mean"]
            if a is not None and b is not None:
                diffs.append(abs(a - b))
    chk("fixed-final-weight deletion differs from re-chained jackknife",
        bool(diffs) and max(diffs) > 1e-6,
        f"最大差 {max(diffs) if diffs else 'n/a'}")
    max_diff = max(diffs) if diffs else 0.0

    print(f"独立标量参考核对完成：{checks} 项检查（数据 {len(people)} 人，"
          f"{len(plan)} 个删除复制）")
    if failures:
        print("失败:", file=sys.stderr)
        for f in failures:
            print("  -", f, file=sys.stderr)
        return 1
    print("全部通过：逐人权重、全体/子人群点估计、每次删除后的估计、方差/SE/区间、"
          "必入层规则均与独立参考一致")
    print(f"重做链路 vs 固定最终权重删除：最大均值差 {max_diff:.6g}"
          "（确实不同，不能拿固定权重凑方差）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
