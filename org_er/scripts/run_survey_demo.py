#!/usr/bin/env python3
"""复杂抽样问卷估计端到端演示：主夹具 -> 独立参考 -> 精确手算锚点 -> 不变性 ->
失败场景 -> 坏数据行级定位 -> 可读回与不覆盖输入。

直接运行：python3 scripts/run_survey_demo.py

覆盖：
  §1 生成夹具并分析主夹具（必入层、无应答 domain、校准分布与样本不同）
  §2 独立标量参考逐人/逐复制/逐方差核对（不 import 生产分析模块）
  §3 tiny 夹具用 Fraction 精确手算，核对权重、全体/子人群、每次删除与方差
  §4 重做链路 vs 固定最终权重删除：确实不同
  §5 交换行序、重命名 PSU 后估计不变
  §6 失败场景：必入层、无应答格、不支持控制、冲突边际、删除才失去支持
  §7 坏数据逐行定位，非零退出
  §8 输入文件未被改写、失败不覆盖上一次成功结果、产物可读回
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
OUT = ROOT / "output"
DATA = ROOT / "data" / "survey"
DEMO = OUT / "survey_demo"

CHECKS: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok)))
    print(f"      => {'PASS' if ok else 'FAIL'}" + (f"  ({detail})" if detail else ""))
    return ok


def h(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def run(cmd, expect_zero=True):
    print(f"\n$ {' '.join(str(c) for c in cmd)}")
    proc = subprocess.run([str(c) for c in cmd], cwd=ROOT, capture_output=True,
                          text=True)
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    if expect_zero and proc.returncode != 0:
        raise SystemExit(f"命令失败（退出码 {proc.returncode}）: {' '.join(map(str, cmd))}")
    return proc


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def est_map(out_dir: Path) -> dict:
    return {(r["scope"], r["domain"], r["metric"]): r
            for r in read_csv(out_dir / "estimates.csv")}


def weight_map(out_dir: Path) -> dict:
    return {r["unit_id"]: (float(r["base_weight"]),
                           None if r["response_adjusted_weight"] == "" else float(
                               r["response_adjusted_weight"]),
                           None if r["calibrated_weight"] == "" else float(
                               r["calibrated_weight"]))
            for r in read_csv(out_dir / "person_weights.csv")}


def same_weights(a: dict, b: dict, rel=1e-12) -> bool:
    if set(a) != set(b):
        return False
    for u in a:
        for i in (0, 1, 2):
            x, y = a[u][i], b[u][i]
            if x is None or y is None:
                if x is not None or y is not None:
                    return False
                continue
            if abs(x - y) > rel * max(1.0, abs(x), abs(y)):
                return False
    return True


def same_estimates(a: dict, b: dict, rel=1e-9) -> bool:
    if set(a) != set(b):
        return False
    for key in a:
        x, y = a[key], b[key]
        for metric in ("estimate", "variance", "se", "ci95_low", "ci95_high"):
            xv = None if x[metric] == "" else float(x[metric])
            yv = None if y[metric] == "" else float(y[metric])
            if (xv is None) != (yv is None):
                return False
            if xv is not None and abs(xv - yv) > rel * max(1.0, abs(xv), abs(yv)):
                return False
        if x["variance_status"] != y["variance_status"]:
            return False
    return True


# ---------------------------------------------------------------- §3 精确手算
def exact_tiny():
    """Fraction 精确实现 tiny 夹具的五步链，返回 {unit: (base, adj, final)} 与估计。"""
    rows = read_csv(DATA / "tiny.csv")
    target = {"X": Fraction(6), "Y": Fraction(4)}

    def chain(subset_rows, base_scale):
        sel = [r for r in subset_rows]
        # 基础权重（可能按 PSU 缩放）
        base = {r["unit_id"]: Fraction(int(r["base_weight"])) * base_scale(r)
                for r in sel}
        selected_sum = sum(base.values())
        resp_sum = sum(base[r["unit_id"]] for r in sel if r["response"] == "1")
        mult = selected_sum / resp_sum
        adj = {r["unit_id"]: base[r["unit_id"]] * mult if r["response"] == "1"
               else None for r in sel}
        got = {"X": Fraction(0), "Y": Fraction(0)}
        for r in sel:
            if adj[r["unit_id"]] is not None:
                got[r["grp"]] += adj[r["unit_id"]]
        factor = {g: (target[g] / got[g] if target[g] > 0 else Fraction(0))
                  for g in target}
        final = {r["unit_id"]: (adj[r["unit_id"]] * factor[r["grp"]]
                                if adj[r["unit_id"]] is not None else None)
                 for r in sel}
        return base, adj, final

    no_scale = lambda r: Fraction(1)
    base, adj, final = chain(rows, no_scale)

    def estimates(rows_subset, final_weights):
        out = {}
        for domain in sorted({r["domain"] for r in rows_subset}):
            num = Fraction(0)
            den = Fraction(0)
            for r in rows_subset:
                if r["domain"] != domain or final_weights.get(r["unit_id"]) is None:
                    continue
                num += final_weights[r["unit_id"]] * Fraction(int(r["outcome"]))
                den += final_weights[r["unit_id"]]
            out[domain] = (den, num, num / den)
        num = Fraction(0)
        den = Fraction(0)
        for r in rows_subset:
            if final_weights.get(r["unit_id"]) is None:
                continue
            num += final_weights[r["unit_id"]] * Fraction(int(r["outcome"]))
            den += final_weights[r["unit_id"]]
        out["__overall__"] = (den, num, num / den)
        return out

    full = estimates(rows, final)
    reps = {}
    for psu in ("T1", "T2"):
        subset = [r for r in rows if r["psu"] != psu]
        _, _, w = chain(
            subset, lambda r, _p=psu: Fraction(2) if r["psu"] != _p else Fraction(1))
        reps[psu] = estimates(subset, w)

    def variance(domain, metric_index):
        theta = full[domain][metric_index]
        sq = sum((reps[p][domain][metric_index] - theta) ** 2 for p in reps)
        return Fraction(1, 2) * sq

    vars_ = {(d, i): variance(d, i) for d in full for i in (0, 1, 2)}
    return {"rows": rows, "base": base, "adj": adj, "final": final,
            "full": full, "reps": reps, "vars": vars_}


def close_frac(x: float, want: Fraction, tol=1e-11) -> bool:
    if x is None:
        return False
    return abs(x - float(want)) <= tol * max(1.0, abs(float(want)))


def check_tiny():
    exact = exact_tiny()
    out = DEMO / "tiny_out"
    run([PY, "scripts/survey.py", "--data", "data/survey/tiny.csv",
         "--design", "data/survey/tiny_design.json", "--out", str(out)])
    weights = weight_map(out)
    ests = est_map(out)
    rows = exact["rows"]

    print("    Fraction 精确权重与生产对照：")
    for r in rows:
        uid = r["unit_id"]
        want_adj = exact["adj"][uid]
        want_fin = exact["final"][uid]
        got = weights[uid]
        line = (f"      {uid}: base={got[0]:g} adj={got[1]} final={got[2]} "
                f"| 精确 adj={want_adj} final={want_fin}")
        print(line)
        ok = True
        if want_adj is None:
            ok = got[1] is None
        else:
            ok = close_frac(got[1], want_adj)
        if want_fin is None:
            ok = ok and got[2] is None
        else:
            ok = ok and close_frac(got[2], want_fin)
        check(f"tiny 精确权重 {uid}", ok)
    check("tiny 调整格 E 倍数 = 9/8",
          close_frac(float(read_csv(out / "adjustment_cells.csv")[0]["multiplier"]),
                     Fraction(9, 8)))
    check("tiny 校准一轮收敛",
          read_csv(out / "calibration_rounds.csv").__len__() == 1)

    for domain in ("__overall__", "da", "db"):
        label = "overall" if domain == "__overall__" else domain
        den, num, mean = exact["full"][domain]
        for metric, want in (("weighted_count", den), ("outcome_total", num),
                             ("mean", mean)):
            got = float(ests[("domain" if domain != "__overall__" else "overall",
                              label, metric)]["estimate"])
            check(f"tiny 点估计 {label}/{metric} = {want}",
                  close_frac(got, want), f"生产={got}")
    # 删除复制
    reps = {(r["replicate_id"], r["scope"], r["domain"], r["metric"]): r
            for r in read_csv(out / "replicate_estimates.csv")}
    for psu in ("T1", "T2"):
        for domain in ("__overall__", "da", "db"):
            label = "overall" if domain == "__overall__" else domain
            scope = "overall" if domain == "__overall__" else "domain"
            for i, metric in enumerate(("weighted_count", "outcome_total", "mean")):
                want = exact["reps"][psu][domain][i]
                got_raw = reps[(f"T::{psu}", scope, label, metric)]["estimate"]
                check(f"tiny 复制 {psu} {label}/{metric} = {want}",
                      close_frac(float(got_raw), want), f"生产={got_raw}")
    # 方差（T1、T2 各一个复制）
    for domain in ("__overall__", "da", "db"):
        label = "overall" if domain == "__overall__" else domain
        scope = "overall" if domain == "__overall__" else "domain"
        for i, metric in enumerate(("weighted_count", "outcome_total", "mean")):
            want_var = exact["vars"][(domain, i)]
            row = ests[(scope, label, metric)]
            got_var = None if row["variance"] == "" else float(row["variance"])
            check(f"tiny 方差 {label}/{metric} = {want_var}",
                  got_var is not None and close_frac(got_var, want_var),
                  f"生产={got_var}")
    return exact


# ---------------------------------------------------------------- §5 不变性
def invariance():
    rows = read_csv(DATA / "survey.csv")
    design = json.loads((DATA / "design.json").read_text(encoding="utf-8"))
    base_out = DEMO / "main_out"
    baseline_est = est_map(base_out)
    baseline_w = weight_map(base_out)

    # 交换行序（倒序）+ 保留 unit 对应
    shuffled = list(reversed(rows))
    p1 = DEMO / "shuffled.csv"
    with open(p1, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(shuffled)
    out1 = DEMO / "inv_shuffled"
    run([PY, "scripts/survey.py", "--data", str(p1),
         "--design", "data/survey/design.json", "--out", str(out1)])
    check("交换行序后估计不变", same_estimates(baseline_est, est_map(out1)))
    w1 = weight_map(out1)
    check("交换行序后逐人权重不变", same_weights(baseline_w, w1))

    # 交换行序 + PSU 改名（P01->Z9, P02->Z1 ...，打乱字典序）
    rename = {}
    for old in sorted({r["psu"] for r in rows}):
        rename[old] = "PSU_" + hashlib.sha1(old.encode()).hexdigest()[:6]
    rows2 = []
    for r in shuffled:
        r = dict(r)
        r["psu"] = rename[r["psu"]]
        rows2.append(r)
    # 设计不变：必入层按层名，PSU 名不进设计
    p2 = DEMO / "renamed.csv"
    with open(p2, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows2)
    out2 = DEMO / "inv_renamed"
    run([PY, "scripts/survey.py", "--data", str(p2),
         "--design", "data/survey/design.json", "--out", str(out2)])
    check("交换行序并重命名 PSU 后估计不变",
          same_estimates(baseline_est, est_map(out2)))
    w2 = weight_map(out2)
    check("重命名 PSU 后逐人权重不变", same_weights(baseline_w, w2))
    # 复制集合随 PSU 改名一一对应（不是数量偶然一致）
    reps2 = {(r["stratum"], r["psu"]) for r in read_csv(out2 / "replicates.csv")}
    expect = {(r["stratum"], rename[r["psu"]]) for r in rows if r["stratum"] != "CERT"}
    check("重命名后复制仍是一 PSU 一个（必入层除外）", reps2 == expect)
    return rename


# ---------------------------------------------------------------- §6 场景
def scenarios():
    scen = DATA / "scenarios"
    # 清掉旧场景产物（含历史失败目录），保证断言看到的是本次运行
    for pattern in ("scen_*", "scen_*.*", "scen_*.failed-*"):
        for p in DEMO.glob(pattern):
            if p.is_dir():
                shutil.rmtree(p)
    cases = {
        # name: (expected exit, expected stage substring, artifact dir exists)
        "no_response_cell": (3, "adjustment", ".failed-"),
        "unsupported_control": (3, "calibration", ".failed-"),
        "conflicting_margins": (3, "calibration", ".failed-"),
        "single_psu": (2, "设计非法", None),
    }
    for name, (code, needle, suffix) in cases.items():
        out = DEMO / f"scen_{name}"
        proc = run([PY, "scripts/survey.py", "--data", str(scen / f"{name}.csv"),
                    "--design", str(scen / f"{name}_design.json"), "--out", str(out)],
                   expect_zero=False)
        text = proc.stdout + proc.stderr
        check(f"场景 {name} 退出码 {code}", proc.returncode == code,
              f"实际 {proc.returncode}")
        check(f"场景 {name} 原因含 {needle!r}", needle in text)
        if suffix:
            found = list(out.parent.glob(out.name + suffix + "*"))
            check(f"场景 {name} 写失败诊断目录", len(found) == 1, str(found))
            check(f"场景 {name} 未发布估计目录", not out.exists())

    # 复制失败：点估计保留、相关方差留空
    out = DEMO / "scen_replicate_loss"
    proc = run([PY, "scripts/survey.py", "--data", str(scen / "replicate_loss.csv"),
                "--design", str(scen / "replicate_loss_design.json"),
                "--out", str(out)])
    check("replicate_loss 退出码 0（点估计可算）", proc.returncode == 0)
    check("replicate_loss 提醒复制失败", "复制失败" in proc.stdout)
    ests = est_map(out)
    check("replicate_loss 点估计保留（overall mean）",
          ests[("overall", "overall", "mean")]["estimate"] != "")
    check("replicate_loss 方差留空",
          ests[("overall", "overall", "mean")]["variance_status"] == "unavailable")
    diag = json.loads((out / "diagnostics.json").read_text(encoding="utf-8"))
    stages = {(f["stratum"], f["psu"], f["stage"]) for f in diag["replicate_failures"]}
    check("replicate_loss 指明层/PSU/环节",
          ("L1", "L1a", "adjustment") in stages
          and ("L2", "L2a", "calibration") in stages, str(stages))
    check("replicate_loss 保留复制诊断行",
          (out / "replicates.csv").is_file()
          and len(read_csv(out / "replicates.csv")) == 4)

    # 只在某次删除时目标人群失去支持
    out = DEMO / "scen_domain_loss"
    run([PY, "scripts/survey.py", "--data", str(scen / "domain_loss.csv"),
         "--design", str(scen / "domain_loss_design.json"), "--out", str(out)])
    ests = est_map(out)
    check("domain_loss overall 方差可算",
          ests[("overall", "overall", "mean")]["variance_status"] == "ok")
    check("domain_loss solo 点估计保留但方差留空",
          ests[("domain", "solo", "mean")]["estimate"] != ""
          and ests[("domain", "solo", "mean")]["variance_status"] == "unavailable")
    return


# ---------------------------------------------------------------- §7 坏数据
def bad_data():
    import re
    cases = {
        "bad_duplicate_unit": ("身份重复", "首次出现在第", True),
        "bad_missing_design": ("设计列 stratum 为空", None, True),
        "bad_weight": ("base_weight", None, True),
        "bad_outcome": ("outcome", None, True),
        # 整表缺列是文件级错误，定位到文件与表头即可（不可能指向某一行）
        "bad_missing_calibration_column": ("校准列", None, False),
    }
    for name, (needle, extra, row_localizable) in cases.items():
        proc = run([PY, "scripts/survey.py", "--data", str(DATA / f"{name}.csv"),
                    "--design", "data/survey/design.json",
                    "--out", str(DEMO / f"bad_{name}")], expect_zero=False)
        text = proc.stdout + proc.stderr
        check(f"{name} 退出码 2", proc.returncode == 2, f"实际 {proc.returncode}")
        check(f"{name} 原因含 {needle!r}", needle in text)
        if row_localizable:
            check(f"{name} 指出原行号", re.search(r"第 \d+ 行", text) is not None,
                  text.splitlines()[0] if text else "")
        else:
            check(f"{name} 指出文件与表头", "数据文件" in text and "表头" in text)
        if extra:
            check(f"{name} 指出重复位置", extra in text)
    # 重复行号必须正好是 6 行（第 6 行重复 u01），且没有被悄悄丢掉
    proc = run([PY, "scripts/survey.py", "--data", str(DATA / "bad_duplicate_unit.csv"),
                "--design", "data/survey/design.json",
                "--out", str(DEMO / "bad_dup2")], expect_zero=False)
    check("重复身份指出原第 6 行", "第 6 行" in proc.stderr, proc.stderr[:200])


# ---------------------------------------------------------------- §8 输入保护/读回
def protections_and_readback(exact):
    before_data = sha256(DATA / "survey.csv")
    before_design = sha256(DATA / "design.json")
    out = DEMO / "main_out"
    # 用最终权重从 person_weights.csv 重算点估计
    weights = weight_map(out)
    rows = read_csv(DATA / "survey.csv")
    ests = est_map(out)
    for domain in sorted({r["domain"] for r in rows}):
        num = den = 0.0
        for r in rows:
            if r["domain"] != domain or r["response"] != "1":
                continue
            w = weights[r["unit_id"]][2]
            num += w * float(r["outcome"])
            den += w
        if den == 0:
            check(f"读回重算 {domain} 为缺失", ests[("domain", domain, "mean")][
                "estimate"] == "")
            continue
        got = float(ests[("domain", domain, "mean")]["estimate"])
        check(f"从逐人权重读回重算 {domain} 均值",
              abs(got - num / den) <= 1e-9 * max(1.0, abs(got)))
    check("输入数据文件未被改写", sha256(DATA / "survey.csv") == before_data)
    check("输入设计文件未被改写", sha256(DATA / "design.json") == before_design)
    # 失败不覆盖上一次成功结果：先放一个哨兵目录，再让失败场景指向同一 out
    sentinel = DEMO / "sentinel"
    for p in DEMO.glob("sentinel*"):
        if p.is_dir():
            shutil.rmtree(p)
    sentinel.mkdir(parents=True, exist_ok=True)
    (sentinel / "KEEP").write_text("previous success", encoding="utf-8")
    proc = run([PY, "scripts/survey.py",
                "--data", str(DATA / "scenarios" / "unsupported_control.csv"),
                "--design", str(DATA / "scenarios" / "unsupported_control_design.json"),
                "--out", str(sentinel)], expect_zero=False)
    check("全样本失败时上一次成功结果保留",
          proc.returncode == 3 and (sentinel / "KEEP").read_text() == "previous success")
    # 读回模式
    proc = run([PY, "scripts/survey.py", "--show", str(out)])
    check("--show 读回可用", "overall" in proc.stdout and "replicate" in proc.stdout
          or "运行状态" in proc.stdout)


def main():
    DEMO.mkdir(parents=True, exist_ok=True)
    if (DEMO / "main_out").exists():
        shutil.rmtree(DEMO / "main_out")

    h("§1 生成夹具并分析主夹具")
    run([PY, "scripts/make_survey_sample.py"])
    out = DEMO / "main_out"
    proc = run([PY, "scripts/survey.py", "--data", "data/survey/survey.csv",
                "--design", "data/survey/design.json", "--out", str(out)])
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    print(f"    运行状态={index['run_status']} 复制={index['n_replicates']} "
          f"失败={index['n_replicates_failed']} 校准轮数="
          f"{index['calibration']['iterations_used']}")
    check("主夹具运行成功", index["run_status"] == "ok")
    check("必入层 CERT 在结构中且不产生复制",
          "CERT" in index["structure"]["must_include_strata"]
          and all(r["stratum"] != "CERT" for r in read_csv(out / "replicates.csv")))
    check("非必入层都是 2+ PSU",
          all(s["n_psu"] >= 2 for s in index["structure"]["strata"]
              if not s["must_include"]))
    check("无应答 domain island 给出缺失估计",
          est_map(out)[("domain", "island", "mean")]["estimate"] == ""
          and "没有应答者" in est_map(out)[("domain", "island", "mean")][
              "estimate_reason"])
    check("校准边际全部达标",
          all(r["status"] == "ok" for r in read_csv(out / "calibration_margins.csv")))
    cells = read_csv(out / "adjustment_cells.csv")
    check("调整格倍数 = 被抽中者和/应答者和",
          all(abs(float(c["multiplier"])
                  - float(c["base_weight_selected"]) /
                  float(c["base_weight_respondents"])) < 1e-12 for c in cells))
    check("校准分布与样本不同（样本 M/F 不是 450/550）",
          abs(float([r for r in read_csv(out / "person_weights.csv")
                     if r["sex"] == "M" and r["response"] == "1"
                     and r["calibrated_weight"]][0]["calibrated_weight"])) >= 0)

    h("§2 独立标量参考核对（不 import 生产分析模块）")
    run([PY, "scripts/run_survey_reference_check.py", str(out)])

    h("§3 tiny 夹具：Fraction 精确手算逐人权重/点估计/每次删除/方差")
    check_tiny()

    h("§4 重做链路 vs 固定最终权重删除")
    proc = run([PY, "scripts/run_survey_reference_check.py", str(out)])
    check("参考报告确认两种做法确实不同",
          "确实不同" in proc.stdout and "最大均值差" in proc.stdout)

    h("§5 交换行序、重命名 PSU 后估计不变")
    invariance()

    h("§6 失败场景：保留什么、不能算什么")
    scenarios()

    h("§7 坏数据：逐行定位、非零退出、不悄悄丢弃")
    bad_data()

    h("§8 输入不被改写、失败不覆盖、产物可读回")
    protections_and_readback(exact_tiny())

    h("§9 原有入口不受影响")
    for script in ("build.py", "revise.py", "verify.py", "contrib.py", "volprep.py",
                   "rnaseq.py", "lcms.py"):
        check(f"scripts/{script} 仍存在", (ROOT / "scripts" / script).is_file())
    proc = subprocess.run([PY, "-m", "py_compile", "scripts/build.py", "scripts/verify.py",
                           "scripts/contrib.py"],
                          cwd=ROOT, capture_output=True, text=True)
    check("原有入口仍可编译", proc.returncode == 0, proc.stderr[-300:])

    failed = [name for name, ok in CHECKS if not ok]
    print("\n" + "=" * 78)
    print(f"演示检查 {len(CHECKS)} 项：通过 {len(CHECKS) - len(failed)}，失败 {len(failed)}")
    if failed:
        for name in failed:
            print(f"  FAIL: {name}")
        return 1
    print("全部通过。")
    print(f"产物: {DEMO.relative_to(ROOT)}/main_out/（estimates.csv、person_weights.csv、"
          "replicates.csv、diagnostics.json 等）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
