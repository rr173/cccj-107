"""多条件 FBA+FVA 作业编排、原子发布与读回。

每个条件独立从原网络快照派生上下界，条件之间互不继承：

  1. FBA 求最大生物量 z（含状态：最优/不可行/无界/数值失败）；
  2. 有限 z（含 z=0 且可行）时加 v_biomass >= retain_fraction*z，
     对每条反应分别独立求 min/max，端点状态逐个记录；
  3. 每个有限解保存完整见证通量，并独立代回全部约束记录最大残差；
  4. 全部产物先写暂存目录，成功后整体换入 <out>，失败不破坏上一次结果。
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path

from .errors import FluxInputError, FluxSolverError
from .model import (Condition, ConditionBundle, EffectiveBounds, Network,
                    apply_condition, load_conditions, load_network)
from .solver import (LPResult, classify_flux, diagnose_witness, fba,
                     fva_endpoint)


def _file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_default(obj):
    if isinstance(obj, frozenset):
        return sorted(obj)
    raise TypeError(f"不可序列化的对象: {type(obj)!r}")


@dataclass
class _ConditionOutcome:
    condition_id: str
    order: int
    fba_status: str
    z: float | None
    fva_performed: bool
    warning: str | None
    ranges: list[dict]
    witness_count: int
    invalid_witness_count: int
    unavailable_endpoint_count: int


def _write_json(path: Path, payload) -> None:
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=_json_default)
        fh.write("\n")


def _witness_record(network: Network, bounds: EffectiveBounds, result: LPResult,
                    tolerance: float, retain_fraction, z, stage: str,
                    reaction_id: str | None, direction: str | None) -> dict:
    diag = diagnose_witness(
        network, bounds.lower, bounds.upper, result.flux,
        retain_fraction, z, tolerance)
    target_value = None
    if reaction_id is not None:
        target_value = result.flux[network.reaction_index()[reaction_id]]
    rec = {
        "stage": stage,                       # fba / fva
        "condition_id": None,                 # 由调用方补
        "reaction_id": reaction_id,
        "direction": direction,
        "solver_status": result.status,
        # 原始 LP 目标值（FBA 是 -v_biomass，因为 linprog 只做最小化）
        "lp_objective_value": result.objective,
        # FVA 时被 extremize 的反应的真实有符号通量；FBA 时为 None
        "target_reaction_value": target_value,
        "nit": result.nit,
        "message": result.message,
        "diagnostics": {
            "max_balance_residual": diag.max_balance_residual,
            "max_bound_violation": diag.max_bound_violation,
            "biomass_retention_gap": diag.biomass_retention_gap,
            "valid": diag.valid,
            "note": diag.note,
        },
        "flux": {rid: val for rid, val in zip(network.reaction_ids, result.flux)},
    }
    return rec


def _run_condition(network: Network, condition: Condition, retain_fraction: float,
                   tolerance: float, cond_dir: Path) -> _ConditionOutcome:
    bounds = apply_condition(network, condition)
    wit_dir = cond_dir / "witnesses"
    wit_dir.mkdir(parents=True, exist_ok=True)

    fba_result = fba(network, bounds.lower, bounds.upper)
    fba_witness: dict | None = None
    invalid = 0
    unavailable_eps = 0

    if fba_result.status == "numerical_failure":
        # 硬失败：本批次不发布为正式结果（诊断进 .failed），命令非零退出。
        raise FluxSolverError(
            f"条件 {condition.id!r} 的 FBA 求解器数值失败: {fba_result.message}")

    if fba_result.status == "optimal":
        fba_witness = _witness_record(
            network, bounds, fba_result, tolerance, None, None, "fba", None, None)
        fba_witness["condition_id"] = condition.id
        if not fba_witness["diagnostics"]["valid"]:
            invalid += 1
            raise FluxSolverError(
                f"条件 {condition.id!r} 的 FBA 见证残差超出容差 {tolerance:g}: "
                f"{fba_witness['diagnostics']}")
        _write_json(wit_dir / "fba.json", fba_witness)

    # FBA 用 linprog 最小化 -v_biomass，真实最大生物量是目标值的相反数。
    z = (-fba_result.objective
         if fba_result.status == "optimal" and fba_result.objective is not None
         else None)
    warning = None
    ranges: list[dict] = []
    fva_performed = False
    witness_records: list[dict] = []

    if fba_result.status == "infeasible":
        warning = "条件不可行：守恒与上下界下不存在稳态通量，不做 FVA"
    elif fba_result.status == "unbounded":
        warning = "生物量目标无界：不做依赖有限 z 的 FVA，也未给无界反应私加上限"
    elif fba_result.status == "optimal":
        fva_performed = True
        threshold = retain_fraction * (z or 0.0)
        for j, rxn in enumerate(network.reactions):
            row = {
                "condition_id": condition.id,
                "reaction_id": rxn.id,
                "is_biomass": rxn.id == network.biomass_reaction,
                "retain_fraction": retain_fraction,
                "biomass_threshold": threshold,
            }
            for direction in ("min", "max"):
                ep = fva_endpoint(network, bounds.lower, bounds.upper,
                                  j, direction, retain_fraction, z or 0.0)
                row[f"{direction}_status"] = ep.status
                row[f"{direction}_message"] = ep.message
                if ep.status == "optimal":
                    value = ep.flux[j]
                    row[f"{direction}_value"] = value
                    rec = _witness_record(network, bounds, ep, tolerance,
                                          retain_fraction, z, "fva",
                                          rxn.id, direction)
                    rec["condition_id"] = condition.id
                    if not rec["diagnostics"]["valid"]:
                        invalid += 1
                        # 残差超容差的有限解不能标有效：该端点改记不可用。
                        row[f"{direction}_status"] = "unavailable"
                        row[f"{direction}_value"] = None
                        row[f"{direction}_message"] = (
                            ep.message + "；见证残差超出声明容差，标记 unavailable")
                        unavailable_eps += 1
                    else:
                        witness_records.append(rec)
                else:
                    row[f"{direction}_value"] = None
                    if ep.status == "numerical_failure":
                        row[f"{direction}_status"] = "unavailable"
                        unavailable_eps += 1
                    # unbounded/infeasible 端点保留原状态词，无见证向量
            row["category"] = classify_flux(
                row["min_value"], row["max_value"],
                row["min_status"], row["max_status"], tolerance)
            ranges.append(row)

        with (wit_dir / "fva.jsonl").open("w", encoding="utf-8") as fh:
            for rec in witness_records:
                fh.write(json.dumps(rec, ensure_ascii=False, default=_json_default) + "\n")

    _write_json(cond_dir / "condition.json", {
        "condition_id": condition.id,
        "order": condition.order,
        "applied_changes": bounds.changes,
        "fba": {
            "status": fba_result.status,
            "message": fba_result.message,
            "z": z,
            "witness": "witnesses/fba.json" if fba_witness else None,
        },
        "fva_performed": fva_performed,
        "warning": warning,
        "retain_fraction": retain_fraction,
        "tolerance": tolerance,
        "unavailable_endpoint_count": unavailable_eps,
        "invalid_witness_count": invalid,
    })

    return _ConditionOutcome(
        condition_id=condition.id,
        order=condition.order,
        fba_status=fba_result.status,
        z=z,
        fva_performed=fva_performed,
        warning=warning,
        ranges=ranges,
        witness_count=(1 if fba_witness else 0) + len(witness_records),
        invalid_witness_count=invalid,
        unavailable_endpoint_count=unavailable_eps,
    )


def run_flux_analysis(network_path: str, conditions_path: str, out_dir: str,
                      retain_fraction: float, tolerance: float = 1e-7) -> dict:
    """完整作业入口。返回 index 摘要字典；产物原子发布到 out_dir。"""
    if not (isinstance(retain_fraction, (int, float)) and not isinstance(retain_fraction, bool)
            and math.isfinite(retain_fraction)
            and 0.0 < retain_fraction <= 1.0):
        raise FluxInputError(
            f"retain_fraction 必须在 (0, 1] 内且有限，得到 {retain_fraction!r}")
    if not (isinstance(tolerance, (int, float)) and not isinstance(tolerance, bool)
            and math.isfinite(tolerance) and tolerance > 0.0):
        raise FluxInputError(f"判零容差必须是正有限数，得到 {tolerance!r}")

    network = load_network(network_path)
    bundle = load_conditions(conditions_path, network)

    out = Path(out_dir)
    stage = out.parent / f".{out.name}.stage"
    failed = out.parent / f".{out.name}.failed"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True, exist_ok=True)

    outcomes: list[_ConditionOutcome] = []
    hard_error: str | None = None
    try:
        for condition in bundle.conditions:
            cond_dir = stage / "conditions" / condition.id
            cond_dir.mkdir(parents=True, exist_ok=True)
            try:
                outcomes.append(_run_condition(
                    network, condition, float(retain_fraction),
                    float(tolerance), cond_dir))
            except FluxSolverError as exc:
                # 硬失败：停止后续条件，诊断写进暂存；本批次不换入正式目录，
                # 上一次成功产物保持原样。
                hard_error = str(exc)
                _write_json(cond_dir / "condition.json", {
                    "condition_id": condition.id,
                    "order": condition.order,
                    "fba": {"status": "numerical_failure", "message": str(exc),
                            "z": None, "witness": None},
                    "fva_performed": False,
                    "warning": str(exc),
                    "retain_fraction": retain_fraction,
                    "tolerance": tolerance,
                })
                break

        if hard_error is not None:
            # 已完成条件的产物与失败诊断一起保留在 .failed，供排查，但不发布。
            _write_json(stage / "diagnostics.json", {
                "run_status": "hard_failure",
                "hard_error": hard_error,
                "completed_before_failure": [o.condition_id for o in outcomes],
                "note": "硬失败批次不发布为正式结果；逐条件产物见 conditions/。",
            })
            if failed.exists():
                shutil.rmtree(failed)
            shutil.move(str(stage), str(failed))
        else:
            index = _publish(network, bundle, outcomes, stage, out,
                             retain_fraction, tolerance)
    except BaseException:
        # 任何意外都不覆盖上一次成功产物
        if stage.exists():
            if failed.exists():
                shutil.rmtree(failed)
            shutil.move(str(stage), str(failed))
        raise

    if hard_error:
        raise FluxSolverError(hard_error)
    return index


def _publish(network: Network, bundle: ConditionBundle, outcomes,
             stage: Path, out: Path, retain_fraction, tolerance) -> dict:
    by_id = {o.condition_id: o for o in outcomes}
    summary_rows = []
    range_rows: list[dict] = []
    warnings: list[str] = []
    for condition in bundle.conditions:
        oc = by_id[condition.id]   # 成功路径下每个条件都有结果
        summary_rows.append({
            "condition_id": oc.condition_id, "order": oc.order,
            "fba_status": oc.fba_status,
            "z": "" if oc.z is None else (0.0 if abs(oc.z) <= tolerance else oc.z),
            "fva_performed": oc.fva_performed,
            "warning": oc.warning or "",
            "witness_count": oc.witness_count,
            "invalid_witness_count": oc.invalid_witness_count,
            "unavailable_endpoint_count": oc.unavailable_endpoint_count,
        })
        range_rows.extend(oc.ranges)
        if oc.warning:
            warnings.append(f"{oc.condition_id}: {oc.warning}")
        if oc.unavailable_endpoint_count:
            warnings.append(
                f"{oc.condition_id}: {oc.unavailable_endpoint_count} 个 FVA 端点不可用"
                "（数值失败或见证超容差），未计入 blocked")

    _write_csv(stage / "condition_summary.csv", summary_rows, [
        "condition_id", "order", "fba_status", "z", "fva_performed",
        "witness_count", "invalid_witness_count",
        "unavailable_endpoint_count", "warning"], tolerance)
    _write_csv(stage / "fva_ranges.csv", range_rows, [
        "condition_id", "reaction_id", "is_biomass", "retain_fraction",
        "biomass_threshold", "min_status", "min_value", "max_status",
        "max_value", "category", "min_message", "max_message"], tolerance)

    index = {
        "schema_version": 1,
        "inputs": {
            "network_path": network.source_path,
            "network_sha256": _file_sha256(network.source_path),
            "conditions_path": bundle.source_path,
            "conditions_sha256": _file_sha256(bundle.source_path),
        },
        "parameters": {
            "retain_fraction": retain_fraction,
            "tolerance": tolerance,
        },
        "network_summary": {
            "metabolites": list(network.metabolites),
            "boundary_metabolites": sorted(network.boundary),
            "internal_metabolites": list(network.internal_metabolites),
            "reactions": [
                {"id": r.id, "lower": r.lower, "upper": r.upper,
                 "stoichiometry": r.stoichiometry}
                for r in network.reactions],
            "biomass_reaction": network.biomass_reaction,
        },
        "conditions": [
            {"id": c.id, "order": c.order,
             "replacements": {k: list(v) for k, v in c.replacements.items()},
             "knockouts": sorted(c.knockouts)}
            for c in bundle.conditions],
        "outcomes": [
            {"condition_id": o.condition_id, "order": o.order,
             "fba_status": o.fba_status, "z": o.z,
             "fva_performed": o.fva_performed, "warning": o.warning,
             "witness_count": o.witness_count,
             "invalid_witness_count": o.invalid_witness_count,
             "unavailable_endpoint_count": o.unavailable_endpoint_count}
            for o in outcomes],
        "warnings": warnings,
        "run_status": "completed_with_warnings" if warnings else "completed",
    }
    _write_json(stage / "index.json", index)
    _write_json(stage / "diagnostics.json", {
        "run_status": index["run_status"],
        "warnings": warnings,
        "hard_error": None,
        "note": "逐见证的守恒/上下界/保留约束残差见 conditions/<id>/witnesses/",
    })

    # 原子换入：旧结果整体退为 .prev
    if out.exists():
        prev = out.parent / f".{out.name}.prev"
        if prev.exists():
            shutil.rmtree(prev)
        shutil.move(str(out), str(prev))
    shutil.move(str(stage), str(out))
    return index


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str],
               tolerance: float = 1e-7) -> None:
    def clean(value):
        # 仅展示层清理：容差内的浮点数记为 0.0，避免 -0.0 噪声；
        # JSON 见证里的原始数值精度不动，残差诊断仍用未清理值。
        if isinstance(value, float) and abs(value) <= tolerance:
            return 0.0
        return value

    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: clean(row[k]) for k in fieldnames})


# ---------------------------------------------------------------- 读回

def read_index(out_dir: str) -> dict:
    with (Path(out_dir) / "index.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)


def read_condition_summary(out_dir: str) -> list[dict]:
    with (Path(out_dir) / "condition_summary.csv").open("r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_fva_ranges(out_dir: str) -> list[dict]:
    with (Path(out_dir) / "fva_ranges.csv").open("r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def iter_witnesses(out_dir: str):
    """遍历全部见证记录（FBA 单条 JSON + 每条件 FVA JSONL）。"""
    base = Path(out_dir) / "conditions"
    for cond_dir in sorted(base.iterdir()):
        fba = cond_dir / "witnesses" / "fba.json"
        if fba.exists():
            with fba.open("r", encoding="utf-8") as fh:
                yield json.load(fh)
        fva = cond_dir / "witnesses" / "fva.jsonl"
        if fva.exists():
            with fva.open("r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        yield json.loads(line)
