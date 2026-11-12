#!/usr/bin/env python3
"""论文署名贡献核算作业：端到端演示（自建独立机构库，含全部场景与核验）。

直接运行：python3 scripts/run_contrib_demo.py

流程：
  §0 自建机构解析库（round 0-3，复用 org_er 样本与修订），作业只读引用
  §1 生成论文目录样本
  §2 运行核算作业（指定机构库 + 轮次 + 供应方优先级）
  §3 场景逐项核对（去重/分摊/隔离/坏行）
  §4 机构×年份汇总与明细解释
  §5 独立核验（份额守恒、汇总可追、隔离不计、行归属）
  §6 机构库保持原样（前后快照一致）
  §7 乱序读入结果一致
  §8 轮次敏感性：同一目录在 round 0 与 round 3 的差异（只读对照的意义）
"""
import json
import random
import shutil
import tempfile
from fractions import Fraction
from pathlib import Path

import _bootstrap  # noqa: F401
import make_contrib_sample as contrib_sample
import make_sample_data as er_sample
from org_er import store as er_store
from org_er.config import load_config
from org_er.contrib import store as cstore
from org_er.contrib.job import run_job
from org_er.contrib.verify import er_snapshot, run_signature, verify_run
from org_er.normalize import make_record_key
from org_er.pipeline import rebuild
from org_er.revisions import apply_events

ROOT = _bootstrap.ROOT
OUT = ROOT / "output"
ER_DB = OUT / "demo_contrib_er.duckdb"
CONTRIB_DB = OUT / "demo_contrib.duckdb"
CSV_DIR = OUT / "contrib"
CATALOG = ROOT / "data" / "papers.jsonl"
PRIORITY = ["crossref", "scopus", "local_ir"]

CHECKS = []


def check(name, ok):
    CHECKS.append((name, bool(ok)))
    print(f"      => {'PASS' if ok else 'FAIL'}")


