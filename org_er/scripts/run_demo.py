#!/usr/bin/env python3
"""端到端演示：样本生成 -> 批量构建 -> 三轮修订 -> 全部核验 -> 候选统计。

直接运行：python3 scripts/run_demo.py
"""
import json
from pathlib import Path

import _bootstrap  # noqa: F401
import make_sample_data as sample
from org_er import store
from org_er.config import load_config
from org_er.normalize import make_record_key
from org_er.pipeline import rebuild, state_signature
from org_er.reference import check_against_pipeline
from org_er.revisions import apply_events
from org_er.verifylib import (check_config_examples, check_constraint,
                              check_incremental_equivalence,
                              check_shuffle_stability)

ROOT = _bootstrap.ROOT
OUT = ROOT / "output"
DB = OUT / "demo_er.duckdb"

CHECKS = []  # (名称, 是否通过)


def check(name, ok):
    CHECKS.append((name, bool(ok)))
    print(f"      => {'PASS' if ok else 'FAIL'}")


def h(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def key(src, sk, cfg):
    return make_record_key(src, sk, cfg.unicode_form)


def ent_of(con, k, rnd):
    row = con.execute("SELECT entity_id FROM entity_members WHERE round_no=? AND record_key=?",
                      [rnd, k]).fetchone()
    return row[0] if row else None


def members_of(con, eid, rnd):
    return [r[0] for r in con.execute(
        "SELECT record_key FROM entity_members WHERE round_no=? AND entity_id=? ORDER BY record_key",
        [rnd, eid]).fetchall()]


def show_entity(con, k, rnd, label=""):
    eid = ent_of(con, k, rnd)
    ms = members_of(con, eid, rnd) if eid else []
    print(f"      {label}{k}  ->  {eid}  成员={ms}")
    return eid, ms


def show_edge(con, ka, kb, rnd):
    rows = con.execute(
        "SELECT edge_type, score, status, reason, evidence_json FROM edges "
        "WHERE round_no=? AND key_a=? AND key_b=?", [rnd, *sorted((ka, kb))]).fetchall()
    for et, score, status, reason, ev in rows:
        print(f"      边 {ka} ~ {kb}: type={et} score={score} status={status}")
        print(f"          证据={ev}")
        if reason:
            print(f"          拒绝原因={reason}")
    return rows


def show_candidate_note(con, ka, kb, rnd):
    row = con.execute(
        "SELECT via_token, via_regno, via_domain, rule_fired, notes FROM candidates "
        "WHERE round_no=? AND key_a=? AND key_b=?", [rnd, *sorted((ka, kb))]).fetchone()
    if row:
        vt, vr, vd, fired, notes = row
        print(f"      候选 {ka} ~ {kb}: via(token={vt},regno={vr},domain={vd}) "
              f"规则命中={fired or '无'} 说明={notes or '-'}")
    else:
        print(f"      {ka} ~ {kb} 不在候选集中")
    return row


def main():
    cfg = load_config(ROOT / "config" / "er_config.json")
    OUT.mkdir(exist_ok=True)
    if DB.exists():
        DB.unlink()

    h("§0 配置自检（注册号判定规则 + 占位值排除规则，样例驱动）")
    problems = check_config_examples(cfg)
    for p in problems:
        print("   ", p)
    check("配置样例自检", not problems)
    print(f"    相似度算法={cfg.sim_algorithm} 阈值={cfg.sim_threshold}；"
          f"免费邮箱域 {len(cfg.free_email_domains)} 个、共享托管域 {len(cfg.shared_hosting_domains)} 个不构成证据")

    h("§1 样本生成（场景条目 + 填充条目，确定性种子）")
    records = sample.SCENARIO_RECORDS + sample.make_fillers()
    print(f"    共 {len(records)} 条：场景 {len(sample.SCENARIO_RECORDS)} + 填充 {len(records) - len(sample.SCENARIO_RECORDS)}")

    con = store.open_db(DB)
    store.init_db(con)
    for rec in records:
        store.upsert_raw(con, key(rec["source"], rec["source_key"], cfg), rec)
    store.set_meta(con, "current_round", 0)

    h("§2 批量构建（round 0）：三路阻塞 -> 规则评估 -> 约束连通分量")
    r0 = rebuild(con, cfg)
    s = r0.stats
    print(f"    {s['n_records']} 条目 -> {s['n_entities']} 实体；"
          f"候选 {s['candidates_total']} / 全配对 {s['all_pairs']}（节省 {s['candidate_savings_pct']}%）")
    print(f"    规则命中 {s['rule_matched']} 条边：接受 {s['edges_accepted']}，拒绝 {s['edges_rejected']}")

    print("\n  [S1] 跨来源同机构合并")
    k1, k2 = key("companies_house", "GB-ACME-1", cfg), key("opencorporates", "oc/998877", cfg)
    e1, m1 = show_entity(con, k1, 0, "Acme ")
    show_entity(con, k2, 0, "Acme ")
    show_edge(con, k1, k2, 0)
    check("S1a GB 跨来源合并（强边优先，证据并集）", k2 in m1)
    k1, k2 = key("cn_registry", "CN-YUNTU-1", cfg), key("opencorporates", "oc/cn-55", cfg)
    e1b, m1b = show_entity(con, k1, 0, "云图 ")
    show_edge(con, k1, k2, 0)
    check("S1b CN 全角注册号经 Unicode 规范化后合并", k2 in m1b)
    k1, k2 = key("handelsregister", "DE-QUX-1", cfg), key("opencorporates", "oc/de-7", cfg)
    _, m1c = show_entity(con, k1, 0, "Quix ")
    check("S1c DE 跨来源合并", k2 in m1c)

    print("\n  [S2] 免费邮箱域 / 共享托管域不构成证据 -> 不误并")
    k1, k2 = key("dir_a", "A-1001", cfg), key("dir_b", "B-2002", cfg)
    show_candidate_note(con, k1, k2, 0)
    check("S2a gmail.com 被排除，Delta Fitness 两条不并", ent_of(con, k1, 0) != ent_of(con, k2, 0))
    k1, k2 = key("dir_a", "A-1002", cfg), key("dir_b", "B-2003", cfg)
    show_candidate_note(con, k1, k2, 0)
    check("S2b wordpress.com 被排除，同名 Kilo Crafts 不并", ent_of(con, k1, 0) != ent_of(con, k2, 0))

    print("\n  [S3] 相似名称但注册号冲突 -> 弱边被拒并记录原因")
    k1, k2 = key("companies_house", "GB-GLX-1", cfg), key("opencorporates", "oc/glx-2", cfg)
    rows = show_edge(con, k1, k2, 0)
    check("S3 Globex 弱边被拒绝（regno_conflict）",
          rows and rows[0][2] == "rejected" and "regno_conflict" in rows[0][3])
    check("S3 冲突双方保持不同实体", ent_of(con, k1, 0) != ent_of(con, k2, 0))

    print("\n  [S4] 桥接结构（撤回前）：R9--R10 强边，R10--R11 弱边")
    k9, k10, k11 = (key("sec_filings", "US-NIM-1", cfg), key("dir_a", "A-3001", cfg),
                    key("dir_b", "B-3002", cfg))
    _, m4 = show_entity(con, k9, 0, "Nimbus ")
    check("S4 撤回前三者同实体", {k9, k10, k11} <= set(m4))

    print("\n  [S5] 占位注册号（修订前）：无共享证据，各自成实体")
    k12, k13 = key("dir_a", "A-4001", cfg), key("companies_house", "GB-UMB-1", cfg)
    show_candidate_note(con, k12, k13, 0)
    check("S5 修订前 Umbrella Health 两条不并", ent_of(con, k12, 0) != ent_of(con, k13, 0))

    # ---------------- round 1：撤回桥接 ----------------
    h("§3 round 1：证据撤回（撤消桥接条目 dir_a::A-3001）-> 分量分裂")
    apply_events(con, cfg, sample.ROUND1, 1)
    r1 = rebuild(con, cfg)
    e9, e11 = ent_of(con, k9, 1), ent_of(con, k11, 1)
    show_entity(con, k9, 1, "Nimbus ")
    show_entity(con, k11, 1, "Nimbus ")
    print("    迁移映射：")
    for rnd, k, old, new, ct in r1.migrations:
        print(f"      [{ct:8s}] {k}: {old} -> {new}")
    check("S4 桥接撤回后分裂为两个实体", e9 != e11 and k10 not in r1.members)
    check("S4 迁移映射含 split 与 removed",
          any(m[4] == "split" for m in r1.migrations) and any(m[4] == "removed" for m in r1.migrations))

    # ---------------- round 2：注册号修订 ----------------
    h("§4 round 2：注册号修订（N/A -> 44-444-444）-> 候选变化 -> 新强边合并")
    before = con.execute("SELECT COUNT(*) FROM candidates WHERE round_no=1 AND via_regno").fetchone()[0]
    apply_events(con, cfg, sample.ROUND2, 2)
    r2 = rebuild(con, cfg)
    after = con.execute("SELECT COUNT(*) FROM candidates WHERE round_no=2 AND via_regno").fetchone()[0]
    print(f"    注册号索引候选对：round1={before} -> round2={after}（修订使占位值变为合法注册号，进入索引）")
    show_candidate_note(con, k12, k13, 2)
    show_edge(con, k12, k13, 2)
    e12, e13 = ent_of(con, k12, 2), ent_of(con, k13, 2)
    print("    迁移映射：")
    for rnd, k, old, new, ct in r2.migrations:
        print(f"      [{ct:8s}] {k}: {old} -> {new}")
    check("S5 修订后 Umbrella Health 合并", e12 == e13)
    check("S5 注册号候选对增加", after > before)

    # ---------------- round 3：冲突纠错 ----------------
    h("§5 round 3：登记目录纠错（Globex 注册号 33333333 -> 22222222）-> 冲突解除合并")
    apply_events(con, cfg, sample.ROUND3, 3)
    r3 = rebuild(con, cfg)
    k1, k2 = key("companies_house", "GB-GLX-1", cfg), key("opencorporates", "oc/glx-2", cfg)
    show_edge(con, k1, k2, 3)
    print("    迁移映射：")
    for rnd, k, old, new, ct in r3.migrations:
        print(f"      [{ct:8s}] {k}: {old} -> {new}")
    check("S3 纠错后 Globex 合并（强边）", ent_of(con, k1, 3) == ent_of(con, k2, 3))

    # ---------------- 核验 ----------------
    h("§6 核验")
    rep = check_against_pipeline(r3.records, cfg, r3.candidates, r3.edges)
    print(f"    [穷举参考] N={rep['n_records']} 全配对={rep['all_pairs']} 候选={rep['n_candidates']} "
          f"规则可接受边={rep['n_reference_pairs']}；候选遗漏={len(rep['missing_in_candidates'])} "
          f"判定不一致={len(rep['mismatched']) + len(rep['extra_in_pipeline']) + len(rep['not_evaluated'])}")
    check("穷举参考：候选不漏规则可接受的边，且判定一致", rep["ok"])

    violations = check_constraint(r3.members, r3.records)
    check("约束不变量：同实体同国家至多一个有效注册号", not violations)

    raw_rows = store.read_raw_rows(con)
    shuf = check_shuffle_stability(raw_rows, cfg)
    print(f"    [乱序稳定] seeds={shuf['seeds']} 结果签名一致")
    check("S6 输入顺序打乱结果稳定", shuf["ok"])

    eq = check_incremental_equivalence(raw_rows, r3, cfg)
    check("增量（三轮修订）== 全部当前条目全量重算", eq["ok"])

    # ---------------- 候选统计 ----------------
    h("§7 候选规模与节省（相较全配对）")
    rows = con.execute(
        "SELECT round_no, k, v FROM run_stats WHERE k IN "
        "('n_records','all_pairs','candidates_total','cand_via_token','cand_via_regno',"
        "'cand_via_domain','rule_matched','edges_accepted','edges_rejected','candidate_savings_pct') "
        "ORDER BY round_no, k").fetchall()
    per = {}
    for rnd, k, v in rows:
        per.setdefault(rnd, {})[k] = v
    hdr = ("round", "条目", "全配对", "候选合计", "token", "注册号", "域名", "规则命中", "接受", "拒绝", "节省%")
    print("    " + " | ".join(f"{x:>8s}" for x in hdr))
    for rnd in sorted(per):
        d = per[rnd]
        vals = (rnd, d.get("n_records"), d.get("all_pairs"), d.get("candidates_total"),
                d.get("cand_via_token"), d.get("cand_via_regno"), d.get("cand_via_domain"),
                d.get("rule_matched"), d.get("edges_accepted"), d.get("edges_rejected"),
                d.get("candidate_savings_pct"))
        print("    " + " | ".join(f"{str(x):>8s}" for x in vals))
    print("""
    节省来源：
    1) 候选只来自三个等值索引（国家+token 倒排 / 合法注册号 / 域名），不做 N(N-1)/2 全配对；
    2) token 阻塞限定同国家，跨国家条目互不产生候选；
    3) 注册号索引只含合法值（占位值、非法格式被排除），域名索引只含可作证据的域名
       （免费邮箱、共享托管域被排除），无效键不产生候选；
    4) 规则可接受边必然落在注册号或域名索引内（已由穷举参考程序核验），
       因此候选集合是规则可接受边的超集，数量远小于全配对。""")

    store.export_outputs(con, OUT, 3)
    print(f"\n交付表已导出：{OUT}/（entity_members.csv / match_edges.csv / rejected_edges.csv / "
          f"candidates.csv / entity_migrations.csv / run_stats.csv）")

    con.close()

    h("总结")
    n_ok = sum(1 for _, ok in CHECKS if ok)
    for name, ok in CHECKS:
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n    {n_ok}/{len(CHECKS)} 项通过")
    if n_ok != len(CHECKS):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
