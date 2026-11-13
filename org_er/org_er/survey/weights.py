"""权重链：应答调整 -> IPF 边际校准 -> 点估计。

应答调整
--------
对每个 ``adjustment_cell``：先求**全部被抽中者**（含未应答者）的基础权重之和，
再除以该格**应答者**的基础权重之和，得到该格应答者的调整倍数；应答者权重
= 基础权重 × 倍数。某格没有应答者 -> 停止并解释（禁止偷偷合并格子）。

边际校准（IPF）
---------------
在全部应答者上，按设计 JSON 配置的列顺序轮流缩放，使每个类别边际贴近已知人数；
每轮记录最大相对误差，直到所有边际相对误差 < 容差。权重保持为正。以下情况
**不得标成校准成功**：

- 某个已知正控制格没有任何应答样本支持（列边际无法被样本触及）；
- 分母为 0（某轮某列某类别权重和为 0）；
- 不同边际互相矛盾（IPF 反复振荡，相对误差不收敛）；
- 超过迭代上限。

点估计
------
domain 是分析子人群，**不是另一次抽样**：权重始终在全部人上做应答调整与校准，
再按 domain 取子集汇总。均值的分母是该人群最终权重之和；没有应答者的子人群
给缺失估计与原因。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass

from .data import Person
from .design import Design
from .errors import EstimationError, ResponseAdjustmentError


# ---------------------------------------------------------------- 应答调整
@dataclass
class AdjustmentCell:
    name: str
    n_selected: int
    n_respondents: int
    base_weight_selected: float
    base_weight_respondents: float
    multiplier: float | None       # None 表示该格无应答者
    status: str                    # ok / no_respondents

    def as_row(self) -> dict:
        return {
            "adjustment_cell": self.name,
            "n_selected": self.n_selected,
            "n_respondents": self.n_respondents,
            "base_weight_selected": self.base_weight_selected,
            "base_weight_respondents": self.base_weight_respondents,
            "multiplier": self.multiplier,
            "status": self.status,
        }


@dataclass
class ResponseAdjustment:
    cells: tuple[AdjustmentCell, ...]
    weights: tuple[float | None, ...]      # 与 people 对齐；未应答者为 None

    @property
    def ok(self) -> bool:
        return all(c.status == "ok" for c in self.cells)


def compute_response_adjustment(people: list[Person], *,
                                base_weights=None) -> ResponseAdjustment:
    """按 adjustment_cell 计算应答调整倍数与应答者权重。

    ``base_weights`` 允许替换基础权重（jackknife 复制用）；缺省用每人的 base_weight。
    复制中某格消失或没有应答者同样报错，由调用方标明层/PSU 与环节。
    汇总用 ``math.fsum``，与输入行序无关。
    """
    base = [p.base_weight for p in people] if base_weights is None else list(base_weights)
    if len(base) != len(people):
        raise ValueError("base_weights 长度与人数不一致")

    selected_sums: dict[str, list[float]] = {}
    respondent_sums: dict[str, list[float]] = {}
    counts: dict[str, list[int]] = {}
    for p, w in zip(people, base):
        selected_sums.setdefault(p.adjustment_cell, []).append(w)
        counts.setdefault(p.adjustment_cell, [0, 0])[0] += 1
        if p.response:
            respondent_sums.setdefault(p.adjustment_cell, []).append(w)
            counts[p.adjustment_cell][1] += 1

    cells: list[AdjustmentCell] = []
    missing: list[str] = []
    for name in sorted(selected_sums):
        selected_sum = math.fsum(selected_sums[name])
        resp_sum = math.fsum(respondent_sums.get(name, ()))
        n_sel, n_resp = counts[name]
        if n_resp == 0:
            cells.append(AdjustmentCell(
                name=name, n_selected=n_sel, n_respondents=0,
                base_weight_selected=selected_sum,
                base_weight_respondents=0.0, multiplier=None,
                status="no_respondents"))
            missing.append(name)
            continue
        if not (resp_sum > 0.0 and math.isfinite(resp_sum)):
            raise ResponseAdjustmentError(
                f"adjustment_cell {name!r} 的应答者基础权重和非法: {resp_sum!r}")
        multiplier = selected_sum / resp_sum
        if not (math.isfinite(multiplier) and multiplier > 0.0):
            raise ResponseAdjustmentError(
                f"adjustment_cell {name!r} 的应答调整倍数非法: {multiplier!r}"
                f"（被抽中者和={selected_sum!r}，应答者和={resp_sum!r}）")
        cells.append(AdjustmentCell(
            name=name, n_selected=n_sel, n_respondents=n_resp,
            base_weight_selected=selected_sum,
            base_weight_respondents=resp_sum,
            multiplier=multiplier, status="ok"))

    if missing:
        raise ResponseAdjustmentError(
            f"应答调整失败：adjustment_cell {', '.join(map(repr, missing))} 没有应答者；"
            "禁止偷偷合并调整格或跳过该格")

    multiplier_of = {c.name: c.multiplier for c in cells}
    weights: list[float | None] = []
    for p, w in zip(people, base):
        weights.append(w * multiplier_of[p.adjustment_cell] if p.response else None)
    return ResponseAdjustment(cells=tuple(cells), weights=tuple(weights))


# ---------------------------------------------------------------- IPF 校准
@dataclass
class CalibrationRound:
    iteration: int
    max_relative_error: float
    errors: dict[str, dict[str, float]]        # 列 -> 类别 -> 相对误差

    def as_row(self) -> dict:
        return {
            "iteration": self.iteration,
            "max_relative_error": self.max_relative_error,
            "errors_json": json.dumps(self.errors, ensure_ascii=False,
                                      sort_keys=True, allow_nan=False),
        }


@dataclass
class CalibrationResult:
    status: str                    # ok / failed
    weights: tuple[float | None, ...]
    rounds: tuple[CalibrationRound, ...]
    final_errors: dict[str, dict[str, float]]
    column_totals: dict[str, dict[str, float]]
    reason: str = ""
    failed_stage: str = ""
    iterations_used: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _column_totals(people: list[Person], weights, design: Design,
                   column_index: int) -> dict[str, float]:
    """列内各分类的权重和；用 math.fsum，结果与输入行序无关。"""
    col = design.calibration_columns[column_index]
    pieces: dict[str, list[float]] = {cat: [] for cat in col.categories}
    for p, w in zip(people, weights):
        if not p.response:
            continue
        pieces[p.calibration[column_index]].append(w)
    return {cat: math.fsum(pieces[cat]) for cat in col.categories}


def _all_totals(people, weights, design) -> dict[str, dict[str, float]]:
    return {c.name: _column_totals(people, weights, design, j)
            for j, c in enumerate(design.calibration_columns)}


def _relative_errors(totals: dict[str, dict[str, float]],
                     design: Design) -> dict[str, dict[str, float]]:
    errs: dict[str, dict[str, float]] = {}
    for c in design.calibration_columns:
        per: dict[str, float] = {}
        for cat, target in c.target_map.items():
            got = totals[c.name].get(cat, 0.0)
            if target > 0.0:
                per[cat] = math.inf if got <= 0.0 else abs(got - target) / target
            else:
                per[cat] = 0.0 if got <= 0.0 else math.inf
        errs[c.name] = per
    return errs


def _max_error(errors: dict[str, dict[str, float]]) -> float:
    worst = 0.0
    for per in errors.values():
        for v in per.values():
            if math.isinf(v):
                return math.inf
            worst = max(worst, v)
    return worst


def _failed(status_weights, rounds, errors, totals, stage, reason,
            iterations=0) -> CalibrationResult:
    return CalibrationResult(
        status="failed", weights=tuple(status_weights), rounds=tuple(rounds),
        final_errors=errors, column_totals=totals, reason=reason,
        failed_stage=stage, iterations_used=iterations)


def calibrate(people: list[Person], weights, design: Design, *,
              max_iterations: int | None = None,
              tolerance: float | None = None) -> CalibrationResult:
    """在全部应答者上按配置列顺序做 IPF 校准；失败返回 status='failed' 与原因。

    不抛异常（复制诊断需要失败细节），调用方按 ``ok`` 决定点估计/方差是否为缺失。
    """
    max_iter = design.max_iterations if max_iterations is None else max_iterations
    tol = design.tolerance if tolerance is None else tolerance
    columns = design.calibration_columns
    work = list(weights)

    # 1) 设计未声明却出现在样本里的类别：没有已知人数，不能校准
    declared: dict[str, set[str]] = {c.name: set(c.categories) for c in columns}
    unknown: list[str] = []
    for p in people:
        if not p.response:
            continue
        for j, c in enumerate(columns):
            if p.calibration[j] not in declared[c.name]:
                unknown.append(f"列 {c.name!r} 的类别 {p.calibration[j]!r}"
                               f"（样本行 {p.line_no}，unit_id={p.unit_id!r}）")
    if unknown:
        return _failed(work, (), {}, {}, "undeclared_category",
                       "样本中出现设计未声明的校准类别，无法给出已知人数："
                       + "；".join(sorted(unknown)[:20])
                       + (f" 等 {len(unknown)} 处" if len(unknown) > 20 else ""))

    totals0 = _all_totals(people, work, design)
    errors0 = _relative_errors(totals0, design)

    # 2) 样本支持检查：正控制格必须有正权重支持；目标为 0 的格不能有样本
    problems: list[str] = []
    for c in columns:
        for cat, target in c.target_map.items():
            got = totals0[c.name][cat]
            if target > 0.0 and got <= 0.0:
                problems.append(
                    f"列 {c.name!r} 类别 {cat!r} 已知人数 {target:g}，但应答样本权重和为 0")
            elif target <= 0.0 and got > 0.0:
                problems.append(
                    f"列 {c.name!r} 类别 {cat!r} 已知人数为 0，但应答样本权重和 {got:g}>0"
                    "（校准将产生 0 权重，与保留正权重矛盾）")
    if problems:
        return _failed(work, (), errors0, totals0, "unsupported_control",
                       "没有样本支持的正控制格 / 目标为 0 却有样本的格："
                       + "；".join(problems))

    # 3) IPF：按配置列顺序轮流缩放，每完成一整轮检查收敛
    rounds: list[CalibrationRound] = []
    errors = errors0
    totals = totals0
    for iteration in range(1, max_iter + 1):
        for j, c in enumerate(columns):
            totals_j = _column_totals(people, work, design, j)
            target_map = c.target_map
            zeros = [cat for cat, target in target_map.items()
                     if target > 0.0 and totals_j[cat] <= 0.0]
            if zeros:
                return _failed(
                    work, rounds, errors, _all_totals(people, work, design),
                    "zero_denominator",
                    f"第 {iteration} 轮列 {c.name!r} 出现零分母类别 {zeros}；"
                    "边际互相矛盾或前序缩放把该格清零", iteration)
            for i, p in enumerate(people):
                if not p.response:
                    continue
                factor = target_map[p.calibration[j]] / totals_j[p.calibration[j]]
                new_w = work[i] * factor
                if not (math.isfinite(new_w) and new_w > 0.0):
                    return _failed(
                        work, rounds, errors, _all_totals(people, work, design),
                        "numeric",
                        f"第 {iteration} 轮列 {c.name!r} 校准时权重非正/非有限: {new_w!r}",
                        iteration)
                work[i] = new_w
        totals = _all_totals(people, work, design)
        errors = _relative_errors(totals, design)
        worst = _max_error(errors)
        rounds.append(CalibrationRound(iteration, worst, errors))
        if worst < tol:
            return CalibrationResult(
                status="ok", weights=tuple(work), rounds=tuple(rounds),
                final_errors=errors, column_totals=totals,
                iterations_used=iteration)

    return _failed(
        work, rounds, errors, totals, "max_iterations",
        f"超过迭代上限 {max_iter} 轮仍未收敛（最大相对误差 "
        f"{_max_error(errors):.3e} >= 容差 {tol:.3e}）；"
        "不同边际可能互相矛盾", max_iter)


# ---------------------------------------------------------------- 点估计
@dataclass
class DomainEstimate:
    domain: str
    status: str                    # ok / missing
    reason: str
    n_selected: int
    n_respondents: int
    weighted_count: float | None
    outcome_total: float | None
    mean: float | None

    def as_dict(self) -> dict:
        return {
            "domain": self.domain, "status": self.status, "reason": self.reason,
            "n_selected": self.n_selected, "n_respondents": self.n_respondents,
            "weighted_count": self.weighted_count,
            "outcome_total": self.outcome_total, "mean": self.mean,
        }


@dataclass
class PointEstimates:
    overall: DomainEstimate
    domains: tuple[DomainEstimate, ...]
    weighted_total: float

    def ordered(self) -> list[DomainEstimate]:
        return [self.overall, *self.domains]

    def by_domain(self) -> dict[str, DomainEstimate]:
        out = {d.domain: d for d in self.domains}
        out[self.overall.domain] = self.overall
        return out


def point_estimates(people: list[Person], weights,
                    domains: list[str]) -> PointEstimates:
    """用最终权重（或复制权重）汇总全体与每个 domain。

    均值的分母是该人群的最终权重和；``weights`` 里的 None 表示未应答者。
    domain 只是子集抽取，权重本身已在全部人上完成应答调整与校准。
    """
    def estimate(domain: str, members: list[tuple[Person, float]]) -> DomainEstimate:
        n_sel = len(members)
        respondents = [(p, w) for p, w in members if p.response and w is not None]
        n_resp = len(respondents)
        if n_resp == 0:
            return DomainEstimate(
                domain=domain, status="missing",
                reason="该子人群没有应答者（未应答者不提供 outcome，不能估计）",
                n_selected=n_sel, n_respondents=0,
                weighted_count=None, outcome_total=None, mean=None)
        denom = math.fsum(w for _, w in respondents)
        if not (denom > 0.0 and math.isfinite(denom)):
            return DomainEstimate(
                domain=domain, status="missing",
                reason=f"该子人群应答者最终权重和为 0 或非有限（{denom!r}），均值无定义",
                n_selected=n_sel, n_respondents=n_resp,
                weighted_count=None, outcome_total=None, mean=None)
        outcome_total = math.fsum(w * p.outcome for p, w in respondents)
        return DomainEstimate(
            domain=domain, status="ok", reason="",
            n_selected=n_sel, n_respondents=n_resp,
            weighted_count=denom, outcome_total=outcome_total,
            mean=outcome_total / denom)

    paired = list(zip(people, weights))
    overall = estimate("__overall__", paired)
    by_domain: dict[str, list[tuple[Person, float]]] = {d: [] for d in domains}
    for p, w in paired:
        by_domain[p.domain].append((p, w))
    per_domain = tuple(estimate(d, by_domain[d]) for d in domains)
    weighted_total = math.fsum(w for p, w in paired if p.response and w is not None)
    return PointEstimates(overall=overall, domains=per_domain,
                          weighted_total=weighted_total)


# ---------------------------------------------------------------- 结构与不变量
def design_strata(people: list[Person]) -> dict[str, dict[str, list[Person]]]:
    """层 -> PSU -> 该 PSU 的人员列表（保留人员文件顺序）。"""
    out: dict[str, dict[str, list[Person]]] = {}
    for p in people:
        out.setdefault(p.stratum, {}).setdefault(p.psu, []).append(p)
    return out


def resolve_domains(people: list[Person]) -> list[str]:
    return sorted({p.domain for p in people})


def assert_estimation_invariants(people: list[Person], weights,
                                 design: Design) -> None:
    """点估计后自检：响应者权重为正有限，校准边际在容差内。"""
    for p, w in zip(people, weights):
        if p.response and (w is None or not (w > 0.0 and math.isfinite(w))):
            raise EstimationError(
                f"响应者 {p.unit_id!r}（第 {p.line_no} 行）最终权重非正/非有限: {w!r}")
    for j, c in enumerate(design.calibration_columns):
        totals = {cat: 0.0 for cat in c.categories}
        for p, w in zip(people, weights):
            if p.response:
                totals[p.calibration[j]] += w
        for cat, target in c.target_map.items():
            got = totals.get(cat, 0.0)
            if target > 0.0 and abs(got - target) / target >= max(design.tolerance * 10,
                                                                 1e-10):
                raise EstimationError(
                    f"最终权重未满足校准边际 {c.name!r}/{cat!r}: "
                    f"权重和 {got:g} vs 已知 {target:g}")
