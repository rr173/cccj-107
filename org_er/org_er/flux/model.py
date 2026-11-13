"""网络与条件文件的读取、严格校验与条件快照构建。

约定（与题目一致）：

- metabolites: [{"id", "boundary"}]，id 唯一；
- reactions:   [{"id", "stoichiometry": {met_id: 系数}, "lower", "upper"}]，
  id 唯一；负系数=消耗、正系数=生成，系数必须非零且有限；
- 边界物质不进守恒方程，内部物质必须满足 S·v=0；
- lower/upper 用 null 表示该方向无界，其余必须有限且 lower<=upper；
  可逆反应用负下界表达，绝不先取绝对值或拆反应；
- biomass 反应的有效下界必须非负，目标是最大化其净通量。

条件文件每个条件给 replacements（替换上下界）与 knockouts（固定为 0）。
条件之间互不继承，每个条件都从原网络的不可变快照重新出发。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from .errors import FluxInputError


def _is_finite_number(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _load_json(path: str | Path, label: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise FluxInputError(f"{label}文件不存在: {p}")
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise FluxInputError(f"{label}不是合法 JSON（{p}）: {exc}") from exc
    if not isinstance(data, dict):
        raise FluxInputError(f"{label}顶层必须是对象（{p}）")
    return data


@dataclass(frozen=True)
class Reaction:
    id: str
    stoichiometry: dict[str, float]
    lower: float | None
    upper: float | None


@dataclass(frozen=True)
class Network:
    """不可变网络快照。反应顺序按输入固定，矩阵列也按此顺序排列。"""

    metabolites: tuple[str, ...]
    boundary: frozenset[str]
    reactions: tuple[Reaction, ...]
    biomass_reaction: str
    source_path: str

    @property
    def internal_metabolites(self) -> tuple[str, ...]:
        return tuple(m for m in self.metabolites if m not in self.boundary)

    @property
    def reaction_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.reactions)

    def reaction_index(self) -> dict[str, int]:
        return {r.id: i for i, r in enumerate(self.reactions)}


def load_network(path: str | Path) -> Network:
    """读取并严格校验网络文件。任何结构问题都带位置地报 FluxInputError。"""
    data = _load_json(path, "网络文件")

    raw_mets = data.get("metabolites")
    if not isinstance(raw_mets, list) or not raw_mets:
        raise FluxInputError("网络文件 metabolites 必须是非空列表")
    metabolites: list[str] = []
    boundary: set[str] = set()
    for pos, item in enumerate(raw_mets):
        if not isinstance(item, dict):
            raise FluxInputError(f"metabolites[{pos}] 必须是对象")
        mid = item.get("id")
        if not isinstance(mid, str) or not mid:
            raise FluxInputError(f"metabolites[{pos}].id 必须是非空字符串")
        if mid in metabolites:
            raise FluxInputError(f"代谢物 id 重复: {mid!r}（首次出现于 metabolites[{metabolites.index(mid)}]）")
        if not isinstance(item.get("boundary"), bool):
            raise FluxInputError(f"metabolites[{pos}]（{mid}）的 boundary 必须是布尔值")
        metabolites.append(mid)
        if item["boundary"]:
            boundary.add(mid)
    known = frozenset(metabolites)

    raw_rxns = data.get("reactions")
    if not isinstance(raw_rxns, list) or not raw_rxns:
        raise FluxInputError("网络文件 reactions 必须是非空列表")
    reactions: list[Reaction] = []
    seen_rxn: set[str] = set()
    for pos, item in enumerate(raw_rxns):
        if not isinstance(item, dict):
            raise FluxInputError(f"reactions[{pos}] 必须是对象")
        rid = item.get("id")
        if not isinstance(rid, str) or not rid:
            raise FluxInputError(f"reactions[{pos}].id 必须是非空字符串")
        if rid in seen_rxn:
            raise FluxInputError(f"反应 id 重复: {rid!r}（reactions[{pos}]）")
        seen_rxn.add(rid)

        stoich = item.get("stoichiometry")
        if not isinstance(stoich, dict) or not stoich:
            raise FluxInputError(f"反应 {rid!r}（reactions[{pos}]）的 stoichiometry 必须是非空对象")
        clean_stoich: dict[str, float] = {}
        for met_id, coef in stoich.items():
            if not isinstance(met_id, str) or not met_id:
                raise FluxInputError(f"反应 {rid!r} 的 stoichiometry 含非字符串键: {met_id!r}")
            if met_id not in known:
                raise FluxInputError(
                    f"反应 {rid!r} 引用了不存在的代谢物 {met_id!r}（stoichiometry 键）")
            if not _is_finite_number(coef):
                raise FluxInputError(f"反应 {rid!r} 对 {met_id!r} 的系数必须是有限数，得到 {coef!r}")
            if coef == 0:
                raise FluxInputError(f"反应 {rid!r} 对 {met_id!r} 的系数为 0；系数须非零")
            clean_stoich[met_id] = float(coef)

        lower, upper = item.get("lower"), item.get("upper")
        if lower is not None and not _is_finite_number(lower):
            raise FluxInputError(f"反应 {rid!r} 的 lower 必须是有限数或 null，得到 {lower!r}")
        if upper is not None and not _is_finite_number(upper):
            raise FluxInputError(f"反应 {rid!r} 的 upper 必须是有限数或 null，得到 {upper!r}")
        lower_f = None if lower is None else float(lower)
        upper_f = None if upper is None else float(upper)
        if lower_f is not None and upper_f is not None and lower_f > upper_f:
            raise FluxInputError(f"反应 {rid!r} 的 lower({lower_f}) > upper({upper_f})")

        reactions.append(Reaction(rid, clean_stoich, lower_f, upper_f))

    biomass = data.get("biomass_reaction")
    if not isinstance(biomass, str) or not biomass:
        raise FluxInputError("网络文件 biomass_reaction 必须是非空字符串 id")
    if biomass not in seen_rxn:
        raise FluxInputError(f"biomass_reaction {biomass!r} 不在 reactions 中")
    bm = next(r for r in reactions if r.id == biomass)
    if bm.lower is not None and bm.lower < 0.0:
        raise FluxInputError(
            f"biomass 反应 {biomass!r} 的下界必须非负，得到 lower={bm.lower}")

    return Network(
        metabolites=tuple(metabolites),
        boundary=frozenset(boundary),
        reactions=tuple(reactions),
        biomass_reaction=biomass,
        source_path=str(Path(path).resolve()),
    )


@dataclass(frozen=True)
class Condition:
    id: str
    replacements: dict[str, tuple[float | None, float | None]]
    knockouts: frozenset[str]
    order: int


@dataclass(frozen=True)
class ConditionBundle:
    conditions: tuple[Condition, ...]
    source_path: str


def load_conditions(path: str | Path, network: Network) -> ConditionBundle:
    """读取条件文件并对照网络校验。报错都带条件 id 与位置。"""
    data = _load_json(path, "条件文件")
    raw = data.get("conditions")
    if not isinstance(raw, list) or not raw:
        raise FluxInputError("条件文件 conditions 必须是非空列表")

    known = set(network.reaction_ids)
    conditions: list[Condition] = []
    seen_ids: set[str] = set()
    for pos, item in enumerate(raw):
        where = f"conditions[{pos}]"
        if not isinstance(item, dict):
            raise FluxInputError(f"{where} 必须是对象")
        cid = item.get("id")
        if not isinstance(cid, str) or not cid:
            raise FluxInputError(f"{where}.id 必须是非空字符串")
        if cid in seen_ids:
            raise FluxInputError(f"条件 id 重复: {cid!r}（{where}）")
        seen_ids.add(cid)
        where = f"条件 {cid!r}（conditions[{pos}]）"

        replacements_raw = item.get("replacements", {})
        if not isinstance(replacements_raw, dict):
            raise FluxInputError(f"{where} 的 replacements 必须是对象")
        replacements: dict[str, tuple[float | None, float | None]] = {}
        for rid, bounds in replacements_raw.items():
            if rid not in known:
                raise FluxInputError(f"{where} 的 replacements 引用未知反应 {rid!r}")
            if not isinstance(bounds, dict):
                raise FluxInputError(f"{where} 的 replacements[{rid!r}] 必须是对象")
            if "lower" not in bounds or "upper" not in bounds:
                raise FluxInputError(
                    f"{where} 的 replacements[{rid!r}] 必须同时给出 lower 与 upper（可用 null）")
            lower, upper = bounds["lower"], bounds["upper"]
            if lower is not None and not _is_finite_number(lower):
                raise FluxInputError(f"{where} 的 replacements[{rid!r}].lower 非法: {lower!r}")
            if upper is not None and not _is_finite_number(upper):
                raise FluxInputError(f"{where} 的 replacements[{rid!r}].upper 非法: {upper!r}")
            lower_f = None if lower is None else float(lower)
            upper_f = None if upper is None else float(upper)
            # 注意：条件替换允许 lower>upper——那是“故意不可行”实验，
            # 交给 LP 可行性判定；基础网络的 lower<=upper 仍在 load_network 严格校验。
            replacements[rid] = (lower_f, upper_f)

        knockouts_raw = item.get("knockouts", [])
        if not isinstance(knockouts_raw, list):
            raise FluxInputError(f"{where} 的 knockouts 必须是列表")
        knockouts: list[str] = []
        for rid in knockouts_raw:
            if not isinstance(rid, str) or not rid:
                raise FluxInputError(f"{where} 的 knockouts 含非字符串项: {rid!r}")
            if rid not in known:
                raise FluxInputError(f"{where} 的 knockouts 引用未知反应 {rid!r}")
            knockouts.append(rid)
        if len(set(knockouts)) != len(knockouts):
            dupes = sorted({rid for rid in knockouts if knockouts.count(rid) > 1})
            raise FluxInputError(f"{where} 的 knockouts 内反应重复: {dupes}")
        overlap = sorted(set(knockouts) & set(replacements))
        if overlap:
            raise FluxInputError(f"{where} 同时替换又敲除反应: {overlap}")
        # 敲除 biomass 反应是合法实验（切断全部生物量供给），不在这里拦截。

        conditions.append(Condition(
            id=cid,
            replacements=replacements,
            knockouts=frozenset(knockouts),
            order=pos,
        ))

    return ConditionBundle(tuple(conditions), str(Path(path).resolve()))


@dataclass(frozen=True)
class EffectiveBounds:
    """一个条件应用后的完整上下界（从原网络快照派生，不改动原网络）。"""

    lower: tuple[float | None, ...]
    upper: tuple[float | None, ...]
    changes: dict[str, dict] = field(default_factory=dict)


def apply_condition(network: Network, condition: Condition) -> EffectiveBounds:
    """从原网络出发应用单个条件，返回该条件专属的上下界副本。"""
    lower: list[float | None] = []
    upper: list[float | None] = []
    changes: dict[str, dict] = {}
    for rxn in network.reactions:
        lo, hi = rxn.lower, rxn.upper
        if rxn.id in condition.knockouts:
            lo, hi = 0.0, 0.0
            changes[rxn.id] = {"op": "knockout",
                               "original": [rxn.lower, rxn.upper],
                               "applied": [0.0, 0.0]}
        elif rxn.id in condition.replacements:
            new_lo, new_hi = condition.replacements[rxn.id]
            changes[rxn.id] = {"op": "replace",
                               "original": [rxn.lower, rxn.upper],
                               "applied": [new_lo, new_hi]}
            lo, hi = new_lo, new_hi
        lower.append(lo)
        upper.append(hi)
    return EffectiveBounds(tuple(lower), tuple(upper), changes)
