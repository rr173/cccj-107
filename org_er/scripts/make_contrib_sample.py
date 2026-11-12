#!/usr/bin/env python3
"""生成论文署名贡献核算的小样本目录（data/papers.jsonl，确定性）。

场景覆盖：
  P1  跨供应方重复收录：DOI 写法不同（链接前缀/大小写/空白）归并为一篇；
      两版本年份冲突 -> 按优先级选取，记录 year_differs
  P2  作者顺序冲突 -> 按优先级选取，记录 author_order_differs
  P3  同一机构多条署名 -> 先去重再分摊
  P4  部分署名未定位 -> 只在已定位机构间分配，未定位引用保留
  P5  署名全部未定位 / 无署名 -> 整份记入待归属
  P6  优先级打平且作者名单不同 -> 整篇隔离，不计贡献
  P7  优先级打平但内容一致 -> 计入（确定序取一）
  P8  无 DOI -> 按供应方+论文号独立保留；同供应方同号重复 -> 归并
  P9  坏行：空作者名单、非法年份、作者结构损坏、JSON 残缺 -> 说明原因，不计入
  P11 署名指向已撤回条目（round 1 撤回）-> 该引用未定位，其余正常定位
"""
import argparse
import json
from pathlib import Path

import _bootstrap  # noqa: F401

ACME_CH = {"source": "companies_house", "entry": "GB-ACME-1"}     # round>=0 与 oc/998877 同实体
ACME_OC = {"source": "opencorporates", "entry": "oc/998877"}
YUNTU_CN = {"source": "cn_registry", "entry": "CN-YUNTU-1"}       # 与 oc/cn-55 同实体
YUNTU_OC = {"source": "opencorporates", "entry": "oc/cn-55"}
QUIX_DE = {"source": "handelsregister", "entry": "DE-QUX-1"}
UMB_A = {"source": "dir_a", "entry": "A-4001"}                    # round>=2 与 GB-UMB-1 同实体
NIM_SEC = {"source": "sec_filings", "entry": "US-NIM-1"}
NIM_RETRACTED = {"source": "dir_a", "entry": "A-3001"}            # round 1 已撤回 -> 未定位
UNKNOWN_X = {"source": "dir_x", "entry": "UNKNOWN-9"}             # 从未存在 -> 未定位
UNKNOWN_1 = {"source": "dir_x", "entry": "UNKNOWN-1"}
UNKNOWN_2 = {"source": "dir_y", "entry": "UNKNOWN-2"}

P1_AUTHORS = [
    {"name": "Alice Zhang", "affils": [ACME_CH, YUNTU_CN]},
    {"name": "Bob Smith", "affils": [QUIX_DE]},
    {"name": "Carol Lee"},  # 无署名 -> 待归属
]

