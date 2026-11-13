#!/usr/bin/env python3
"""生成稳态通量分析的微型人工网络与条件夹具（data/flux/）。

全部网络都小到可以手算：守恒方程只有几行，最优目标与 FVA 范围都是
线性约束的直接交点。run_flux_reference_check.py 不用任何生产代码，
仅凭守恒关系重新推导这些期望值并逐项核对见证。
"""
import json
from pathlib import Path

import _bootstrap  # noqa: F401

ROOT = Path(_bootstrap.ROOT) / "data" / "flux"


def _write(name: str, payload: dict) -> Path:
    path = ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


# ---------------------------------------------------------------- 主网络 A
#
# 限量碳源 C_ext --EX_C--> C，两条替代合成路线经中间物 M/X 生成 P：
#   C --syn1--> M；C --syn2--> X
#   可逆反应 iso: M <-> X（lower=-8 表达可逆，不拆反应、不取绝对值）
#   M --p1--> P；X --p2--> P；P --GROWTH--> BIO
#   M --EX_M--> M_ext、X --EX_X--> X_ext（两条独立排出，
#   使 iso 在 GROWTH=0 时两侧都有出口，真正能正能负）
# 内部循环 cyc1/cyc2: Y1 -> Y2 -> Y1（与主通路完全无关，有界 [0,3]）
#
# 守恒（内部 C,M,X,P,Y1,Y2；边界 C_ext,BIO,M_ext,X_ext），逐元素平衡：
#   C : EX_C - syn1 - syn2 = 0
#   M : syn1 - iso - p1 - EX_M = 0
#   X : syn2 + iso - p2 - EX_X = 0
#   P : p1 + p2 - GROWTH = 0
#   Y1: -cyc1 + cyc2 = 0
#   Y2:  cyc1 - cyc2 = 0
#
# 四主式相加：GROWTH + EX_M + EX_X = syn1 + syn2 = EX_C（严格守恒、路线等产量）。
# EX_C <= 10 且排出不产 P，故基线 z* = 10（两个排出都为 0 时达到）。
# 各条件的手算范围与交点推导见 run_flux_reference_check.py 的 EXPECTED。
def demo_network() -> dict:
    return {
        "metabolites": [
            {"id": "C_ext", "boundary": True},
            {"id": "BIO", "boundary": True},
            {"id": "M_ext", "boundary": True},
            {"id": "X_ext", "boundary": True},
            {"id": "C", "boundary": False},
            {"id": "M", "boundary": False},
            {"id": "X", "boundary": False},
            {"id": "P", "boundary": False},
            {"id": "Y1", "boundary": False},
            {"id": "Y2", "boundary": False},
        ],
        "reactions": [
            {"id": "EX_C",
             "stoichiometry": {"C_ext": -1, "C": 1},
             "lower": 0.0, "upper": 10.0},
            {"id": "syn1",
             "stoichiometry": {"C": -1, "M": 1},
             "lower": 0.0, "upper": None},
            {"id": "syn2",
             "stoichiometry": {"C": -1, "X": 1},
             "lower": 0.0, "upper": None},
            {"id": "iso",
             "stoichiometry": {"M": -1, "X": 1},
             "lower": -8.0, "upper": 8.0},
            {"id": "p1",
             "stoichiometry": {"M": -1, "P": 1},
             "lower": 0.0, "upper": None},
            {"id": "p2",
             "stoichiometry": {"X": -1, "P": 1},
             "lower": 0.0, "upper": None},
            {"id": "GROWTH",
             "stoichiometry": {"P": -1, "BIO": 1},
             "lower": 0.0, "upper": None},
            {"id": "EX_M",
             "stoichiometry": {"M": -1, "M_ext": 1},
             "lower": 0.0, "upper": None},
            {"id": "EX_X",
             "stoichiometry": {"X": -1, "X_ext": 1},
             "lower": 0.0, "upper": None},
            {"id": "cyc1",
             "stoichiometry": {"Y1": -1, "Y2": 1},
             "lower": 0.0, "upper": 3.0},
            {"id": "cyc2",
             "stoichiometry": {"Y2": -1, "Y1": 1},
             "lower": 0.0, "upper": 3.0},
        ],
        "biomass_reaction": "GROWTH",
    }


def demo_conditions() -> dict:
    return {
        "conditions": [
            # 0) 基线：不做任何修改，z*=10
            {"id": "baseline",
             "replacements": {},
             "knockouts": []},
            # 1) 限制摄取：EX_C 上限 10 -> 4，z*=4
            {"id": "carbon_limited",
             "replacements": {"EX_C": {"lower": 0.0, "upper": 4.0}},
             "knockouts": []},
            # 2) 切断替代路线 syn1：只剩 syn2 一侧仍可达同样的 z*（替代最优解）
            {"id": "route1_knockout",
             "replacements": {},
             "knockouts": ["syn1"]},
            # 3) 切断另一条替代路线 syn2：对称的另一组最优解
            {"id": "route2_knockout",
             "replacements": {},
             "knockouts": ["syn2"]},
            # 4) 切断全部生物量供给：GROWTH 固定 0，系统仍可行（z=0），继续做 FVA；
            #    此时 iso 能正能负（reversible），主通路反应全部 blocked
            {"id": "no_biomass",
             "replacements": {},
             "knockouts": ["GROWTH"]},
            # 5) 故意不可行：要求 EX_C >= 6 又 <= 4（lower>upper 是合法实验描述）
            {"id": "forced_infeasible",
             "replacements": {"EX_C": {"lower": 6.0, "upper": 4.0}},
             "knockouts": []},
        ]
    }


