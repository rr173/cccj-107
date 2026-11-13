"""分层删一 PSU 的 jackknife：每次复制都从复制后的基础权重重新做应答调整与校准。

复制构造（对一个**非必入层** h，m 个 PSU，逐个 PSU r）：

1. 删去 PSU r 中的所有人；
2. 同层其他 PSU 的人的基础权重乘 ``m/(m-1)``；
3. 其他层不变；
4. **重新**做应答调整（倍数按复制后的基础权重重新算）与 IPF 边际校准。

必入层（设计 JSON 的 ``must_include_strata``）不产生删除复制；其方差贡献按设计
为“不估计”，绝不当成 0。

方差（每个估计量各自计算）::

    Var(θ) = Σ_h (m_h - 1) / m_h × Σ_{r ∈ h} (θ_r − θ_full)²

只对非必入层求和；区间用 ``θ ± 1.96 × sqrt(Var)``。

复制失败（调整格消失/无应答、校准失败、目标人群分母为 0）时，**该估计量的方差
和区间留空**，并记录层、PSU 与失败环节；不拿成功复制拼一个方差。点估计与已完成
的复制诊断仍保留。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .data import Person
from .design import Design
from .errors import ResponseAdjustmentError
from .weights import (calibrate, compute_response_adjustment, point_estimates,
                      design_strata)

Z_95 = 1.96
METRICS = ("weighted_count", "outcome_total", "mean")
SCOPE_OVERALL = "overall"


@dataclass(frozen=True)
class ReplicateSpec:
    replicate_id: str
    stratum: str
    psu: str
    m_psu: int
    n_deleted: int


@dataclass
class ReplicateResult:
    spec: ReplicateSpec
    status: str                    # ok / failed
    failure_stage: str             # "" / adjustment / calibration / estimate
    failure_reason: str
    estimates: dict                # (scope, domain, metric) -> float | None
    n_people: int
    n_respondents: int
    calibration_iterations: int = 0

    def value(self, scope: str, domain: str, metric: str):
        if self.status != "ok":
            return None
        return self.estimates.get((scope, domain, metric))


def plan_replicates(people: list[Person], design: Design) -> list[ReplicateSpec]:
    """按层名、PSU 名排序生成删除复制；必入层不产生复制。"""
    strata = design_strata(people)
    specs: list[ReplicateSpec] = []
    for h in sorted(strata):
        if h in design.must_include_strata:
            continue
        psus = sorted(strata[h])
        m = len(psus)
        for psu in psus:
            specs.append(ReplicateSpec(
                replicate_id=f"{h}::{psu}", stratum=h, psu=psu,
                m_psu=m, n_deleted=len(strata[h][psu])))
    return specs


def _fsum(values) -> float:
    return math.fsum(values)


def run_replicate(people: list[Person], design: Design, spec: ReplicateSpec,
                  domains: list[str]) -> ReplicateResult:
    """执行一次删除复制：缩放同层基础权重 -> 应答调整 -> 校准 -> 点估计。"""
    h, psu, m = spec.stratum, spec.psu, spec.m_psu
    scale = m / (m - 1.0)

    keep: list[int] = []
    base: list[float] = []
    for i, p in enumerate(people):
        if p.stratum == h and p.psu == psu:
            continue                      # 删去该 PSU 的所有人
        keep.append(i)
        base.append(p.base_weight * scale if p.stratum == h else p.base_weight)

    sub_people = [people[i] for i in keep]
    n_resp = sum(1 for p in sub_people if p.response)

    full_cells = {p.adjustment_cell for p in people}
    sub_cells = {p.adjustment_cell for p in sub_people}
    disappeared = sorted(full_cells - sub_cells)
    if disappeared:
        return ReplicateResult(
            spec, "failed", "adjustment",
            f"删除后 adjustment_cell 消失: {disappeared}（禁止重新合并或跳过）",
            {}, len(sub_people), n_resp)

    try:
        adjustment = compute_response_adjustment(sub_people, base_weights=base)
    except ResponseAdjustmentError as e:
        return ReplicateResult(
            spec, "failed", "adjustment", str(e), {}, len(sub_people), n_resp)

    calibration = calibrate(sub_people, adjustment.weights, design)
    if not calibration.ok:
        return ReplicateResult(
            spec, "failed", "calibration",
            f"[{calibration.failed_stage}] {calibration.reason}",
            {}, len(sub_people), n_resp,
            calibration_iterations=calibration.iterations_used)

    estimates = point_estimates(sub_people, calibration.weights, domains)
    values: dict = {}
    for scope, est in [("overall", estimates.overall)] + [
            ("domain", d) for d in estimates.domains]:
        domain_key = SCOPE_OVERALL if scope == "overall" else est.domain
        for metric in METRICS:
            values[(scope, domain_key, metric)] = getattr(est, metric)
    return ReplicateResult(
        spec, "ok", "", "", values, len(sub_people), n_resp,
        calibration_iterations=calibration.iterations_used)


@dataclass
class EstimatorVariance:
    scope: str
    domain: str
    metric: str
    estimate: float | None
    estimate_status: str            # ok / missing
    estimate_reason: str
    variance: float | None
    se: float | None
    ci_low: float | None
    ci_high: float | None
    variance_status: str            # ok / unavailable
    variance_reason: str
    n_replicates_used: int
    n_replicates_failed: int
    strata_contributions: list     # [(stratum, m, n_psu, contribution|None, reason)]


def _failure_summary(replicates: list[ReplicateResult]) -> list[dict]:
    out = []
    for r in replicates:
        if r.status != "ok":
            out.append({
                "replicate_id": r.spec.replicate_id,
                "stratum": r.spec.stratum,
                "psu": r.spec.psu,
                "stage": r.failure_stage,
                "reason": r.failure_reason,
            })
    return out


def compute_variances(people: list[Person], design: Design, domains: list[str],
                      full_estimates, replicates: list[ReplicateResult]
                      ) -> list[EstimatorVariance]:
    """逐估计量计算 jackknife 方差；复制失败或估计缺失 -> 方差留空并说明。"""
    strata = design_strata(people)
    replicates_by_stratum: dict[str, list[ReplicateResult]] = {}
    for r in replicates:
        replicates_by_stratum.setdefault(r.spec.stratum, []).append(r)

    scopes = [("overall", SCOPE_OVERALL, full_estimates.overall)] + [
        ("domain", d.domain, d) for d in full_estimates.domains]

    out: list[EstimatorVariance] = []
    for scope, domain, full in scopes:
        for metric in METRICS:
            point = getattr(full, metric)
            if full.status != "ok" or point is None:
                out.append(EstimatorVariance(
                    scope=scope, domain=domain, metric=metric,
                    estimate=None, estimate_status="missing",
                    estimate_reason=full.reason or "点估计缺失",
                    variance=None, se=None, ci_low=None, ci_high=None,
                    variance_status="unavailable",
                    variance_reason="点估计缺失，无法计算方差与区间",
                    n_replicates_used=0, n_replicates_failed=len(replicates),
                    strata_contributions=[]))
                continue

            contributions: list = []
            stratum_contributions: list[float] = []
            blocked: list[str] = []
            used = 0
            failed = 0
            for h in sorted(replicates_by_stratum):
                m = len(strata[h])
                reps = sorted(replicates_by_stratum[h],
                              key=lambda r: r.spec.psu)
                squared: list[float] = []
                reason = ""
                for r in reps:
                    if r.status != "ok":
                        failed += 1
                        if not reason:
                            reason = (f"复制 {r.spec.replicate_id} 在 "
                                      f"{r.failure_stage} 环节失效：{r.failure_reason}")
                        continue
                    value = r.value(scope, domain, metric)
                    if value is None:
                        if not reason:
                            reason = (f"复制 {r.spec.replicate_id} 的目标人群 "
                                      f"{domain} 在删除后没有可估计的分母（估计缺失）")
                        continue
                    used += 1
                    squared.append((value - point) ** 2)
                if reason:
                    blocked.append(f"层 {h!r}：{reason}")
                    contributions.append({
                        "stratum": h, "m_psu": m, "n_replicates": len(reps),
                        "n_used": sum(1 for r in reps if r.status == "ok"
                                      and r.value(scope, domain, metric) is not None),
                        "contribution": None, "reason": reason})
                else:
                    contribution = (m - 1.0) / m * math.fsum(squared)
                    stratum_contributions.append(contribution)
                    contributions.append({
                        "stratum": h, "m_psu": m, "n_replicates": len(reps),
                        "n_used": len(reps),
                        "contribution": contribution, "reason": ""})

            if not replicates:
                out.append(EstimatorVariance(
                    scope=scope, domain=domain, metric=metric,
                    estimate=point, estimate_status="ok", estimate_reason="",
                    variance=None, se=None, ci_low=None, ci_high=None,
                    variance_status="unavailable",
                    variance_reason="设计中没有非必入层的可删除 PSU，无法估计方差；"
                                    "不把必入层方差当作 0",
                    n_replicates_used=0, n_replicates_failed=0,
                    strata_contributions=contributions))
                continue
            if blocked:
                out.append(EstimatorVariance(
                    scope=scope, domain=domain, metric=metric,
                    estimate=point, estimate_status="ok", estimate_reason="",
                    variance=None, se=None, ci_low=None, ci_high=None,
                    variance_status="unavailable",
                    variance_reason="部分复制失败/估计缺失，方差与区间留空（不拿成功复制凑数）："
                                    + "；".join(blocked),
                    n_replicates_used=used, n_replicates_failed=failed,
                    strata_contributions=contributions))
                continue

            se = math.sqrt(math.fsum(stratum_contributions))
            total_var = math.fsum(stratum_contributions)
            out.append(EstimatorVariance(
                scope=scope, domain=domain, metric=metric,
                estimate=point, estimate_status="ok", estimate_reason="",
                variance=total_var, se=se,
                ci_low=point - Z_95 * se, ci_high=point + Z_95 * se,
                variance_status="ok", variance_reason="",
                n_replicates_used=used, n_replicates_failed=failed,
                strata_contributions=contributions))
    return out


def must_include_summary(people: list[Person], design: Design) -> list[dict]:
    strata = design_strata(people)
    out = []
    for h in sorted(strata):
        out.append({
            "stratum": h,
            "n_psu": len(strata[h]),
            "must_include": h in design.must_include_strata,
            "produces_replicates": h not in design.must_include_strata,
            "variance_note": ("必入层：不产生删除复制；其方差贡献按设计不估计，"
                              "绝不当作 0" if h in design.must_include_strata else ""),
        })
    return out