LINES = [
    # ---- P1 跨供应方重复收录（DOI 写法不同），年份冲突按优先级取 crossref 版
    dict(supplier="crossref", paper_id="cr-0001", doi="https://doi.org/10.1000/XYZ.123",
         year=2023, authors=P1_AUTHORS, scenario="P1_cross_supplier_dup"),
    dict(supplier="wos", paper_id="w-100", doi="10.4000/CONF.1", year=2023,
         authors=[{"name": "Liz Chen", "affils": [ACME_CH]}], scenario="P6_tie_conflict"),
    dict(supplier="crossref", paper_id="cr-0003", doi="10.3000/DEDUP.1", year=2023,
         authors=[{"name": "Frank Wu", "affils": [ACME_CH, ACME_OC]},   # 两条署名 -> 同一实体
                  {"name": "Grace Ho", "affils": [YUNTU_OC]}],
         scenario="P3_same_institution_dedup"),
    dict(supplier="scopus", paper_id="sc-9001", doi=" 10.1000/xyz.123 ", year=2022,
         authors=P1_AUTHORS, scenario="P1_cross_supplier_dup"),
    # ---- P8 无 DOI：按供应方+论文号独立保留
    dict(supplier="local_ir", paper_id="ir-0001", doi="", year=2023,
         authors=[{"name": "Owen Sun", "affils": [YUNTU_CN]}], scenario="P8_no_doi"),
    dict(supplier="crossref", paper_id="cr-0002", doi="10.2000/AAA.1", year=2024,
         authors=[{"name": "Dana Xu", "affils": [ACME_CH]},
                  {"name": "Eli Zhou", "affils": [YUNTU_CN]}], scenario="P2_author_order"),
    dict(supplier="crossref", paper_id="cr-0005", doi="10.3002/NONE.1", year=2023,
         authors=[{"name": "Jack Ma", "affils": [UNKNOWN_1, UNKNOWN_2]},  # 全部未定位
                  {"name": "Kate Lin", "affils": []}],                    # 无署名
         scenario="P5_unlocated_all"),
    dict(supplier="pubmed", paper_id="pm-100", doi="10.4000/conf.1", year=2023,
         authors=[{"name": "Liz Chen", "affils": [ACME_CH]},
                  {"name": "Max Li", "affils": [YUNTU_CN]}], scenario="P6_tie_conflict"),
    # 同供应方同号无 DOI 重复（内容一致）-> 归并为一篇
    dict(supplier="local_ir", paper_id="ir-0001", doi=None, year=2023,
         authors=[{"name": "Owen Sun", "affils": [YUNTU_CN]}], scenario="P8_no_doi"),
    dict(supplier="crossref", paper_id="cr-0004", doi="10.3001/PART.1", year=2024,
         authors=[{"name": "Henry Gu", "affils": [ACME_CH, UNKNOWN_X]},   # 部分未定位
                  {"name": "Ivy Shen", "affils": [UMB_A]}], scenario="P4_partial_unlocated"),
    dict(supplier="wos", paper_id="w-101", doi="10.4001/SAME.1", year=2024,
         authors=[{"name": "Nina Zhao", "affils": [ACME_CH]}], scenario="P7_tie_identical"),
    dict(supplier="scopus", paper_id="sc-9002", doi="DOI:10.2000/aaa.1", year=2024,
         authors=[{"name": "Eli Zhou", "affils": [YUNTU_CN]},             # 作者顺序不同
                  {"name": "Dana Xu", "affils": [ACME_CH]}], scenario="P2_author_order"),
    dict(supplier="crossref", paper_id="cr-0008", year=2023,              # 无 doi 字段
         authors=[{"name": "Owen Sun", "affils": [YUNTU_CN]}], scenario="P8_no_doi"),
    dict(supplier="pubmed", paper_id="pm-101", doi="http://dx.doi.org/10.4001/same.1", year=2024,
         authors=[{"name": "Nina Zhao", "affils": [ACME_CH]}], scenario="P7_tie_identical"),
    dict(supplier="crossref", paper_id="cr-0011", doi="10.3003/RETRACT.1", year=2024,
         authors=[{"name": "Pat Tang", "affils": [NIM_RETRACTED, NIM_SEC]}],
         scenario="P11_retracted_entry"),
    # ---- P9 坏行（说明原因，不计入）
    dict(supplier="crossref", paper_id="cr-bad1", doi="10.9000/B1", year=2023,
         authors=[], scenario="P9_bad_empty_authors"),
    dict(supplier="crossref", paper_id="cr-bad2", doi="10.9000/B2", year="n/a",
         authors=[{"name": "Zed"}], scenario="P9_bad_year"),
    dict(supplier="crossref", paper_id="cr-bad3", doi="10.9000/B3", year=2023,
         authors="not-a-list", scenario="P9_bad_authors_structure"),
    dict(supplier="crossref", paper_id="cr-bad4", doi="10.9000/B4", year=2023,
         authors=[{"name": "Zed", "affils": "oops"}], scenario="P9_bad_affils_structure"),
    dict(supplier="crossref", paper_id="cr-bad6", doi="10.9000/B6", year=20235,
         authors=[{"name": "Zed"}], scenario="P9_bad_year_range"),
]
# JSON 残缺的坏行（无法解析）
BROKEN_RAW = '{"supplier": "crossref", "paper_id": "cr-bad5", "doi": "10.9000/B5", "year": 2023, "authors": ['


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(_bootstrap.ROOT / "data" / "papers.jsonl"))
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for rec in LINES:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.write(BROKEN_RAW + "\n")
    print(f"已生成 {len(LINES) + 1} 行目录样本（含 6 行坏行）-> {out}")


if __name__ == "__main__":
    main()