# ---------------------------------------------------------------- 无界目标网络 B
#
# C_ext --import--> C --grow--> BIO，两条反应上界均为 null。
# 守恒 C: import - grow = 0；grow 无上界 -> 最大生物量无界。
def unbounded_objective_network() -> dict:
    return {
        "metabolites": [
            {"id": "C_ext", "boundary": True},
            {"id": "BIO", "boundary": True},
            {"id": "C", "boundary": False},
        ],
        "reactions": [
            {"id": "import",
             "stoichiometry": {"C_ext": -1, "C": 1},
             "lower": 0.0, "upper": None},
            {"id": "grow",
             "stoichiometry": {"C": -1, "BIO": 1},
             "lower": 0.0, "upper": None},
        ],
        "biomass_reaction": "grow",
    }


def unbounded_conditions() -> dict:
    return {
        "conditions": [
            # 无界目标：不做依赖有限 z 的 FVA
            {"id": "obj_unbounded", "replacements": {}, "knockouts": []},
            # 同一网络加上限后变有限，证明无界来自缺失的上界而非结构矛盾
            {"id": "capped",
             "replacements": {"import": {"lower": 0.0, "upper": 7.0}},
             "knockouts": []},
        ]
    }


# ------------------------------------------------ 有限目标 + 无界循环网络 C
#
# 主通路有界：feed <= 5，bio 守恒要求 bio = feed，故 z=5 有限。
# 循环 lp/lm: L1 -> L2 -> L1，两条循环反应上下界都为 null，
# 守恒只要求 lp = lm，其公共通量可任意大 -> FVA 两端点无界（目标仍有限）。
def unbounded_cycle_network() -> dict:
    return {
        "metabolites": [
            {"id": "S_ext", "boundary": True},
            {"id": "BIO", "boundary": True},
            {"id": "S", "boundary": False},
            {"id": "L1", "boundary": False},
            {"id": "L2", "boundary": False},
        ],
        "reactions": [
            {"id": "feed",
             "stoichiometry": {"S_ext": -1, "S": 1},
             "lower": 0.0, "upper": 5.0},
            {"id": "bio",
             "stoichiometry": {"S": -1, "BIO": 1},
             "lower": 0.0, "upper": None},
            {"id": "lp",
             "stoichiometry": {"L1": -1, "L2": 1},
             "lower": None, "upper": None},
            {"id": "lm",
             "stoichiometry": {"L2": -1, "L1": 1},
             "lower": None, "upper": None},
        ],
        "biomass_reaction": "bio",
    }


def cycle_conditions() -> dict:
    return {"conditions": [
        {"id": "finite_obj_free_cycle", "replacements": {}, "knockouts": []}]}


# ---------------------------------------------------------------- 坏输入夹具
def bad_inputs() -> dict[str, dict]:
    net = demo_network()

    unknown = json.loads(json.dumps(net))
    unknown["reactions"][0]["stoichiometry"]["GHOST"] = 1.0

    bad_bounds = json.loads(json.dumps(net))
    bad_bounds["reactions"][0]["upper"] = 3.0
    bad_bounds["reactions"][0]["lower"] = 4.0

    bad_biomass = json.loads(json.dumps(net))
    growth_idx = next(i for i, r in enumerate(net["reactions"])
                      if r["id"] == "GROWTH")
    bad_biomass["reactions"][growth_idx]["lower"] = -1.0

    return {
        "bad_unknown_metabolite.json": unknown,
        "bad_bounds.json": bad_bounds,
        "bad_biomass_lower.json": bad_biomass,
    }


def bad_conditions_file() -> dict:
    return {"conditions": [
        # 同一反应既替换又敲除
        {"id": "conflict",
         "replacements": {"syn1": {"lower": 0.0, "upper": 1.0}},
         "knockouts": ["syn1"]},
        # 未知反应
        {"id": "ghost",
         "replacements": {"NO_SUCH_RXN": {"lower": 0.0, "upper": 1.0}},
         "knockouts": []},
        # 重复条件 id
        {"id": "dup", "replacements": {}, "knockouts": []},
        {"id": "dup", "replacements": {}, "knockouts": []},
    ]}


def main() -> None:
    _write("demo_network.json", demo_network())
    _write("demo_conditions.json", demo_conditions())
    _write("unbounded_objective_network.json", unbounded_objective_network())
    _write("unbounded_objective_conditions.json", unbounded_conditions())
    _write("unbounded_cycle_network.json", unbounded_cycle_network())
    _write("unbounded_cycle_conditions.json", cycle_conditions())
    for name, payload in bad_inputs().items():
        _write(name, payload)
    _write("bad_conditions.json", bad_conditions_file())
    print(f"通量分析夹具已写入 {ROOT}")


if __name__ == "__main__":
    main()
