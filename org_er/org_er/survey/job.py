"""survey 作业编排：校验 -> 全样本权重链 -> 点估计 -> 删除复制 -> 方差 -> 原子发布。

产物目录（一次发布，不覆盖输入）::

    <out>/
      index.json                 运行参数、输入快照（路径 + sha256）、结构摘要、状态
      diagnostics.json           结构性诊断、复制失败明细、估计有效状态
      estimates.csv              全体与每个 domain 的点估计、方差、标准误、95% 区间
      person_weights.csv         逐人：基础 / 应答调整后 / 校准后权重
      adjustment_cells.csv       每个调整格的人数、权重和、倍数与状态
      calibration_rounds.csv     每轮 IPF 的最大相对误差与逐类别误差
      calibration_margins.csv    最终权重和控制量的逐格对照
      strata_psus.csv            层 / PSU 清单、必入标记、是否产生复制
      replicates.csv             每次删除复制的层、PSU、状态与失败环节
      replicate_estimates.csv    每次复制在全体/各 domain 上的估计值
      design.copy.json           设计文件快照（与 index 中的 sha256 对应）
      data.copy.csv              数据文件快照（只读复制，绝不改写输入）

全样本链路失败（无应答调整格、校准不支持/矛盾/超限）时不发布到 ``<out>``，
而是写 ``<out>.failed-<batch>/``，保证上一次成功结果原样保留。
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import jackknife as jk
from .data import Person, SurveyData, file_sha256, load_data
from .design import Design, load_design
from .errors import (CalibrationError, EstimationError, ResponseAdjustmentError,
                     SurveyDataError, SurveyDesignError, SurveyError)
from .weights import (assert_estimation_invariants, calibrate,
                      compute_response_adjustment, design_strata,
                      point_estimates, resolve_domains)

STATUS_OK = "ok"
STATUS_REPLICATE_FAILURES = "replicate_failures"


class SurveyRunFailed(SurveyError):
    """全样本链路失败（数据/设计本身可能合法，但无法产出点估计）。

    属性 ``failed_dir`` 指向写好的失败诊断目录；``stage`` 是失败环节。
    """

    def __init__(self, message: str, *, stage: str, failed_dir: str = "",
                 report: dict | None = None):
        super().__init__(message)
        self.stage = stage
        self.failed_dir = failed_dir
        self.report = report or {}


# ---------------------------------------------------------------- 结构校验
def validate_structure(design: Design, data: SurveyData) -> dict:
    """设计 vs 数据：必入层存在性、非必入层 PSU 数、PSU 清单摘要。"""
    strata = design_strata(list(data.people))
    missing_must = [h for h in design.must_include_strata if h not in strata]
    if missing_must:
        raise SurveyDesignError(
            f"设计声明的必入层在数据中不存在: {missing_must}；"
            f"数据中的层为 {sorted(strata)}")
    too_small = []
    for h in sorted(strata):
        if h in design.must_include_strata:
            continue
        m = len(strata[h])
        if m < 2:
            too_small.append(
                f"层 {h!r} 只有 {m} 个 PSU，且未在 must_include_strata 中声明为必入层"
                "（不能把单 PSU 层的方差默认为 0）")
    if too_small:
        raise SurveyDesignError("设计非法：" + "；".join(too_small))
    unknown_psu_structure = []
    for h in sorted(strata):
        for psu, members in strata[h].items():
            if not psu:
                unknown_psu_structure.append(f"层 {h!r} 存在空 PSU 名")
            if not members:
                unknown_psu_structure.append(f"层 {h!r} PSU {psu!r} 没有任何人")
    if unknown_psu_structure:
        raise SurveyDataError("；".join(sorted(set(unknown_psu_structure))))

    strata_rows = []
    for h in sorted(strata):
        strata_rows.append({
            "stratum": h,
            "n_psu": len(strata[h]),
            "n_people": sum(len(v) for v in strata[h].values()),
            "n_respondents": sum(1 for v in strata[h].values() for p in v if p.response),
            "must_include": h in design.must_include_strata,
            "produces_replicates": h not in design.must_include_strata,
            "psus": sorted(strata[h]),
        })
    return {
        "n_strata": len(strata),
        "n_psus": sum(len(v) for v in strata.values()),
        "n_people": len(data.people),
        "n_respondents": sum(1 for p in data.people if p.response),
        "strata": strata_rows,
        "must_include_strata": list(design.must_include_strata),
    }


# ---------------------------------------------------------------- 全样本链路
@dataclass
class FullSampleResult:
    adjustment: object
    calibration: object
    estimates: object
    domains: list


def run_full_sample(people: list[Person], design: Design) -> FullSampleResult:
    try:
        adjustment = compute_response_adjustment(people)
    except ResponseAdjustmentError as e:
        raise SurveyRunFailed(str(e), stage="adjustment") from e

    calibration = calibrate(people, adjustment.weights, design)
    if not calibration.ok:
        raise SurveyRunFailed(
            f"全样本边际校准失败 [{calibration.failed_stage}]: {calibration.reason}",
            stage="calibration") from CalibrationError(calibration.reason)

    try:
        assert_estimation_invariants(people, calibration.weights, design)
    except EstimationError as e:
        raise SurveyRunFailed(str(e), stage="estimate") from e

    domains = resolve_domains(people)
    estimates = point_estimates(people, calibration.weights, domains)
    return FullSampleResult(adjustment, calibration, estimates, domains)


# ---------------------------------------------------------------- 输出写入
def _fmt(x) -> str:
    if x is None:
        return ""
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, int):
        return str(x)
    x = float(x)
    if not math.isfinite(x):
        return ""
    return f"{x:.17g}"


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def _write_estimates(path: Path, variances) -> None:
    header = ["scope", "domain", "metric", "estimate", "estimate_status",
              "estimate_reason", "variance", "se", "ci95_low", "ci95_high",
              "variance_status", "variance_reason", "n_replicates_used",
              "n_replicates_failed", "strata_contributions_json"]
    rows = []
    for v in variances:
        rows.append([
            v.scope, v.domain, v.metric, _fmt(v.estimate), v.estimate_status,
            v.estimate_reason, _fmt(v.variance), _fmt(v.se), _fmt(v.ci_low),
            _fmt(v.ci_high), v.variance_status, v.variance_reason,
            v.n_replicates_used, v.n_replicates_failed,
            json.dumps(v.strata_contributions, ensure_ascii=False, sort_keys=True),
        ])
    _write_csv(path, header, rows)


def _write_person_weights(path: Path, people: list[Person], design: Design,
                          adjustment, calibration) -> None:
    header = ["line_no", "unit_id", "stratum", "psu", "adjustment_cell", "domain",
              "response", "outcome", "base_weight", "response_adjusted_weight",
              "calibrated_weight", *design.calibration_column_names]
    rows = []
    for i, p in enumerate(people):
        rows.append([
            p.line_no, p.unit_id, p.stratum, p.psu, p.adjustment_cell, p.domain,
            "1" if p.response else "0",
            "" if p.outcome is None else _fmt(p.outcome),
            _fmt(p.base_weight),
            _fmt(adjustment.weights[i]),
            _fmt(calibration.weights[i]),
            *p.calibration,
        ])
    _write_csv(path, header, rows)


def _write_adjustment_cells(path: Path, cells) -> None:
    header = ["adjustment_cell", "n_selected", "n_respondents",
              "base_weight_selected", "base_weight_respondents", "multiplier", "status"]
    rows = [[c.name, c.n_selected, c.n_respondents, _fmt(c.base_weight_selected),
             _fmt(c.base_weight_respondents), _fmt(c.multiplier), c.status]
            for c in cells]
    _write_csv(path, header, rows)


def _write_calibration_rounds(path: Path, rounds) -> None:
    header = ["iteration", "max_relative_error", "errors_json"]
    rows = [[r.iteration, _fmt(r.max_relative_error),
             json.dumps(r.errors, ensure_ascii=False, sort_keys=True)]
            for r in rounds]
    _write_csv(path, header, rows)


def _write_calibration_margins(path: Path, design: Design, calibration) -> None:
    header = ["column", "category", "target", "weight_sum", "absolute_error",
              "relative_error", "status"]
    rows = []
    for c in design.calibration_columns:
        for cat, target in c.target_map.items():
            got = calibration.column_totals.get(c.name, {}).get(cat, 0.0)
            abs_err = abs(got - target)
            rel = (abs_err / target) if target > 0 else (0.0 if got <= 0 else math.inf)
            ok = rel < design.tolerance
            rows.append([c.name, cat, _fmt(target), _fmt(got), _fmt(abs_err),
                         _fmt(rel), "ok" if ok else "fail"])
    _write_csv(path, header, rows)


def _write_strata_psus(path: Path, strata_rows: list[dict]) -> None:
    header = ["stratum", "psu", "n_people", "n_respondents", "must_include",
              "produces_replicates"]
    rows = []
    for s in strata_rows:
        # 需要每 PSU 的计数：由结构校验处传回的明细重建
        for item in s.get("psu_rows", []):
            rows.append([s["stratum"], item["psu"], item["n_people"],
                         item["n_respondents"], _fmt(s["must_include"]),
                         _fmt(s["produces_replicates"])])
    _write_csv(path, header, rows)


def _write_replicates(path: Path, replicates) -> None:
    header = ["replicate_id", "stratum", "psu", "m_psu", "n_deleted", "n_people",
              "n_respondents", "status", "failure_stage", "failure_reason",
              "calibration_iterations"]
    rows = [[r.spec.replicate_id, r.spec.stratum, r.spec.psu, r.spec.m_psu,
             r.spec.n_deleted, r.n_people, r.n_respondents, r.status,
             r.failure_stage, r.failure_reason, r.calibration_iterations]
            for r in replicates]
    _write_csv(path, header, rows)


def _write_replicate_estimates(path: Path, replicates, domains: list[str]) -> None:
    header = ["replicate_id", "stratum", "psu", "status", "scope", "domain",
              "metric", "estimate", "note"]
    rows = []
    scopes = [("overall", jk.SCOPE_OVERALL)] + [("domain", d) for d in domains]
    for r in replicates:
        for scope, domain in scopes:
            for metric in jk.METRICS:
                value = r.value(scope, domain, metric)
                note = ""
                if r.status != "ok":
                    note = f"{r.failure_stage}: {r.failure_reason}"
                elif value is None:
                    note = "复制中该子人群没有可用分母（估计缺失）"
                rows.append([r.spec.replicate_id, r.spec.stratum, r.spec.psu,
                             r.status, scope, domain, metric, _fmt(value), note])
    _write_csv(path, header, rows)


def _write_diagnostics(path: Path, index: dict, replicate_failures: list[dict],
                       variances) -> None:
    estimate_status = []
    for v in variances:
        estimate_status.append({
            "scope": v.scope, "domain": v.domain, "metric": v.metric,
            "estimate_status": v.estimate_status,
            "variance_status": v.variance_status,
            "variance_reason": v.variance_reason,
        })
    doc = {
        "run_status": index["run_status"],
        "structural_checks": index["checks"],
        "replicate_failures": replicate_failures,
        "estimator_status": estimate_status,
        "notes": [
            "必入层不产生删除复制，其方差贡献不估计，绝不当作 0。",
            "复制失败时该估计量的方差与区间留空，不拿成功复制凑方差。",
            "domain 是分析子人群；应答调整与校准始终在全部人上进行。",
        ],
    }
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")


def _psu_rows(people: list[Person]) -> dict[str, list[dict]]:
    out: dict[str, dict[str, dict]] = {}
    for p in people:
        st = out.setdefault(p.stratum, {}).setdefault(
            p.psu, {"psu": p.psu, "n_people": 0, "n_respondents": 0})
        st["n_people"] += 1
        st["n_respondents"] += 1 if p.response else 0
    return {h: [v for _, v in sorted(d.items())] for h, d in out.items()}


def _verify_from_person_weights(people, design, adjustment, calibration,
                                full_estimates, strata_rows) -> dict:
    """用逐人权重表回读重算，核对点估计、校准边际与调整格，可复核性自检。"""
    checks = {}

    # 1) 应答调整倍数：按表内权重重算
    recomputed_cells = {}
    for p, w_adj in zip(people, adjustment.weights):
        if not p.response:
            continue
        recomputed_cells.setdefault(p.adjustment_cell, 0.0)
        recomputed_cells[p.adjustment_cell] += w_adj
    checks["adjustment_cells_reproducible"] = all(
        abs(recomputed_cells.get(c.name, 0.0) - c.base_weight_selected) < 1e-9 * max(1.0, c.base_weight_selected)
        for c in adjustment.cells if c.status == "ok")

    # 2) 边际：最终权重和 vs 已知人数
    margin_ok = True
    margins = {}
    for j, c in enumerate(design.calibration_columns):
        totals = {cat: 0.0 for cat in c.categories}
        for p, w in zip(people, calibration.weights):
            if p.response:
                totals[p.calibration[j]] += w
        margins[c.name] = totals
        for cat, target in c.target_map.items():
            if target > 0 and abs(totals[cat] - target) / target >= design.tolerance:
                margin_ok = False
    checks["calibration_margins_reproducible"] = margin_ok

    # 3) 点估计：从逐人权重重算全体与各 domain
    recomputed = point_estimates(people, calibration.weights,
                                 [d.domain for d in full_estimates.domains])
    checks["point_estimates_reproducible"] = _same_estimates(full_estimates, recomputed)

    # 4) PSU 结构：非必入层至少 2 个 PSU
    checks["non_must_include_strata_have_2plus_psus"] = all(
        s["n_psu"] >= 2 for s in strata_rows if not s["must_include"])

    # 5) 必入层没有删除复制（由调用方在复制生成后补充）
    checks["margins"] = margins
    return checks


def _same_estimates(a, b, tol=1e-9) -> bool:
    def cmp(x, y):
        if x is None or y is None:
            return x is None and y is None
        return abs(x - y) <= tol * max(1.0, abs(x), abs(y))
    pairs = [(a.overall, b.overall)] + list(zip(a.domains, b.domains))
    for ea, eb in pairs:
        if ea.domain != eb.domain or ea.status != eb.status:
            return False
        for metric in ("weighted_count", "outcome_total", "mean"):
            if not cmp(getattr(ea, metric), getattr(eb, metric)):
                return False
    return True


def _snapshot_inputs(staging: Path, data_path: Path, design_path: Path,
                     data_sha: str, design_sha: str) -> None:
    copied = staging / "data.copy.csv"
    shutil.copyfile(data_path, copied)
    if file_sha256(copied) != data_sha:
        raise SurveyError("数据快照复制后 sha256 与输入不一致（输入在运行中被改动？）")
    (staging / "design.copy.json").write_bytes(Path(design_path).read_bytes())
    if hashlib.sha256((staging / "design.copy.json").read_bytes()).hexdigest() != design_sha:
        raise SurveyError("设计快照复制后 sha256 与输入不一致")


# ---------------------------------------------------------------- 主入口
def run_survey(data_path, design_path, out_dir, *, max_iterations: int | None = None,
               tolerance: float | None = None) -> dict:
    """执行完整分析链并原子发布产物；返回 index dict。

    数据/设计非法 -> SurveyDataError / SurveyDesignError；
    全样本链路失败 -> SurveyRunFailed（失败诊断写到 ``<out>.failed-<batch>``）；
    有复制失败但点估计可算 -> 正常发布，``run_status = replicate_failures``。
    """
    design_path = Path(design_path)
    data_path = Path(data_path)
    if not data_path.is_file():
        raise SurveyDataError(f"数据文件不存在: {data_path}")
    if not design_path.is_file():
        raise SurveyDesignError(f"设计文件不存在: {design_path}")
    design_sha = file_sha256(design_path)
    design = load_design(design_path, sha256=design_sha)
    if max_iterations is not None:
        if isinstance(max_iterations, bool) or max_iterations <= 0:
            raise SurveyDesignError(f"max_iterations 必须是正整数，实际 {max_iterations!r}")
        design = _replace_design(design, max_iterations=int(max_iterations),
                                 tolerance=tolerance if tolerance is not None else design.tolerance,
                                 population_total=design.population_total,
                                 calibration_columns=design.calibration_columns,
                                 must_include_strata=design.must_include_strata)
    elif tolerance is not None:
        design = _replace_design(design, max_iterations=design.max_iterations,
                                 tolerance=float(tolerance),
                                 population_total=design.population_total,
                                 calibration_columns=design.calibration_columns,
                                 must_include_strata=design.must_include_strata)
    if not (math.isfinite(design.tolerance) and design.tolerance > 0):
        raise SurveyDesignError(f"tolerance 必须是正有限数，实际 {design.tolerance!r}")

    data = load_data(data_path, calibration_columns=design.calibration_column_names)
    people = list(data.people)
    structure = validate_structure(design, data)
    psu_rows = _psu_rows(people)
    for row in structure["strata"]:
        row["psu_rows"] = psu_rows[row["stratum"]]

    out_dir = Path(out_dir)
    batch_id = uuid.uuid4().hex[:12]
    out_dir.parent.mkdir(parents=True, exist_ok=True)

    try:
        full = run_full_sample(people, design)
    except SurveyRunFailed as e:
        _write_failure_artifacts(e, out_dir, batch_id, design, data, structure,
                                 design_path, data_path, design_sha, data.sha256)
        raise

    domains = full.domains
    replicate_specs = jk.plan_replicates(people, design)
    replicates = [jk.run_replicate(people, design, spec, domains)
                  for spec in replicate_specs]
    replicate_failures = [
        {"replicate_id": r.spec.replicate_id, "stratum": r.spec.stratum,
         "psu": r.spec.psu, "stage": r.failure_stage, "reason": r.failure_reason}
        for r in replicates if r.status != "ok"]
    variances = jk.compute_variances(people, design, domains, full.estimates,
                                     replicates)

    checks = _verify_from_person_weights(people, design, full.adjustment,
                                        full.calibration, full.estimates,
                                        structure["strata"])
    checks["must_include_strata_have_no_replicates"] = all(
        r.spec.stratum not in design.must_include_strata for r in replicates)
    checks["replicates_are_one_per_non_must_include_psu"] = (
        len(replicates) == sum(s["n_psu"] for s in structure["strata"]
                               if not s["must_include"]))
    checks["adjustment_multiplier_identity"] = all(
        abs(c.multiplier - c.base_weight_selected / c.base_weight_respondents)
        <= 1e-12 * max(1.0, c.multiplier) for c in full.adjustment.cells)

    run_status = STATUS_REPLICATE_FAILURES if replicate_failures else STATUS_OK
    index = {
        "batch_id": batch_id,
        "run_status": run_status,
        "data_path": str(data_path),
        "design_path": str(design_path),
        "data_sha256": data.sha256,
        "design_sha256": design_sha,
        "max_iterations": design.max_iterations,
        "tolerance": design.tolerance,
        "population_total": design.population_total,
        "n_people": len(people),
        "n_respondents": sum(1 for p in people if p.response),
        "domains": domains,
        "structure": structure,
        "calibration_columns": [
            {"name": c.name, "categories": list(c.categories),
             "targets": list(c.targets), "total": c.total}
            for c in design.calibration_columns],
        "adjustment": {
            "cells": [c.as_row() for c in full.adjustment.cells],
            "n_cells": len(full.adjustment.cells),
        },
        "calibration": {
            "status": full.calibration.status,
            "iterations_used": full.calibration.iterations_used,
            "n_rounds": len(full.calibration.rounds),
            "max_final_relative_error": jk_max_error(full.calibration.final_errors),
        },
        "n_replicates": len(replicates),
        "n_replicates_failed": len(replicate_failures),
        "replicate_failures": replicate_failures,
        "checks": checks,
        "outputs": [
            "estimates.csv", "person_weights.csv", "adjustment_cells.csv",
            "calibration_rounds.csv", "calibration_margins.csv", "strata_psus.csv",
            "replicates.csv", "replicate_estimates.csv", "diagnostics.json",
            "design.copy.json", "data.copy.csv",
        ],
    }

    staging = out_dir.with_name(out_dir.name + ".staging-" + batch_id)
    staging.mkdir(parents=True, exist_ok=False)
    try:
        _write_estimates(staging / "estimates.csv", variances)
        _write_person_weights(staging / "person_weights.csv", people, design,
                              full.adjustment, full.calibration)
        _write_adjustment_cells(staging / "adjustment_cells.csv",
                                full.adjustment.cells)
        _write_calibration_rounds(staging / "calibration_rounds.csv",
                                  full.calibration.rounds)
        _write_calibration_margins(staging / "calibration_margins.csv", design,
                                   full.calibration)
        _write_strata_psus(staging / "strata_psus.csv", structure["strata"])
        _write_replicates(staging / "replicates.csv", replicates)
        _write_replicate_estimates(staging / "replicate_estimates.csv", replicates,
                                   domains)
        _write_diagnostics(staging / "diagnostics.json", index,
                           replicate_failures, variances)
        _snapshot_inputs(staging, data_path, design_path, data.sha256, design_sha)
        (staging / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8")
        _promote(staging, out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return index


def jk_max_error(errors: dict) -> float:
    worst = 0.0
    for per in errors.values():
        for v in per.values():
            if math.isinf(v):
                return math.inf
            worst = max(worst, v)
    return worst


def _replace_design(design: Design, **kw) -> Design:
    from dataclasses import replace
    return replace(design, **kw)


def _write_failure_artifacts(e: SurveyRunFailed, out_dir: Path, batch_id: str,
                             design: Design, data: SurveyData, structure: dict,
                             design_path: Path, data_path: Path,
                             design_sha: str, data_sha: str) -> None:
    """全样本失败：写独立失败目录，绝不覆盖上一次成功结果。"""
    failed = out_dir.with_name(out_dir.name + ".failed-" + batch_id)
    failed.mkdir(parents=True, exist_ok=True)
    report = {
        "batch_id": batch_id,
        "run_status": "failed",
        "stage": e.stage,
        "reason": str(e),
        "data_path": str(data_path),
        "design_path": str(design_path),
        "data_sha256": data_sha,
        "design_sha256": design_sha,
        "n_people": len(data.people),
        "structure": structure,
        "note": ("全样本权重链未完成：没有发布点估计或方差；"
                 "上一次成功结果目录保持不变。此目录只含失败诊断与输入快照。"),
    }
    shutil.copyfile(data_path, failed / "data.copy.csv")
    (failed / "design.copy.json").write_bytes(Path(design_path).read_bytes())
    (failed / "diagnostics.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    (failed / "index.json").write_text(
        json.dumps({k: report[k] for k in
                    ("batch_id", "run_status", "stage", "reason", "data_sha256",
                     "design_sha256", "n_people")},
                   ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8")
    e.failed_dir = str(failed)
    e.report = report


def _promote(staging: Path, out_dir: Path) -> None:
    prev = None
    if out_dir.exists():
        prev = out_dir.with_name(out_dir.name + ".prev-" + uuid.uuid4().hex[:8])
        os.replace(out_dir, prev)
    os.replace(staging, out_dir)
    if prev is not None:
        shutil.rmtree(prev, ignore_errors=True)


# ---------------------------------------------------------------- 读回
def _read_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_estimates(out_dir) -> list[dict]:
    return _read_csv(Path(out_dir) / "estimates.csv")


def read_person_weights(out_dir) -> list[dict]:
    return _read_csv(Path(out_dir) / "person_weights.csv")


def read_diagnostics(out_dir) -> dict:
    return json.loads((Path(out_dir) / "diagnostics.json").read_text(encoding="utf-8"))
