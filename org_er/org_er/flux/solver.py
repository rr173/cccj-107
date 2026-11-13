"""线性规划求解层：FBA、逐反应 FVA 与见证通量诊断。

所有 LP 都交给 SciPy HiGHS（linprog(method="highs")），本模块不实现求解器。
状态机（与 scipy 1.x 的 linprog.status 对齐，另在消息里保留 HiGHS 原文）：

    optimal / infeasible / unbounded / numerical_failure

关键纪律：

- FVA 的 min 与 max 是两个独立 LP，各自重新求解；绝不从 FBA 的一组解推导；
- 无界端点不偷偷加任何大上限，直接记录方向与 unbounded 状态；
- 每个有限解都保存完整见证通量，并独立代回守恒/上下界/保留约束算残差；
- 不做热力学假设，不消除内部循环——循环无界会如实出现在 FVA 端点里。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

from .errors import FluxSolverError
from .model import Network

# linprog.status -> 本作业的稳定状态词
_STATUS_WORDS = {
    0: "optimal",
    1: "numerical_failure",   # iteration limit 等
    2: "infeasible",
    3: "unbounded",
    4: "numerical_failure",
}


@dataclass(frozen=True)
class LPResult:
    status: str                       # optimal/infeasible/unbounded/numerical_failure
    message: str
    flux: tuple[float, ...] | None    # 仅 optimal 时有完整见证
    objective: float | None           # 仅 optimal 时有意义
    nit: int | None


@dataclass(frozen=True)
class WitnessDiagnostics:
    """把见证通量独立代回全部约束后的最大残差。"""

    max_balance_residual: float
    max_bound_violation: float
    biomass_retention_gap: float | None   # retain*z - v_biomass 的最大值；None=无保留约束
    solver_status: str
    valid: bool
    note: str


def build_matrices(network: Network):
    """构造内部物质的守恒矩阵 S（行=内部物质，列=反应）。"""
    internal = network.internal_metabolites
    row_of = {m: i for i, m in enumerate(internal)}
    s_mat = np.zeros((len(internal), len(network.reactions)), dtype=float)
    for j, rxn in enumerate(network.reactions):
        for met_id, coef in rxn.stoichiometry.items():
            if met_id in row_of:
                s_mat[row_of[met_id], j] = coef
    return s_mat, internal


def _bounds_arrays(lower, upper) -> list[tuple[float | None, float | None]]:
    return [(lo, hi) for lo, hi in zip(lower, upper)]


def _solve(c, a_eq, b_eq, bounds, a_ub=None, b_ub=None) -> LPResult:
    try:
        res = linprog(
            c=c,
            A_ub=a_ub,
            b_ub=b_ub,
            A_eq=a_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs",
        )
    except Exception as exc:  # HiGHS 内部异常统一归数值失败
        return LPResult("numerical_failure", f"HiGHS 异常: {exc!r}", None, None, None)

    status = _STATUS_WORDS.get(res.status, "numerical_failure")
    flux = None
    objective = None
    if status == "optimal":
        if res.x is None or not np.all(np.isfinite(res.x)):
            return LPResult("numerical_failure",
                            "HiGHS 声称最优但返回通量缺失或非有限", None, None,
                            getattr(res, "nit", None))
        flux = tuple(float(v) for v in res.x)
        objective = float(res.fun)
    return LPResult(status, str(res.message), flux, objective,
                    int(res.nit) if getattr(res, "nit", None) is not None else None)


def fba(network: Network, lower, upper) -> LPResult:
    """最大化 biomass 净通量（linprog 做最小化，系数取 -1）。"""
    s_mat, _ = build_matrices(network)
    n = len(network.reactions)
    c = np.zeros(n)
    c[network.reaction_index()[network.biomass_reaction]] = -1.0
    b_eq = np.zeros(s_mat.shape[0])
    return _solve(c, s_mat, b_eq, _bounds_arrays(lower, upper))


def fva_endpoint(network: Network, lower, upper, target_idx: int, direction: str,
                 retain_fraction: float, z: float) -> LPResult:
    """在 v_biomass >= retain_fraction*z 的同一可行域内，对单反应求 min/max。

    direction = "min" 最小化有符号通量；"max" 最大化。两个方向各自独立求解。
    """
    if direction not in ("min", "max"):
        raise FluxSolverError(f"未知 FVA 方向 {direction!r}")
    s_mat, _ = build_matrices(network)
    n = len(network.reactions)
    c = np.zeros(n)
    c[target_idx] = 1.0 if direction == "min" else -1.0

    bm_idx = network.reaction_index()[network.biomass_reaction]
    # -v_biomass <= -retain_fraction*z  等价于  v_biomass >= retain_fraction*z
    a_ub = np.zeros((1, n))
    a_ub[0, bm_idx] = -1.0
    b_ub = np.array([-retain_fraction * z])
    b_eq = np.zeros(s_mat.shape[0])
    return _solve(c, s_mat, b_eq, _bounds_arrays(lower, upper), a_ub, b_ub)


def diagnose_witness(network: Network, lower, upper, flux,
                     retain_fraction: float | None, z: float | None,
                     tolerance: float) -> WitnessDiagnostics:
    """独立把见证通量代回全部约束，残差超声明容差即 valid=False。

    检查三项：
      1. 内部物质守恒 S·v=0 的最大绝对残差；
      2. 上下界违背的最大量（界内为 0，null 方向不检查）；
      3. 生物量保留约束 v_biomass >= retain_fraction*z 的缺口（仅 FVA 见证）。
    """
    v = np.asarray(flux, dtype=float)
    s_mat, _ = build_matrices(network)
    balance = s_mat @ v
    max_balance = float(np.max(np.abs(balance))) if balance.size else 0.0

    max_bound = 0.0
    for j, (lo, hi) in enumerate(zip(lower, upper)):
        if lo is not None:
            max_bound = max(max_bound, float(lo - v[j]))
        if hi is not None:
            max_bound = max(max_bound, float(v[j] - hi))

    gap = None
    if retain_fraction is not None:
        bm_idx = network.reaction_index()[network.biomass_reaction]
        gap = float(retain_fraction * (z or 0.0) - v[bm_idx])

    valid = (max_balance <= tolerance
             and max_bound <= tolerance
             and (gap is None or gap <= tolerance)
             and bool(np.all(np.isfinite(v))))
    note = "所有约束残差在声明容差内" if valid else "见证残差超出声明容差，不能标为有效"
    return WitnessDiagnostics(
        max_balance_residual=max_balance,
        max_bound_violation=max(max_bound, 0.0),
        biomass_retention_gap=gap,
        solver_status="optimal",
        valid=valid,
        note=note,
    )


def classify_flux(min_value: float | None, max_value: float | None,
                  min_status: str, max_status: str, tolerance: float) -> str:
    """按两个端点的状态与零容差给反应分类。

    - blocked            两端都在容差内（|端点|<=tol）；
    - forward_only       下界 >= -tol（含固定正值），最大端有限正值；
    - reverse_only       上界 <= tol，最小端有限负值；
    - reversible         能正能负（min < -tol 且 max > tol）；
    - unbounded_min/max  对应端点无界，方向单独记录，绝不混作 blocked；
    - unavailable        至少一个有限端点数值失败，信息不足，不判 blocked。
    """
    if min_status == "numerical_failure" or max_status == "numerical_failure":
        return "unavailable"
    if min_status == "infeasible" or max_status == "infeasible":
        return "unavailable"
    if min_status == "unbounded" and max_status == "unbounded":
        return "unbounded_both"
    if min_status == "unbounded":
        return "unbounded_min"
    if max_status == "unbounded":
        return "unbounded_max"
    if min_value is None or max_value is None or not (
            math.isfinite(min_value) and math.isfinite(max_value)):
        return "unavailable"
    if abs(min_value) <= tolerance and abs(max_value) <= tolerance:
        return "blocked"
    if min_value < -tolerance and max_value > tolerance:
        return "reversible"
    if max_value <= tolerance:
        return "reverse_only"
    # 到这里 max > tol；min 可能是固定正值或贴近 0 的单向活动
    return "forward_only"