def h(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def build_er_db(cfg):
    """自建机构解析库（round 0-3），返回连接。作业对它只读。"""
    if ER_DB.exists():
        ER_DB.unlink()
    con = er_store.open_db(ER_DB)
    er_store.init_db(con)
    for rec in er_sample.SCENARIO_RECORDS + er_sample.make_fillers():
        key = make_record_key(rec["source"], rec["source_key"], cfg.unicode_form)
        er_store.upsert_raw(con, key, rec)
    er_store.set_meta(con, "current_round", 0)
    rebuild(con, cfg)
    for rnd, events in enumerate((er_sample.ROUND1, er_sample.ROUND2, er_sample.ROUND3), start=1):
        apply_events(con, cfg, events, rnd)
        rebuild(con, cfg)
    return con


def ent_of(con, source, skey, rnd, cfg):
    key = make_record_key(source, skey, cfg.unicode_form)
    row = con.execute("SELECT entity_id FROM entity_members WHERE round_no=? AND record_key=?",
                      [rnd, key]).fetchone()
    return row[0] if row else None


def frac_texts(con, group_key):
    """某篇计入论文的全部分摊行：[(target_kind, entity_id, Fraction)]。"""
    rows = con.execute(
        "SELECT target_kind, entity_id, share_num, share_den FROM contrib_share_targets "
        "WHERE group_key = ? ORDER BY target_kind, entity_id", [group_key]).fetchall()
    return [(k, e, Fraction(n, d)) for k, e, n, d in rows]


def main():
    cfg = load_config(ROOT / "config" / "er_config.json")
    OUT.mkdir(exist_ok=True)

    h("§0 机构解析库（只读对照）：round 0-3 构建完成即封存，核算作业不改动它")
    er_con = build_er_db(cfg)
    n_ent = er_con.execute(
        "SELECT COUNT(DISTINCT entity_id) FROM entity_members WHERE round_no=3").fetchone()[0]
    print(f"    机构库 {ER_DB.name}：round 3 共 {n_ent} 个实体（历史轮次 0-3 全保留）")
    E_ACME = ent_of(er_con, "companies_house", "GB-ACME-1", 3, cfg)
    E_YUNTU = ent_of(er_con, "cn_registry", "CN-YUNTU-1", 3, cfg)
    E_QUIX = ent_of(er_con, "handelsregister", "DE-QUX-1", 3, cfg)
    E_UMB = ent_of(er_con, "dir_a", "A-4001", 3, cfg)
    E_NIM_SEC = ent_of(er_con, "sec_filings", "US-NIM-1", 3, cfg)
    E_NIM0 = ent_of(er_con, "dir_a", "A-3001", 0, cfg)
    print(f"    round 3 实体：Acme={E_ACME} 云图={E_YUNTU} Quixotic={E_QUIX}")
    print(f"                  Umbrella={E_UMB} Nimbus(sec)={E_NIM_SEC}")
    print(f"    round 0 实体：Nimbus(含已撤回条目 A-3001)={E_NIM0}")
    snap_before = er_snapshot(er_con)
    er_con.close()

    h("§1 论文目录样本（data/papers.jsonl）")
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CATALOG, "w", encoding="utf-8") as f:
        for rec in contrib_sample.LINES:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.write(contrib_sample.BROKEN_RAW + "\n")
    print(f"    已生成 {len(contrib_sample.LINES) + 1} 行（含 6 行坏行）-> {CATALOG}")

    h("§2 运行核算作业：--er-db 指定机构库，--round 3，--priority " + ",".join(PRIORITY))
    if CONTRIB_DB.exists():
        CONTRIB_DB.unlink()
    summary, con = run_job(er_db=str(ER_DB), round_no=3, catalog=str(CATALOG),
                           priority=PRIORITY, out_db=str(CONTRIB_DB), out_dir=str(CSV_DIR))
    print(f"    目录行 {summary.n_lines}：有效 {summary.n_ok}，坏行 {summary.n_rejected} "
          f"{dict(sorted(summary.reject_reasons.items()))}")
    print(f"    去重分组 {summary.n_groups}：计入 {summary.n_counted}，隔离 {summary.n_quarantined}；"
          f"作者分摊明细 {summary.n_author_rows} 行")

    def ginfo(gkey):
        return con.execute(
            "SELECT status, n_versions, selected_supplier, selected_paper_id, selected_year, "
            "select_reason, version_diffs_json FROM contrib_groups WHERE group_key=?",
            [gkey]).fetchone()

    h("§3 场景逐项核对")

    print("  [P1] 跨供应方重复收录：DOI 链接前缀/大小写/空白差异归并为一篇")
    st, nv, sup, pid, yr, reason, diffs = ginfo("doi::10.1000/xyz.123")
    diffs = json.loads(diffs)
    print(f"      2 版本归并 -> 选中 {sup}::{pid}（{yr}），理由 {reason}")
    for d in diffs:
        print(f"        版本 {d['supplier']}::{d['paper_id']} year={d['year']} diffs={d['diffs']}")
    check("P1 归并为一篇并按优先级选 crossref 版",
          st == "counted" and nv == 2 and sup == "crossref" and yr == 2023
          and reason == "unique_best_priority")
    check("P1 年份冲突留痕（year_differs）",
          any("year_differs" in d["diffs"] for d in diffs))
    shares = dict(((k, e), f) for k, e, f in frac_texts(con, "doi::10.1000/xyz.123"))
    print(f"      分摊：{ {f'{k}:{e}': str(v) for (k, e), v in shares.items()} }")
    check("P1 精确分摊：Acme 1/6、云图 1/6、Quixotic 1/3、待归属 1/3",
          shares.get(("entity", E_ACME)) == Fraction(1, 6)
          and shares.get(("entity", E_YUNTU)) == Fraction(1, 6)
          and shares.get(("entity", E_QUIX)) == Fraction(1, 3)
          and shares.get(("unassigned", "")) == Fraction(1, 3))

    print("  [P2] 作者顺序冲突：仍按优先级选取，差异留痕")
    st, nv, sup, pid, yr, reason, diffs = ginfo("doi::10.2000/aaa.1")
    diffs = json.loads(diffs)
    print(f"      选中 {sup}::{pid}，版本差异：{[d['diffs'] for d in diffs]}")
    check("P2 按优先级选取且记录 author_order_differs",
          st == "counted" and sup == "crossref"
          and any("author_order_differs" in d["diffs"] for d in diffs))

    print("  [P3] 同一机构多条署名：先去重再分摊")
    row = con.execute(
        "SELECT located_entities_json, n_distinct_located, division_basis FROM contrib_authors "
        "WHERE group_key='doi::10.3000/dedup.1' AND author_pos=1").fetchone()
    print(f"      Frank 的 2 条署名 -> {row[0]}（去重后 {row[1]} 个）；{row[2]}")
    check("P3 两条署名去重为同一实体，独得 1/2",
          json.loads(row[0]) == [E_ACME] and row[1] == 1)

    print("  [P4] 部分署名未定位：只在已定位机构间分配，未定位引用保留")
    row = con.execute(
        "SELECT located_entities_json, unlocated_refs_json FROM contrib_authors "
        "WHERE group_key='doi::10.3001/part.1' AND author_pos=1").fetchone()
    unloc = json.loads(row[1])
    print(f"      Henry 定位到 {row[0]}，未定位保留 {unloc}")
    check("P4 已定位机构独得份额，未定位引用留痕",
          json.loads(row[0]) == [E_ACME] and len(unloc) == 1
          and unloc[0]["record_key"] == "dir_x::UNKNOWN-9")

    print("  [P5] 署名全部未定位 / 无署名：整份记入待归属")
    rows = con.execute(
        "SELECT author_name, disposition, division_basis FROM contrib_authors "
        "WHERE group_key='doi::10.3002/none.1' ORDER BY author_pos").fetchall()
    for name, disp, basis in rows:
        print(f"      {name}: {disp}（{basis}）")
    check("P5 两位作者整份待归属",
          [r[1] for r in rows] == ["unassigned_none_located", "unassigned_no_affils"])

    print("  [P6] 优先级打平且作者名单不同：整篇隔离，绝不按输入顺序挑一个")
    st, nv, sup, pid, yr, reason, diffs = ginfo("doi::10.4000/conf.1")
    n_rows = con.execute("SELECT COUNT(*) FROM contrib_authors WHERE group_key='doi::10.4000/conf.1'").fetchone()[0]
    print(f"      wos 与 pubmed 并列最低优先级且内容不同 -> {reason}；分摊行 {n_rows}")
    check("P6 隔离且零分摊", st == "quarantined" and sup is None and n_rows == 0
          and reason == "quarantine:tie_conflicting_content")

    print("  [P7] 优先级打平但内容一致：计入（确定序取一，与输入顺序无关）")
    st, nv, sup, pid, yr, reason, diffs = ginfo("doi::10.4001/same.1")
    print(f"      选中 {sup}::{pid}，理由 {reason}")
    check("P7 打平但内容一致 -> 计入", st == "counted" and reason == "tie_identical_content"
          and sup == "pubmed")

    print("  [P8] 无 DOI：按供应方+论文号独立保留，互不归并")
    rows = con.execute(
        "SELECT group_key, status, n_versions, selected_supplier FROM contrib_groups "
        "WHERE group_kind='noid' ORDER BY group_key").fetchall()
    for gk, st, nv, sup in rows:
        print(f"      {gk}: {st}，版本 {nv}，选中 {sup}")
    check("P8 两个无 DOI 分组各自计入（同供应方重复已归并）",
          len(rows) == 2 and all(r[1] == "counted" for r in rows)
          and dict((r[0], r[2]) for r in rows)["noid::local_ir::ir-0001"] == 2)

    print("  [P9] 坏行：说明原因，不算作正常论文")
    rows = con.execute(
        "SELECT paper_id, reject_reason FROM contrib_lines WHERE status='rejected' "
        "ORDER BY line_no").fetchall()
    for pid, reason in rows:
        print(f"      {pid or '(JSON 残缺)'}: {reason}")
    check("P9 六类坏行全部说明原因",
          len(rows) == 6
          and {r[1].split(":")[0] for r in rows}
              == {"empty_authors", "illegal_year", "broken_author_structure", "json_parse_error"})

    print("  [P11] 署名指向已撤回条目（round 1 撤回）：该引用未定位")
    row = con.execute(
        "SELECT located_entities_json, unlocated_refs_json FROM contrib_authors "
        "WHERE group_key='doi::10.3003/retract.1'").fetchone()
    print(f"      Pat 定位到 {row[0]}，未定位 {json.loads(row[1])}")
    check("P11 已撤回条目未定位、其余正常定位",
          json.loads(row[0]) == [E_NIM_SEC]
          and json.loads(row[1])[0]["record_key"] == "dir_a::A-3001")

    h("§4 机构×年份汇总：每个汇总值都能用明细解释")
    rows = con.execute(
        "SELECT entity_id, year, total_num, total_den, n_shares, n_papers "
        "FROM contrib_institution_year ORDER BY year, entity_id").fetchall()
    expect = {(E_ACME, 2023): Fraction(2, 3), (E_YUNTU, 2023): Fraction(8, 3),
              (E_QUIX, 2023): Fraction(1, 3),
              (E_ACME, 2024): Fraction(2), (E_YUNTU, 2024): Fraction(1, 2),
              (E_UMB, 2024): Fraction(1, 2), (E_NIM_SEC, 2024): Fraction(1)}
    got = {}
    for eid, year, num, den, ns, np_ in rows:
        got[(eid, year)] = Fraction(num, den)
        print(f"    {year}  {eid}  贡献 {num}/{den} = {num / den:.4f}"
              f"（{ns} 笔作者份额，来自 {np_} 篇）")
        detail = con.execute(
            "SELECT t.group_key, t.author_pos, t.share_num, t.share_den "
            "FROM contrib_share_targets t JOIN contrib_authors a USING (group_key, author_pos) "
            "WHERE t.entity_id=? AND a.year=? ORDER BY t.group_key, t.author_pos",
            [eid, year]).fetchall()
        for gk, pos, n2, d2 in detail:
            print(f"        <- {gk} 第{pos}位作者 {n2}/{d2}")
    unas = con.execute("SELECT year, total_num, total_den, n_authors FROM contrib_unassigned_year").fetchall()
    for year, num, den, na in unas:
        print(f"    {year}  待归属  {num}/{den}（{na} 位作者整份）")
    check("机构×年份汇总与手工推算完全一致", got == expect)
    check("待归属 2023 = 4/3（P1 的 1/3 + P5 的 1）",
          {(y): Fraction(n, d) for y, n, d, _ in unas} == {2023: Fraction(4, 3)})

    h("§5 独立核验（不复用核算代码路径）")
    checks_rep = verify_run(con)
    for name, c in checks_rep.items():
        if name == "ok":
            continue
        print(f"    [{'PASS' if c['ok'] else 'FAIL'}] {name}: {c['detail']}")
    check("独立核验全部通过", checks_rep["ok"])
    base_sig = run_signature(con)   # 供 §7 乱序比对
    con.close()                     # 释放作业库与只读挂载，便于独立重开机构库

    h("§6 机构库保持原样（作业前后快照逐表比对）")
    er_con2 = er_store.open_db(ER_DB)
    snap_after = er_snapshot(er_con2)
    er_con2.close()
    check("机构库全部表（含历史轮次）作业前后一致", snap_before == snap_after)

    h("§7 乱序读入：3 个随机种子重放，核算结果签名一致")
    raw_lines = CATALOG.read_text(encoding="utf-8").splitlines(keepends=True)
    shuffle_ok = True
    tmpdir = Path(tempfile.mkdtemp(prefix="contrib_shuffle_"))
    try:
        for seed in (1, 2, 3):
            shuffled = list(raw_lines)
            random.Random(seed).shuffle(shuffled)
            tmp_catalog = tmpdir / f"papers_s{seed}.jsonl"
            tmp_catalog.write_text("".join(shuffled), encoding="utf-8")
            _, tmp_con = run_job(er_db=str(ER_DB), round_no=3, catalog=str(tmp_catalog),
                                 priority=PRIORITY, out_db=":memory:")
            same = run_signature(tmp_con) == base_sig
            shuffle_ok &= same
            print(f"    seed={seed}: {'一致' if same else '不一致'}")
            tmp_con.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    check("乱序读入结果一致", shuffle_ok)

    h("§8 轮次敏感性：同一目录在 round 0 与 round 3 的对照（归并结果只读引用）")
    _, con0 = run_job(er_db=str(ER_DB), round_no=0, catalog=str(CATALOG),
                      priority=PRIORITY, out_db=":memory:")
    row0 = con0.execute(
        "SELECT located_entities_json, unlocated_refs_json FROM contrib_authors "
        "WHERE group_key='doi::10.3003/retract.1'").fetchone()
    print(f"    P11 同一署名 [dir_a::A-3001, sec_filings::US-NIM-1]：")
    print(f"      round 0：定位到 {row0[0]}（A-3001 尚未撤回，与 US-NIM-1 同实体，去重为 1 个）")
    print(f"      round 3：定位到 ['{E_NIM_SEC}']，A-3001 已撤回 -> 未定位保留核查")
    check("轮次决定定位结果（round 0 两引用同实体去重；round 3 一引用已撤回未定位）",
          json.loads(row0[0]) == [E_NIM0] and json.loads(row0[1]) == [] and E_NIM0 != E_NIM_SEC)
    con0.close()

    h("总结")
    n_ok = sum(1 for _, ok in CHECKS if ok)
    for name, ok in CHECKS:
        print(f"    [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n    {n_ok}/{len(CHECKS)} 项通过")
    print(f"    交付表：{CSV_DIR}/（contrib_author_details / contrib_institution_year / "
          f"contrib_unassigned_year / contrib_papers / contrib_paper_versions / "
          f"contrib_quarantine / contrib_rejected_lines）")
    if n_ok != len(CHECKS):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
