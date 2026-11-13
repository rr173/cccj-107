#!/usr/bin/env python3
"""生成小型人工调查夹具与各类失败场景（仅标准库，独立于生产分析代码）。

主夹具 ``survey.csv`` + ``design.json`` 的设计意图：

- **同一 PSU 内的人相似**：outcome 以 PSU 中心值为主，个体只小幅偏离；
- **应答率随调整格变化**：A 格 11/13、B 格 8/11、C 格 7/11；
- **校准分布与样本不同**：样本中男性应答者 15/26，设计却要求 M=450/F=550；
  各年龄段的样本占比也与设计边际明显不同；
- 含一个**必入层 CERT**（单 PSU）与一个**没有应答者的 domain island**；
- 每个调整格、每个校准类别在删除任意单个 PSU 后仍有应答支持（复制可完成）。

另附 ``tiny.csv`` + ``tiny_design.json``：只有 6 人、1 层 2 PSU、单校准列，
所有最终权重都是精确分数（9/8、16/15、32/27 的组合），便于逐人手算核对。

失败场景（各自一对 CSV/JSON）：

- ``no_response_cell``：某调整格没有应答者（全样本应答调整失败）；
- ``unsupported_control``：校准类别已知人数为正但应答样本为零（无样本支持）；
- ``conflicting_margins``：两个边际结构上互斥（IPF 反复振荡、永不收敛）；
- ``replicate_loss``：某调整格只在个别 PSU 有应答者、某校准类别只由一个 PSU
  支撑——全样本可算，但删除特定 PSU 后链路失败；
- ``domain_loss``：某 domain 只有一个应答者，删除其 PSU 后该子人群分母为 0；
- ``single_psu``：单 PSU 层未声明必入（设计非法）。

坏数据夹具（用于行级校验）：身份重复、设计列空、非法权重、response/outcome
不一致、缺校准列。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "survey"


# ---------------------------------------------------------------- 主夹具
MAIN_HEADER = ["unit_id", "stratum", "psu", "base_weight", "response",
               "adjustment_cell", "domain", "outcome", "sex", "age_band"]

# (unit_id, stratum, psu, base_weight, response, adjustment_cell, domain,
#  outcome, sex, age_band)；outcome=None 表示未应答者不提供 outcome
MAIN_ROWS = [
    # S1 / P01：PSU 中心 5
    ("u01", "S1", "P01", 12, 1, "A", "metro", 5.2, "M", "young"),
    ("u02", "S1", "P01", 18, 1, "B", "metro", 4.8, "F", "young"),
    ("u03", "S1", "P01", 15, 1, "A", "metro", 5.5, "M", "mid"),
    ("u04", "S1", "P01", 20, 0, "C", "metro", None, "F", "mid"),
    ("u05", "S1", "P01", 10, 1, "B", "metro", 4.6, "M", "old"),
    # S1 / P02：PSU 中心 7
    ("u06", "S1", "P02", 14, 1, "A", "metro", 7.3, "F", "mid"),
    ("u07", "S1", "P02", 16, 1, "C", "metro", 6.7, "M", "old"),
    ("u08", "S1", "P02", 22, 0, "B", "metro", None, "M", "young"),
    ("u09", "S1", "P02", 11, 1, "B", "metro", 7.1, "F", "young"),
    ("u10", "S1", "P02", 13, 1, "A", "nonmetro", 7.6, "M", "mid"),
    # S2 / P03：PSU 中心 3
    ("u11", "S2", "P03", 25, 1, "A", "nonmetro", 3.2, "M", "young"),
    ("u12", "S2", "P03", 19, 1, "C", "nonmetro", 2.8, "F", "old"),
    ("u13", "S2", "P03", 17, 0, "A", "nonmetro", None, "F", "young"),
    ("u14", "S2", "P03", 21, 1, "B", "metro", 3.4, "M", "mid"),
    ("u15", "S2", "P03", 9, 1, "C", "nonmetro", 2.9, "F", "mid"),
    # S2 / P04：PSU 中心 9
    ("u16", "S2", "P04", 30, 1, "A", "metro", 9.1, "F", "old"),
    ("u17", "S2", "P04", 26, 0, "B", "metro", None, "M", "young"),
    ("u18", "S2", "P04", 24, 1, "B", "metro", 8.8, "M", "mid"),
    ("u19", "S2", "P04", 28, 1, "A", "metro", 9.4, "F", "young"),
    ("u20", "S2", "P04", 23, 1, "C", "nonmetro", 9.0, "M", "old"),
    # S2 / P05：PSU 中心 4
    ("u21", "S2", "P05", 12, 0, "C", "metro", None, "F", "young"),
    ("u22", "S2", "P05", 15, 1, "A", "metro", 4.2, "M", "mid"),
    ("u23", "S2", "P05", 17, 1, "B", "nonmetro", 3.8, "F", "old"),
    ("u24", "S2", "P05", 16, 1, "C", "nonmetro", 4.1, "M", "young"),
    ("u25", "S2", "P05", 14, 0, "A", "metro", None, "F", "mid"),
    # S3 / P06：PSU 中心 6
    ("u26", "S3", "P06", 8, 1, "A", "nonmetro", 6.2, "M", "young"),
    ("u27", "S3", "P06", 9, 1, "B", "nonmetro", 5.8, "F", "mid"),
    ("u28", "S3", "P06", 7, 0, "C", "nonmetro", None, "M", "old"),
    ("u29", "S3", "P06", 10, 1, "C", "nonmetro", 6.4, "F", "old"),
    # S3 / P07：PSU 中心 2
    ("u30", "S3", "P07", 6, 1, "A", "metro", 2.3, "F", "young"),
    ("u31", "S3", "P07", 8, 1, "C", "metro", 1.8, "M", "mid"),
    ("u32", "S3", "P07", 7, 0, "B", "metro", None, "F", "old"),
    # CERT / P08：必入层（单 PSU）
    ("u33", "CERT", "P08", 40, 1, "A", "metro", 8.2, "M", "old"),
    ("u34", "CERT", "P08", 35, 1, "B", "nonmetro", 7.8, "F", "young"),
    ("u35", "CERT", "P08", 30, 0, "C", "island", None, "M", "mid"),
]

MAIN_DESIGN = {
    "must_include_strata": ["CERT"],
    "calibration": {
        "columns": [
            {"name": "sex", "categories": {"M": 450, "F": 550}},
            {"name": "age_band",
             "categories": {"young": 400, "mid": 350, "old": 250}},
        ]
    },
    "max_iterations": 200,
    "tolerance": 1e-10,
}


# ---------------------------------------------------------------- 精确手算夹具
TINY_HEADER = ["unit_id", "stratum", "psu", "base_weight", "response",
               "adjustment_cell", "domain", "outcome", "grp"]
TINY_ROWS = [
    ("t1", "T", "T1", 2, 1, "E", "da", 10, "X"),
    ("t2", "T", "T1", 1, 1, "E", "da", 20, "X"),
    ("t3", "T", "T1", 1, 0, "E", "db", None, "Y"),
    ("t4", "T", "T1", 1, 1, "E", "db", 30, "Y"),
    ("t5", "T", "T2", 2, 1, "E", "da", 40, "X"),
    ("t6", "T", "T2", 2, 1, "E", "db", 50, "Y"),
]
TINY_DESIGN = {
    "must_include_strata": [],
    "calibration": {"columns": [
        {"name": "grp", "categories": {"X": 6, "Y": 4}},
    ]},
    "max_iterations": 100,
    "tolerance": 1e-12,
}


# ---------------------------------------------------------------- 场景
def scenario_rows():
    """返回 {场景名: (header, rows, design)}。"""
    scen = {}

    # 1) 某调整格完全没有应答者：全样本应答调整必须停下
    scen["no_response_cell"] = (
        ["unit_id", "stratum", "psu", "base_weight", "response",
         "adjustment_cell", "domain", "outcome", "band"],
        [
            ("n1", "S", "Q1", 4, 1, "live", "all", 5.0, "low"),
            ("n2", "S", "Q1", 4, 1, "live", "all", 6.0, "low"),
            ("n3", "S", "Q1", 3, 0, "dead", "all", None, "low"),
            ("n4", "S", "Q2", 5, 1, "live", "all", 7.0, "high"),
            ("n5", "S", "Q2", 5, 0, "dead", "all", None, "high"),
            ("n6", "S", "Q2", 6, 1, "live", "all", 8.0, "high"),
        ],
        {"must_include_strata": [],
         "calibration": {"columns": [
             {"name": "band", "categories": {"low": 12, "high": 12}}]},
         "max_iterations": 50, "tolerance": 1e-8},
    )

    # 2) 已知人数为正的控制类别没有任何应答样本
    scen["unsupported_control"] = (
        ["unit_id", "stratum", "psu", "base_weight", "response",
         "adjustment_cell", "domain", "outcome", "band"],
        [
            ("c1", "S", "R1", 5, 1, "e", "all", 1.0, "low"),
            ("c2", "S", "R1", 4, 1, "e", "all", 2.0, "low"),
            ("c3", "S", "R1", 3, 0, "e", "all", None, "high"),
            ("c4", "S", "R2", 6, 1, "e", "all", 3.0, "low"),
            ("c5", "S", "R2", 5, 0, "e", "all", None, "high"),
            ("c6", "S", "R2", 4, 1, "e", "all", 4.0, "low"),
        ],
        {"must_include_strata": [],
         "calibration": {"columns": [
             {"name": "band", "categories": {"low": 10, "high": 10}}]},
         "max_iterations": 50, "tolerance": 1e-8},
    )

    # 3) 两个边际结构上互斥：(a,p) 与 (b,q) 之外没有样本，
    #    但要求 a=150/p=50 与 b=50/q=150 —— IPF 永远振荡，绝不收敛
    scen["conflicting_margins"] = (
        ["unit_id", "stratum", "psu", "base_weight", "response",
         "adjustment_cell", "domain", "outcome", "group", "zone"],
        [
            ("m1", "S", "U1", 1, 1, "e", "all", 1.0, "a", "p"),
            ("m2", "S", "U1", 1, 1, "e", "all", 1.0, "a", "p"),
            ("m3", "S", "U1", 1, 1, "e", "all", 1.0, "b", "q"),
            ("m4", "S", "U2", 1, 1, "e", "all", 1.0, "b", "q"),
            ("m5", "S", "U2", 1, 1, "e", "all", 1.0, "a", "p"),
            ("m6", "S", "U2", 1, 1, "e", "all", 1.0, "b", "q"),
        ],
        {"must_include_strata": [],
         "calibration": {"columns": [
             {"name": "group", "categories": {"a": 150, "b": 50}},
             {"name": "zone", "categories": {"p": 50, "q": 150}}]},
         "max_iterations": 100, "tolerance": 1e-9},
    )

    # 4) 全样本可算，但删除特定 PSU 后调整格无应答者 / 校准类别失去支持
    scen["replicate_loss"] = (
        ["unit_id", "stratum", "psu", "base_weight", "response",
         "adjustment_cell", "domain", "outcome", "grp"],
        [
            # L1a：G1 的应答者全在这里
            ("r1", "L1", "L1a", 5, 1, "G1", "Da", 5.0, "X"),
            ("r2", "L1", "L1a", 4, 1, "G1", "Da", 6.0, "X"),
            ("r3", "L1", "L1a", 5, 1, "G2", "Da", 7.0, "X"),
            # L1b：G1 只剩未应答者
            ("r4", "L1", "L1b", 4, 0, "G1", "Da", None, "X"),
            ("r5", "L1", "L1b", 5, 1, "G2", "Da", 8.0, "X"),
            ("r6", "L1", "L1b", 6, 1, "G2", "Db", 9.0, "X"),
            # L2a：Y 的唯一应答者
            ("r7", "L2", "L2a", 7, 1, "G2", "Da", 10.0, "Y"),
            ("r8", "L2", "L2a", 6, 1, "G2", "Db", 11.0, "X"),
            # L2b：Y 只有未应答者；Dz 的唯一应答者
            ("r9", "L2", "L2b", 5, 0, "G2", "Db", None, "Y"),
            ("r10", "L2", "L2b", 6, 1, "G2", "Db", 12.0, "X"),
            ("r11", "L2", "L2b", 7, 1, "G2", "Dz", 13.0, "X"),
        ],
        {"must_include_strata": [],
         "calibration": {"columns": [
             {"name": "grp", "categories": {"X": 10, "Y": 10}}]},
         "max_iterations": 100, "tolerance": 1e-9},
    )

    # 5) 只有某次删除才让一个 domain 失去全部应答者（overall 方差仍可算）
    scen["domain_loss"] = (
        ["unit_id", "stratum", "psu", "base_weight", "response",
         "adjustment_cell", "domain", "outcome", "grp"],
        [
            ("d1", "D1", "D1a", 3, 1, "e", "main", 1.0, "X"),
            ("d2", "D1", "D1a", 4, 1, "e", "main", 2.0, "Y"),
            ("d3", "D1", "D1b", 3, 1, "e", "main", 3.0, "X"),
            ("d4", "D1", "D1b", 5, 0, "e", "main", None, "Y"),
            ("d5", "D2", "D2a", 4, 1, "e", "main", 4.0, "X"),
            ("d6", "D2", "D2a", 5, 1, "e", "main", 5.0, "Y"),
            ("d7", "D2", "D2b", 4, 1, "e", "main", 6.0, "X"),
            ("d8", "D2", "D2b", 5, 1, "e", "solo", 7.0, "Y"),
        ],
        {"must_include_strata": [],
         "calibration": {"columns": [
             {"name": "grp", "categories": {"X": 10, "Y": 10}}]},
         "max_iterations": 100, "tolerance": 1e-9},
    )
    return scen


def _csv_text(header, rows) -> str:
    def cell(v):
        return "" if v is None else str(v)
    lines = [",".join(header)]
    lines += [",".join(cell(v) for v in row) for row in rows]
    return "\n".join(lines) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_design(path: Path, design: dict) -> None:
    path.write_text(json.dumps(design, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def write_bad_data() -> dict:
    """坏数据夹具：每条只破坏一处，行号可由人直接对照。"""
    header = list(MAIN_HEADER)
    base = [list(r) for r in MAIN_ROWS]
    out = {}

    rows = [list(r) for r in base]
    rows.insert(4, list(base[0]))                       # 第 6 行重复 u01
    p = DATA / "bad_duplicate_unit.csv"
    _write(p, _csv_text(header, rows))
    out["duplicate_unit"] = p

    rows = [list(r) for r in base]
    rows[2][1] = ""                                     # 第 4 行 stratum 空
    p = DATA / "bad_missing_design.csv"
    _write(p, _csv_text(header, rows))
    out["missing_design"] = p

    rows = [list(r) for r in base]
    rows[0][3] = 0
    rows[7][3] = -5
    rows[12][3] = "abc"
    p = DATA / "bad_weight.csv"
    _write(p, _csv_text(header, rows))
    out["bad_weight"] = p

    rows = [list(r) for r in base]
    rows[0][7] = ""                                     # response=1 但 outcome 空
    rows[1][7] = "nan"
    rows[2][7] = ""                                     # response=0：允许空
    p = DATA / "bad_outcome.csv"
    _write(p, _csv_text(header, rows))
    out["bad_outcome"] = p

    rows = [list(r) for r in base]
    header_no_sex = [h for h in header if h != "sex"]
    rows = [[v for h, v in zip(header, r) if h != "sex"] for r in rows]
    p = DATA / "bad_missing_calibration_column.csv"
    _write(p, _csv_text(header_no_sex, rows))
    out["missing_calibration_column"] = p
    return out


def validate_fixture(header, rows, design) -> None:
    """生成器自检：主夹具每个调整格/校准类别在删除任一 PSU 后仍有应答支持。"""
    cols = design["calibration"]["columns"]
    people = [dict(zip(header, r)) for r in rows]
    strata: dict[str, set[str]] = {}
    for p in people:
        strata.setdefault(p["stratum"], set()).add(p["psu"])
    must = set(design["must_include_strata"])
    for h in strata:
        assert h in must or len(strata[h]) >= 2, f"层 {h} 未声明必入但只有 1 个 PSU"

    cells = {p["adjustment_cell"] for p in people}
    # 全样本：每个调整格都有应答者
    for c in cells:
        assert any(p["response"] == 1 and p["adjustment_cell"] == c for p in people), (
            f"调整格 {c} 全样本没有应答者（主夹具不应如此）")

    for h, psus in strata.items():
        if h in must:
            continue
        for psu in psus:
            rest = [p for p in people
                    if not (p["stratum"] == h and p["psu"] == psu)]
            for c in cells:
                assert any(p["response"] == 1 and p["adjustment_cell"] == c
                           for p in rest), (
                    f"删除 {h}/{psu} 后调整格 {c} 无应答者，主夹具不应如此")
            for col in cols:
                for cat in col["categories"]:
                    assert any(p["response"] == 1 and p[col["name"]] == cat
                               for p in rest), (
                        f"删除 {h}/{psu} 后校准类别 {col['name']}={cat} 无应答支持")


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    validate_fixture(MAIN_HEADER, MAIN_ROWS, MAIN_DESIGN)

    _write(DATA / "survey.csv", _csv_text(MAIN_HEADER, MAIN_ROWS))
    _write_design(DATA / "design.json", MAIN_DESIGN)
    _write(DATA / "tiny.csv", _csv_text(TINY_HEADER, TINY_ROWS))
    _write_design(DATA / "tiny_design.json", TINY_DESIGN)

    scen_dir = DATA / "scenarios"
    scen_dir.mkdir(parents=True, exist_ok=True)
    for name, (header, rows, design) in scenario_rows().items():
        _write(scen_dir / f"{name}.csv", _csv_text(header, rows))
        _write_design(scen_dir / f"{name}_design.json", design)

    # 单 PSU 未声明必入：设计非法
    _write(scen_dir / "single_psu.csv", _csv_text(
        ["unit_id", "stratum", "psu", "base_weight", "response",
         "adjustment_cell", "domain", "outcome", "band"],
        [("s1", "LONELY", "P", 2, 1, "e", "all", 1.0, "low"),
         ("s2", "LONELY", "P", 3, 1, "e", "all", 2.0, "high")]))
    _write_design(scen_dir / "single_psu_design.json",
                  {"must_include_strata": [],
                   "calibration": {"columns": [
                       {"name": "band", "categories": {"low": 5, "high": 5}}]},
                   "max_iterations": 50, "tolerance": 1e-8})

    bad = write_bad_data()
    print(f"主夹具: {DATA / 'survey.csv'}（{len(MAIN_ROWS)} 行，必入层 CERT）")
    print(f"手算夹具: {DATA / 'tiny.csv'}（{len(TINY_ROWS)} 行）")
    print(f"场景: {scen_dir}（{len(scenario_rows()) + 1} 个）")
    print(f"坏数据夹具: {', '.join(p.name for p in bad.values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
