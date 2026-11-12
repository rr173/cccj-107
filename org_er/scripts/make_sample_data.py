#!/usr/bin/env python3
"""样本生成入口：生成演示所需的原始条目与三轮修订事件（确定性，固定种子）。

场景覆盖：
  S1  跨来源同机构合并（GB 强边+弱边双证据；CN 全角注册号 Unicode 规范化；DE 仅强边）
  S2  免费邮箱域 / 共享托管域不构成证据 -> 不误并
  S3  相似名称但注册号冲突 -> 弱边被拒
  S4  桥接边撤回 -> 分量分裂
  S5  注册号修订 -> 候选变化 -> 合并
  S6  输入顺序打乱 -> 结果稳定（由 verify 验证）
"""
import argparse
import json
import random
from pathlib import Path

import _bootstrap  # noqa: F401

SCENARIO_RECORDS = [
    # ---- S1a 跨来源合并：同国同有效注册号（强边），且域名+名称相似（弱边），强边优先、证据并集
    dict(source="companies_house", source_key="GB-ACME-1", legal_name="Acme Corporation",
         country_code="gb", website_host="www.acme-corp.com", registration_no="12345678",
         scenario="S1_cross_source_merge"),
    dict(source="opencorporates", source_key="oc/998877", legal_name="ACME Corp.",
         country_code="GB", website_host="acme-corp.com", registration_no="12-345-678",
         scenario="S1_cross_source_merge"),
    # ---- S1b 跨来源合并：中文名 + 全角注册号（NFKC 规范化后相同）
    dict(source="cn_registry", source_key="CN-YUNTU-1", legal_name="北京云图科技有限公司",
         country_code="cn", website_host="yuntu-example.cn", registration_no="91110000MA01C8XW3T",
         scenario="S1_unicode_merge"),
    dict(source="opencorporates", source_key="oc/cn-55", legal_name="北京云图科技",
         country_code="CN", website_host="", registration_no="９１１１００００ＭＡ０１Ｃ８ＸＷ３Ｔ",
         scenario="S1_unicode_merge"),
    # ---- S1c 跨来源合并：DE，仅注册号强边（名称相同但域名不同）
    dict(source="handelsregister", source_key="DE-QUX-1", legal_name="Quixotic GmbH",
         country_code="DE", website_host="quixotic.de", registration_no="HRB 12345",
         scenario="S1_de_merge"),
    dict(source="opencorporates", source_key="oc/de-7", legal_name="Quixotic GmbH",
         country_code="de", website_host="www.quixotic.de", registration_no="HRB12345",
         scenario="S1_de_merge"),
    # ---- S2 免费邮箱域 / 共享托管域不构成证据：名称相似甚至相同也不并
    dict(source="dir_a", source_key="A-1001", legal_name="Delta Fitness",
         country_code="GB", website_host="gmail.com", registration_no="N/A",
         scenario="S2_excluded_domain"),
    dict(source="dir_b", source_key="B-2002", legal_name="Delta Fitness Studio",
         country_code="GB", website_host="gmail.com", registration_no="",
         scenario="S2_excluded_domain"),
    dict(source="dir_a", source_key="A-1002", legal_name="Kilo Crafts",
         country_code="US", website_host="wordpress.com", registration_no="N/A",
         scenario="S2_excluded_domain"),
    dict(source="dir_b", source_key="B-2003", legal_name="Kilo Crafts",
         country_code="US", website_host="wordpress.com", registration_no="TBD",
         scenario="S2_excluded_domain"),
    # ---- S3 相似名称 + 同域名，但注册号冲突 -> 弱边被拒
    dict(source="companies_house", source_key="GB-GLX-1", legal_name="Globex Ltd",
         country_code="GB", website_host="globex.co.uk", registration_no="22222222",
         scenario="S3_regno_conflict"),
    dict(source="opencorporates", source_key="oc/glx-2", legal_name="Globex Limited",
         country_code="GB", website_host="globex.co.uk", registration_no="33333333",
         scenario="S3_regno_conflict"),
    # ---- S4 桥接：R9--R10 强边（注册号），R10--R11 弱边（域名）；撤回 R10 后分裂
    dict(source="sec_filings", source_key="US-NIM-1", legal_name="Nimbus Analytics",
         country_code="US", website_host="", registration_no="123456789",
         scenario="S4_bridge"),
    dict(source="dir_a", source_key="A-3001", legal_name="Nimbus Analytics",
         country_code="US", website_host="nimbus-analytics.com", registration_no="123456789",
         scenario="S4_bridge"),
    dict(source="dir_b", source_key="B-3002", legal_name="Nimbus Analytics",
         country_code="US", website_host="nimbus-analytics.com", registration_no="N/A",
         scenario="S4_bridge"),
    # ---- S5 注册号修订：A-4001 初始为占位值，修订后获得合法注册号 -> 新强边
    dict(source="dir_a", source_key="A-4001", legal_name="Umbrella Health",
         country_code="GB", website_host="", registration_no="N/A",
         scenario="S5_regno_revision"),
    dict(source="companies_house", source_key="GB-UMB-1", legal_name="Umbrella Health",
         country_code="GB", website_host="", registration_no="44444444",
         scenario="S5_regno_revision"),
]

# 三轮修订事件
ROUND1 = [  # 撤回桥接条目 -> S4 分量分裂
    {"op": "retract", "source": "dir_a", "source_key": "A-3001"},
]
ROUND2 = [  # 修订占位注册号为合法值 -> S5 出现新候选、新强边
    {"op": "upsert", "record": dict(source="dir_a", source_key="A-4001",
                                    legal_name="Umbrella Health", country_code="GB",
                                    website_host="", registration_no="44-444-444",
                                    scenario="S5_regno_revision")},
]
ROUND3 = [  # 登记目录纠错：Globex 注册号更正 -> 冲突解除，合并
    {"op": "upsert", "record": dict(source="opencorporates", source_key="oc/glx-2",
                                    legal_name="Globex Limited", country_code="GB",
                                    website_host="globex.co.uk", registration_no="22222222",
                                    scenario="S3_regno_conflict")},
]

# 填充条目：制造 token 块规模，用于展示候选节省；注册号唯一、域名唯一或为空，不会误并
_FIRST = ["Global", "Prime", "United", "Apex", "Summit", "Pioneer", "Vertex", "Zenith", "Nova", "Orion"]
_SECOND = ["Solutions", "Systems", "Logistics", "Partners", "Industries", "Ventures", "Labs", "Works", "Group", "Trading"]
_LEGAL = {"US": "Inc", "GB": "Ltd", "DE": "GmbH"}
_CN1 = ["华", "辰", "恒", "瑞", "锦", "宏", "博", "盛", "嘉", "晟"]
_CN2 = ["科技", "贸易", "物流", "咨询", "实业"]


def make_fillers(n=48, seed=20261112):
    rng = random.Random(seed)
    countries = ["US", "GB", "DE", "CN"]
    fillers = []
    for i in range(n):
        c = countries[i % len(countries)]
        if c == "CN":
            name = f"{rng.choice(_CN1)}{rng.choice(_CN1)}{rng.choice(_CN2)}有限公司"
            regno = f"91{i:016d}"
        elif c == "US":
            name = f"{rng.choice(_FIRST)} {rng.choice(_SECOND)} Inc"
            regno = f"8{i:08d}"
        elif c == "GB":
            name = f"{rng.choice(_FIRST)} {rng.choice(_SECOND)} Ltd"
            regno = f"{10_000_000 + i}"
        else:
            name = f"{rng.choice(_FIRST)} {rng.choice(_SECOND)} GmbH"
            regno = f"HRB{90_000 + i}"
        host = f"fill{i}.example.com" if rng.random() < 0.2 else ""
        fillers.append(dict(source=f"cat_{c.lower()}", source_key=f"F-{i:03d}",
                            legal_name=name, country_code=c, website_host=host,
                            registration_no=regno, scenario="filler"))
    return fillers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(_bootstrap.ROOT / "data"))
    ap.add_argument("--fillers", type=int, default=48)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    records = SCENARIO_RECORDS + make_fillers(args.fillers)
    with open(out / "records.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for i, events in enumerate((ROUND1, ROUND2, ROUND3), start=1):
        with open(out / f"revisions_round{i}.jsonl", "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    print(f"已生成 {len(records)} 条原始条目（场景 {len(SCENARIO_RECORDS)} + 填充 {args.fillers}）")
    print(f"修订事件：revisions_round1.jsonl（撤回桥接）/ round2（注册号修订）/ round3（冲突纠错）")
    print(f"输出目录：{out}")


if __name__ == "__main__":
    main()
