#!/usr/bin/env python3
"""贡献核算故障链的复现与回归演示（自建独立库，全部场景实际执行）。

直接运行：python3 scripts/run_contrib_regression.py

覆盖：
  §1 素数 n=2..101 目录：只有首位作者署名为 d::one，年度分数超过 int128，
     完整保存、精确重建、导出分子分母核对（不是“恰好能跑通”）
  §2 独立重算：另外构造不同作者数 + 多机构分配 + 重复署名 + 隔离论文 + 坏行的目录，
     不复用核算路径，按作者明细独立重算机构年度与待归属，并核对导出 CSV
  §3 旧版 HUGEINT 小账目：识别 -> 独立核验 -> 无损转换 -> 重新导出（无法转换的库不改写）
  §4 一次成功后的“失败重跑”：同一输出位置先有成功结果，再跑会失败的批次；
     确认保住的是哪一份、半成品如何辨认、重启后如何恢复
  §5 写明细中途 / 导出中途进程终止后重启恢复（含数据库已切换、CSV 未发布的窗口）
  §6 机构库只读、历史不动

默认把作业库与交付表放在临时目录（可用 --keep DIR 保留以便查看）。
"""
import argparse
import csv
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

import _bootstrap  # noqa: F401
from org_er.contrib import store as cstore
from org_er.contrib.job import recover_pending, reuse_existing, run_job, verify_export
from org_er.contrib.verify import run_signature, verify_run

ROOT = _bootstrap.ROOT
CHECKS = []


def check(name, ok, note=""):
    CHECKS.append((name, bool(ok)))
    print(f"      => {'PASS' if ok else 'FAIL'}  [{name}]{'  ' + note if note else ''}")


def h(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def primes_between(a, b):
    return [n for n in range(a, b + 1)
            if n >= 2 and all(n % d for d in range(2, int(n ** 0.5) + 1))]


def write_catalog(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for rec in lines:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def csv_exact_totals(csvdir):
    """从导出的 CSV 读精确分数（整数列），绝不经过浮点。"""
    inst = {(r["entity_id"], int(r["year"])): Fraction(int(r["total_num"]), int(r["total_den"]))
            for r in read_csv_rows(Path(csvdir) / "contrib_institution_year.csv")}
    unas = {int(r["year"]): Fraction(int(r["total_num"]), int(r["total_den"]))
            for r in read_csv_rows(Path(csvdir) / "contrib_unassigned_year.csv")}
    return inst, unas


def make_er_db(path, members):
    """最小机构库：entity_members(round_no, entity_id, record_key)。"""
    import duckdb
    if Path(path).exists():
        Path(path).unlink()
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE entity_members (round_no INTEGER, entity_id TEXT, record_key TEXT)")
    con.executemany("INSERT INTO entity_members VALUES (?, ?, ?)", members)
    con.execute("CREATE TABLE archive (x TEXT)")
    con.execute("INSERT INTO archive VALUES ('历史不得改写')")
    con.close()


def er_content_signature(path):
    import duckdb
    con = duckdb.connect(str(path), read_only=True)
    try:
        return (con.execute("SELECT * FROM entity_members ORDER BY 1,2,3").fetchall(),
                con.execute("SELECT * FROM archive").fetchall())
    finally:
        con.close()


def current_file_signature(path):
    if not Path(path).exists():
        return None
    hsh = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            hsh.update(chunk)
    return hsh.hexdigest()


def csv_dir_signature(csvdir):
    out = {}
    for p in sorted(Path(csvdir).glob("*")):
        if p.is_file() and p.name != cstore.MANIFEST_FILE:
            out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def csv_matches_any_db_batch(con, out_dir):
    """导出的 CSV 是否与数据库中“某一批次”整体一致（而不是与新库/半成品混合）。"""
    manifest = cstore.read_manifest(out_dir)
    if not manifest:
        return False, "缺 manifest"
    cur = cstore.get_meta(con, "batch_id")
    if manifest.get("batch_id") != cur:
        return False, f"manifest 批次 {manifest.get('batch_id')} != 当前 {cur}"
    exp = verify_export(con, out_dir)
    return exp["ok"], exp["problems"][:2]


def bank_signature(con):
    return run_signature(con)


def scenario_paths(base):
    b = Path(base)
    return {
        "prime_catalog": b / "prime_papers.jsonl",
        "multi_catalog": b / "multi_papers.jsonl",
        "prime_er": b / "prime_er.duckdb",
        "multi_er": b / "multi_er.duckdb",
        "prime_db": b / "prime_contrib.duckdb",
        "prime_csv": b / "prime_csv",
        "multi_db": b / "multi_contrib.duckdb",
        "multi_csv": b / "multi_csv",
        "legacy_live": b / "legacy_live.duckdb",
        "legacy_live_csv": b / "legacy_live_csv",
        "legacy_bad": b / "legacy_bad.duckdb",
        "legacy_bad_csv": b / "legacy_bad_csv",
        "rerun_db": b / "rerun.duckdb",
        "rerun_csv": b / "rerun_csv",
    }


# ---------------------------------------------------------------- 目录构造
def build_prime_catalog(p):
    p = primes_between(2, 101)
    lines = []
    for n in p:
        authors = [{"name": f"A{n}-{i}"} for i in range(1, n + 1)]
        authors[0]["affils"] = [{"source": "d", "entry": "one"}]
        lines.append({"supplier": "crossref", "paper_id": f"pr-{n}", "doi": f"10.7000/prime.{n}",
                      "year": 2024, "authors": authors})
    write_catalog(pth, lines) if False else None
    return lines, p


def build_multi_catalog(entities):
    """不同作者数 + 多机构分配 + 重复署名 + 隔离 + 坏行。"""
    E = entities

    def A(name, *refs):
        """refs 可以是 (source, entry) 对，也可以直接是 role 名（= entities 里的机构）。"""
        a = {"name": name}
        if refs:
            a["affils"] = [{"source": r[0], "entry": r[1]}
                           if isinstance(r, tuple) else
                           {"source": "org", "entry": f"e:{r}"}
                           for r in refs]
        return a

    def ref(eid_role):
        return ("org", f"e:{eid_role}")

    members = [(1, entities[r], f"org::e:{r}") for r in entities]
    return _multi_lines(A, ref), members


def _multi_lines(A, ref):
    lines = [
        # 3 作者：第一位 3 机构（含同一机构重复署名）、第二位 2 机构、第三位无署名
        {"supplier": "crossref", "paper_id": "m-1", "doi": "10.9000/m.1", "year": 2024,
         "authors": [A("M1", ref("B"), ref("C"), ref("B"), ref("A")),
                     A("M2", ref("C"), ref("D")), A("M3")]},
        # 1 作者、4 个不同机构
        {"supplier": "crossref", "paper_id": "m-2", "doi": "10.9000/m.2", "year": 2023,
         "authors": [A("S1", ref("A"), ref("B"), ref("C"), ref("D"))]},
        # 5 作者：部分署名未定位
        {"supplier": "scopus", "paper_id": "m-3", "doi": "10.9000/m.3", "year": 2024,
         "authors": [A("P1", ref("A"), ("ghost", "x-1")), A("P2", ("ghost", "x-2")),
                     A("P3", ref("E")), A("P4", ref("E"), ref("E")), A("P5")]},
        # 2 版本同 DOI，优先级打平且内容不同 -> 隔离（零贡献）
        {"supplier": "wos", "paper_id": "m-4", "doi": "10.9000/m.4", "year": 2024,
         "authors": [A("Q1", ref("A"))]},
        {"supplier": "pubmed", "paper_id": "m-5", "doi": "10.9000/m.4", "year": 2024,
         "authors": [A("Q2", ref("B")), A("Q3")]},
        # 无 DOI 同供应方同号重复且内容一致 -> 归并
        {"supplier": "local_ir", "paper_id": "m-6", "doi": "", "year": 2022,
         "authors": [A("R1", ref("D")), A("R2", ref("D"))]},
        {"supplier": "local_ir", "paper_id": "m-6", "doi": None, "year": 2022,
         "authors": [A("R1", ref("D")), A("R2", ref("D"))]},
        # 7 作者，署名为空
        {"supplier": "crossref", "paper_id": "m-7", "doi": "10.9000/m.7", "year": 2024,
         "authors": [A(f"T{i}") for i in range(7)]},
    ]
    return lines


def build_multi_er(entities):
    members = [(1, entities["A"], "org::a"), (1, entities["B"], "org::b"),
               (1, entities["C"], "org::c"), (1, entities["D"], "org::d"),
               (1, entities["E"], "org::e")]
    return members


BAD_RAW_LINES = [
    json.dumps({"supplier": "crossref", "paper_id": "bad-1", "doi": "10.9000/b.1",
                "year": 2024, "authors": []}, ensure_ascii=False),
    json.dumps({"supplier": "crossref", "paper_id": "bad-2", "doi": "10.9000/b.2",
                "year": "n/a", "authors": [{"name": "Z"}]}, ensure_ascii=False),
    '{"supplier": "crossref", "paper_id": "bad-3", "doi": "10.9000/b.3", "year": 2024, "authors": [',
]


def independent_recompute(con, entity_map):
    """独立重算：只用作者行 + 分摊行（不调用 account_groups），核对机构/年度与待归属。

    返回 {("entity", eid, year): Fraction, ("unassigned", "", year): Fraction}，
    额外核对每位作者的份额 == 1/作者数 且 目标之和 == 作者份额。
    """
    totals = {}
    problems = []
    rows = con.execute("""
        SELECT a.group_key, a.author_pos, a.year, a.n_authors,
               cast(a.share_num as varchar), cast(a.share_den as varchar)
        FROM contrib_authors a ORDER BY a.group_key, a.author_pos""").fetchall()
    targets = con.execute("""
        SELECT group_key, author_pos, target_kind, entity_id,
               cast(share_num as varchar), cast(share_den as varchar)
        FROM contrib_share_targets""").fetchall()
    by_author = {}
    for gk, pos, kind, eid, num, den in targets:
        by_author.setdefault((gk, pos), []).append(
            (kind, eid, Fraction(int(num), int(den))))
    for gk, pos, year, n_authors, num, den in rows:
        share = Fraction(int(num), int(den))
        if share != Fraction(1, n_authors):
            problems.append(("作者份额错误", gk, pos, share, n_authors))
        ts = by_author.get((gk, pos), [])
        if not ts or sum(s for _, _, s in ts) != share:
            problems.append(("作者目标之和 != 作者份额", gk, pos))
        for kind, eid, s in ts:
            key = ("entity", eid, year) if kind == "entity" else ("unassigned", "", year)
            totals[key] = totals.get(key, Fraction(0)) + s
    return totals, problems


def sum_members_fraction(con):
    """机构库/分摊表的所有分数和 == 计入论文数。"""
    total = Fraction(0)
    for num, den in con.execute(
            "SELECT cast(share_num as varchar), cast(share_den as varchar) "
            "FROM contrib_share_targets").fetchall():
        total += Fraction(int(num), int(den))
    return total


def run_full(base, cat, er, db, csvdir, *, round_no, priority, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import _bootstrap\n"
        "from org_er.contrib.job import run_job\n"
        "from org_er.contrib.verify import verify_run\n"
        "s, con = run_job(er_db=%r, round_no=%d, catalog=%r, priority=%r, out_db=%r, out_dir=%r)\n"
        "print('BATCH', s.batch_id)\n"
        "print('VERIFY', verify_run(con)['ok'])\n"
        "con.close()\n"
    ) % (str(ROOT / "scripts"), str(er), round_no, str(cat), priority, str(db), str(csvdir))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env)
    print(f"    (子进程退出码 {r.returncode}"
          f"{'，'+r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ''}"
          f"{'，'+os.environ.get('ORG_ER_CONTRIB_FAULT','') if extra_env else ''})")
    return r


def _is_legacy(path):
    import duckdb
    con = duckdb.connect(str(path), read_only=True)
    try:
        rows = con.execute(
            "SELECT data_type FROM information_schema.columns WHERE table_catalog = "
            "current_database() AND table_name = 'contrib_institution_year' "
            "AND column_name = 'total_num'").fetchall()
        return bool(rows) and str(rows[0][0]).upper() in ("HUGEINT", "BIGINT", "INTEGER")
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", help="保留工作目录（默认用临时目录并在结束时删除）")
    args = ap.parse_args()

    if args.keep:
        base = Path(args.keep)
        # 每次演示都从干净目录开始，避免上次崩溃留下的暂存物干扰“重启恢复”场景
        for p in base.glob("*"):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink()
        base.mkdir(parents=True, exist_ok=True)
    else:
        base = Path(tempfile.mkdtemp(prefix="contrib_regression_"))
    P = scenario_paths(base)
    print(f"工作目录: {base}")

    try:
        run_all(P)
    finally:
        if not args.keep:
            shutil.rmtree(base, ignore_errors=True)

    n_ok = sum(1 for _, ok in CHECKS if ok)
    print("\n" + "=" * 78)
    print(f"{n_ok}/{len(CHECKS)} 项通过")
    if n_ok != len(CHECKS):
        raise SystemExit(1)


def run_all(P):
    # ============================================================ §1 素数目录
    h("§1 素数 n=2..101：年度精确分数超过 int128，必须完整保存并在导出中可见")
    prime_lines, prime_ps = build_prime_catalog(None)
    write_catalog(P["prime_catalog"], prime_lines)
    exact = sum(Fraction(1, n) for n in prime_ps)
    make_er_db(P["prime_er"], [(1, "E_A", "d::one")])
    erfp_before = er_content_signature(P["prime_er"])

    summary, con = run_job(er_db=str(P["prime_er"]), round_no=1,
                           catalog=str(P["prime_catalog"]), priority=["crossref"],
                           out_db=str(P["prime_db"]), out_dir=str(P["prime_csv"]))
    stored = con.execute(
        "SELECT cast(total_num as varchar), cast(total_den as varchar), total_text, "
        "total_decimal_approx, total_decimal_note FROM contrib_institution_year "
        "WHERE entity_id='E_A' AND year=2024").fetchone()
    got = Fraction(int(stored[0]), int(stored[1]))
    print(f"    {len(prime_ps)} 篇素数作者数论文（2..101 共 {len(prime_ps)} 个素数）")
    print(f"    精确和 {got}")
    print(f"    位数：分子 {len(stored[0])}、分母 {len(stored[1])}（int128 仅 ~39 位十进制）")
    print(f"    展示约 {stored[3]}（标注 {stored[4]}）；总 {summary.institution_year.get(('E_A', 2024))}")
    check("§1 年度分数精确等于 1/n 之和（超过 int128 仍完整）", got == exact)
    check("§1 分子分母未被截断/拒绝，且明显超出 int128",
          len(stored[0]) > 20 and len(stored[1]) > 20 and got.numerator.bit_length() > 128,
          f"分子/分母 {len(stored[0])}/{len(stored[1])} 位十进制")
    check("§1 展示小数与精确值分离并标注", stored[3] != stored[2]
          and stored[4] == "approx_display_only" and not stored[2].startswith(stored[3]))
    v = verify_run(con)
    check("§1 独立核验通过（含大整数精确重建）", v["ok"],
          f"最大 {v['V8_arbitrary_precision_totals']['detail'].split('最大')[1][:32]}")
    exp = verify_export(con, P["prime_csv"])
    check("§1 导出 CSV 回读核验（分子分母逐格一致）", exp["ok"])
    inst_csv, unas_csv = csv_exact_totals(P["prime_csv"])
    check("§1 导出文件携带完整精确分数", inst_csv[("E_A", 2024)] == exact)
    con.close()
    check("§1 机构库与历史未被改写", er_content_signature(P["prime_er"]) == erfp_before)

    # ============================================================ §2 独立重算
    h("§2 另一份目录：不同作者数 + 多机构分配；按作者明细独立重算并核对导出")
    entities = {"A": "ent_A", "B": "ent_B", "C": "ent_C", "D": "ent_D", "E": "ent_E"}
    multi, multi_members = build_multi_catalog(entities)
    write_catalog(P["multi_catalog"], multi)
    with open(P["multi_catalog"], "a", encoding="utf-8") as f:
        for raw in BAD_RAW_LINES:
            f.write(raw + "\n")
    make_er_db(P["multi_er"], multi_members)
    multi_er_sig = er_content_signature(P["multi_er"])

    s2, con2 = run_job(er_db=str(P["multi_er"]), round_no=1,
                       catalog=str(P["multi_catalog"]), priority=["crossref", "scopus", "local_ir"],
                       out_db=str(P["multi_db"]), out_dir=str(P["multi_csv"]))
    print(f"    目录行 {s2.n_lines}：有效 {s2.n_ok}，坏行 {s2.n_rejected}；"
          f"计入 {s2.n_counted}，隔离 {s2.n_quarantined}；作者行 {s2.n_author_rows}")

    # 手工推算（与核算代码无关）：M1 论文 3 作者，首位分到 A/B/C 各 (1/3)/3=1/9
    inst, unas = csv_exact_totals(P["multi_csv"])
    manual = {
        ("ent_A", 2024): Fraction(1, 9) + Fraction(1, 5),  # m.1 首位 + m.3 P1
        ("ent_B", 2024): Fraction(1, 9),                    # m.1 首位（重复署名去重）
        ("ent_C", 2024): Fraction(1, 9) + Fraction(1, 6),   # m.1 首位 + M2 第二机构
        ("ent_D", 2024): Fraction(1, 6),                    # m.1 M2
        ("ent_E", 2024): Fraction(2, 5),                    # m.3 P3 1/5 + P4 1/5（同机构去重）
    }
    manual[("ent_A", 2023)] = Fraction(1, 4)
    manual[("ent_B", 2023)] = Fraction(1, 4)
    manual[("ent_C", 2023)] = Fraction(1, 4)
    manual[("ent_D", 2023)] = Fraction(1, 4)
    manual[("ent_D", 2022)] = Fraction(1)                          # m.6 归并后 2 作者都定位 D
    print("    机构×年份（导出精确分数）：")
    for k in sorted(inst):
        print(f"      {k[1]}  {k[0]}  {inst[k]}")
    print(f"    待归属：{ {y: str(f) for y, f in sorted(unas.items())} }")
    check("§2 导出机构×年份与手工推算一致", inst == manual)
    # 待归属：m.1 M3 1/3；m.3 P2 1/5 + P5 1/5；m.7 全 7 位各 1/7 => 1；m.3 P1 定位到 A 故不入待归属
    expect_unas = {2024: Fraction(1, 3) + Fraction(2, 5) + Fraction(1)}
    check("§2 导出的待归属与手工推算一致", unas == expect_unas)

    recomputed, problems = independent_recompute(con2, dict())
    csv_totals = {("entity", eid, year): v for (eid, year), v in inst.items()}
    csv_totals.update({("unassigned", "", year): v for year, v in unas.items()})
    check("§2 按作者明细独立重算与导出完全一致",
          recomputed == csv_totals and not problems, f"问题 {problems[:2]}")
    check("§2 分摊总分 == 计入论文数", sum_members_fraction(con2) == s2.n_counted)
    v2 = verify_run(con2)
    check("§2 独立核验全过（含隔离/坏行/行归属）", v2["ok"])
    exp2 = verify_export(con2, P["multi_csv"])
    check("§2 导出回读核验", exp2["ok"])
    # 隔离论文零分摊
    n_q = con2.execute(
        "SELECT COUNT(*) FROM contrib_authors WHERE group_key='doi::10.9000/m.4'").fetchone()[0]
    check("§2 优先级打平冲突的论文被隔离且零分摊", s2.n_quarantined == 1 and n_q == 0)
    con2.close()
    check("§2 机构库（含历史表）未被改写", er_content_signature(P["multi_er"]) == multi_er_sig)

    # ============================================================ §3 旧格式
    h("§3 旧版 HUGEINT 小账目：识别 -> 核验 -> 无损转换 -> 重新导出；坏库不动")
    # 旧格式固定样本（data/legacy_contrib_hugeint.duckdb，由旧版 HUGEINT 账目保存而来）
    legacy_src = ROOT / "data" / "legacy_contrib_hugeint.duckdb"
    if not legacy_src.exists():
        legacy_src = ROOT / "output" / "contrib.duckdb"
    if legacy_src.exists() and _is_legacy(legacy_src):
        shutil.copy2(legacy_src, P["legacy_live"])
        # 旧库没有 manifest；先把旧 CSV 也复制过来，模拟“导出文件还是上次的”
        if (ROOT / "output" / "contrib").exists():
            shutil.rmtree(P["legacy_live_csv"], ignore_errors=True)
            shutil.copytree(ROOT / "output" / "contrib", P["legacy_live_csv"])
        before_legacy = legacy_snapshot(P["legacy_live"])
        info, con3 = reuse_existing(out_db=str(P["legacy_live"]), out_dir=str(P["legacy_live_csv"]),
                                    verify=True)
        print(f"    识别 {info['detected_format']}；转换前核验 {info['verified_before']['ok']}；"
              f"转换后核验 {info['verified_after']['ok']}；备份 {Path(info['backup']).name}")
        check("§3 旧 HUGEINT 账目可识别、转换前可核验", info["detected_format"] == "legacy_hugeint"
              and info["verified_before"]["ok"])
        check("§3 无损转换为 BIGNUM 且转换后核验通过",
              info["migrated"] and info["verified_after"]["ok"])
        # 无损：逐分数与转换前的十进制字符串比对
        after = legacy_fraction_map(con3)
        check("§3 转换前后每个分子/分母逐一相等（无损）", after == before_legacy)
        old_csv = read_csv_rows(P["legacy_live_csv"] / "contrib_institution_year.csv")
        new_csv = read_csv_rows(P["legacy_live_csv"] / "contrib_institution_year.csv")
        check("§3 重新导出成功且精确列与库一致",
              verify_export(con3, P["legacy_live_csv"])["ok"])
        print(f"    备份文件保留: {info['backup']}")
        con3.close()

        # 无法转换的旧库：把分数列回写成 HUGEINT 溢出范围之外的字符串（模拟损坏/不兼容）
        shutil.copy2(legacy_src, P["legacy_bad"])
        import duckdb
        bad = duckdb.connect(str(P["legacy_bad"]))
        # 按关键表建一份“看起来像旧格式、但分数不可核验”的账目：
        # 年度汇总与作者明细不一致，独立核验必须拒绝它，转换不得发生。
        bad.execute("UPDATE contrib_institution_year SET total_num = 7, total_den = 1")
        bad.close()
        before_bad_hash = current_file_signature(P["legacy_bad"])
        before_bad = legacy_snapshot(P["legacy_bad"])
        try:
            reuse_existing(out_db=str(P["legacy_bad"]), out_dir=str(P["legacy_bad_csv"]))
            check("§3 不可核验的旧账目被拒绝转换", False)
        except (ValueError, FileNotFoundError) as e:
            print(f"    预期拒绝: {str(e)[:110]}")
            ok_reject = True
        check("§3 无法转换/核验失败时原账目保留", ok_reject
              and legacy_snapshot(P["legacy_bad"]) == before_bad
              and current_file_signature(P["legacy_bad"]) == before_bad_hash)
    else:
        print("    （跳过：仓库中无旧版 output/contrib.duckdb，改用现造旧格式库）")
        build_synthetic_legacy(P["legacy_live"], P["legacy_live_csv"])
        before_legacy = legacy_snapshot(P["legacy_live"])
        info, con3 = reuse_existing(out_db=str(P["legacy_live"]), out_dir=str(P["legacy_live_csv"]))
        check("§3 旧 HUGEINT 账目可识别、无损转换并核验",
              info["detected_format"] == "legacy_hugeint" and info["migrated"]
              and info["verified_before"]["ok"] and info["verified_after"]["ok"]
              and legacy_fraction_map(con3) == before_legacy)
        con3.close()

    # ============================================================ §4 成功后的失败重跑
    h("§4 同一输出位置：先成功一份普通目录，再跑一份会失败的批次（写明细中途报错）")
    P["rerun_csv"].parent.mkdir(parents=True, exist_ok=True)
    s_ok, con4 = run_job(er_db=str(P["multi_er"]), round_no=1,
                         catalog=str(P["multi_catalog"]),
                         priority=["crossref", "scopus", "local_ir"],
                         out_db=str(P["rerun_db"]), out_dir=str(P["rerun_csv"]))
    cur0 = cstore.get_meta(con4, "batch_id")
    sig0 = bank_signature(con4)
    csv0 = csv_dir_signature(P["rerun_csv"])
    vals0 = db_fractions(con4)
    con4.close()
    print(f"    成功批次 {cur0}：机构×年份 {len(s_ok.institution_year)} 项，CSV {len(csv0)} 个文件")

    r = run_full(base=P, cat=P["multi_catalog"], er=P["multi_er"], db=P["rerun_db"],
                 csvdir=P["rerun_csv"], round_no=1, priority=["crossref"],
                 extra_env={"ORG_ER_CONTRIB_FAULT": "mid_write:error"})
    check("§4 失败批次确实失败（写入明细中途）", r.returncode != 0 and "fault-injected" in r.stderr)
    import duckdb
    con5 = duckdb.connect(str(P["rerun_db"]))
    st = cstore.get_state(con5)
    print(f"    状态 {st['status']}；保留批次 {st['previous_batch_id']}；"
          f"暂存物 {st['staging_schema']}")
    check("§4 旧批次完整保留（当前表与 CSV 都未变）",
          bank_signature(con5) == sig0 and csv_dir_signature(P["rerun_csv"]) == csv0
          and db_fractions(con5) == vals0)
    check("§4 新作者明细未“半截入库”为当前结果",
          cstore.get_meta(con5, "batch_id") == cur0
          and verify_run(con5)["ok"])
    # 半成品可辨认：暂存 schema 里有写入中途的明细，且状态记录指向它
    stage_names = [r[0] for r in con5.execute(
        "SELECT schema_name FROM duckdb_schemas() WHERE schema_name LIKE ?",
        [cstore.STAGE_PREFIX + "%"]).fetchall()]
    check("§4 未完成数据可辨认（状态记录 + 命名的暂存 schema）",
          st["status"] == "failed" and st["batch_id"] and st["staging_schema"] in stage_names
          and stage_names)
    # CSV 仍然是上一份（不是空壳/半套）
    check("§4 交付 CSV 仍是上一份完整结果",
          csv_matches_any_db_batch(con5, P["rerun_csv"])[0], cur0)
    con5.close()

    # 进程级终止（不是异常）：同样的窗口
    print("    再次以进程终止方式失败（os._exit，无异常栈）")
    r = run_full(base=P, cat=P["multi_catalog"], er=P["multi_er"], db=P["rerun_db"],
                 csvdir=P["rerun_csv"], round_no=1, priority=["crossref"],
                 extra_env={"ORG_ER_CONTRIB_FAULT": "mid_write:crash"})
    check("§4 进程终止后当前结果仍是旧批次、CSV 未变",
          r.returncode == 137 and csv_dir_signature(P["rerun_csv"]) == csv0
          and current_batch_of(P["rerun_db"]) == cur0 and bank_signature_of(P["rerun_db"]) == sig0)

    # 恢复 + 重跑成功
    con6 = duckdb.connect(str(P["rerun_db"]))
    rec = recover_pending(con6)
    print(f"    重启恢复：{rec['action']}；保留批次 {rec['kept']}")
    check("§4 恢复丢弃未提交暂存物并保住旧批次",
          rec["action"] == "discard_uncommitted_stage" and rec["kept"] == cur0
          and cstore.detect_format(con6) == "bignum_v2"
          and not [r[0] for r in con6.execute(
              "SELECT schema_name FROM duckdb_schemas() WHERE schema_name LIKE ?",
              [cstore.STAGE_PREFIX + "%"]).fetchall()])
    check("§4 恢复后当前结果仍完整可核验", verify_run(con6)["ok"]
          and cstore.read_manifest(P["rerun_csv"])["batch_id"] == cur0
          and db_fractions(con6) == vals0)
    con6.close()
    s_rerun, con7 = run_job(er_db=str(P["multi_er"]), round_no=1,
                            catalog=str(P["multi_catalog"]),
                            priority=["crossref", "scopus", "local_ir"],
                            out_db=str(P["rerun_db"]), out_dir=str(P["rerun_csv"]))
    new_batch = cstore.get_meta(con7, "batch_id")
    check("§4 重跑成功后新批次成为唯一当前结果",
          new_batch != cur0 and verify_run(con7)["ok"]
          and csv_matches_any_db_batch(con7, P["rerun_csv"])[0])
    check("§4 重跑后 CSV 与库整批一致（无新旧混合）",
          cstore.read_manifest(P["rerun_csv"])["batch_id"] == new_batch
          and db_fractions(con7) == vals0
          and csv_exact_totals(P["rerun_csv"]) == vals0)
    con7.close()

    # ============================================================ §5 导出/切换窗口终止
    h("§5 导出中途终止；以及“数据库已切换、CSV 未发布”窗口的恢复")
    # 先制造一个成功结果，记录其库内容与 CSV（用于判断“保住的是哪一份”）
    _, con8 = run_job(er_db=str(P["multi_er"]), round_no=1,
                      catalog=str(P["multi_catalog"]), priority=["crossref", "scopus", "local_ir"],
                      out_db=str(P["rerun_db"]), out_dir=str(P["rerun_csv"]))
    old_batch = cstore.get_meta(con8, "batch_id")
    old_csv = csv_dir_signature(P["rerun_csv"])
    old_sig = bank_signature(con8)
    old_vals = db_fractions(con8)
    con8.close()

    r = run_full(base=P, cat=P["prime_catalog"], er=P["prime_er"], db=P["rerun_db"],
                 csvdir=P["rerun_csv"], round_no=1, priority=["crossref"],
                 extra_env={"ORG_ER_CONTRIB_FAULT": "mid_export:crash"})
    check("§5 导出中途终止：旧库与旧 CSV 原样保留",
          r.returncode == 137 and csv_dir_signature(P["rerun_csv"]) == old_csv
          and bank_signature_of(P["rerun_db"]) == old_sig)
    con9 = duckdb.connect(str(P["rerun_db"]))
    st = cstore.get_state(con9)
    print(f"    导出台账：状态 {st['status']}，临时目录 {Path(st['staging_csv_dir']).name}")
    check("§5 导出半成品在临时目录中，可辨认且不影响交付目录",
          Path(st["staging_csv_dir"]).exists() and not Path(st["staging_csv_dir"]).name ==
          Path(P["rerun_csv"]).name)
    con9.close()

    # 模拟“数据库已切换、CSV 未发布”：用真实运行到 committed 后强行终止发布
    r = run_full(base=P, cat=P["prime_catalog"], er=P["prime_er"], db=P["rerun_db"],
                 csvdir=P["rerun_csv"], round_no=1, priority=["crossref"],
                 extra_env={"ORG_ER_CONTRIB_FAULT": "before_publish:crash"})
    check("§5 切换后发布前终止：数据库是新批次，CSV 仍是旧批次（不混合）",
          r.returncode == 137 and csv_dir_signature(P["rerun_csv"]) == old_csv)
    con10 = duckdb.connect(str(P["rerun_db"]))
    st10 = cstore.get_state(con10)
    check("§5 该窗口被记录为 committed（可续做）", st10["status"] == "committed")
    published = con10.execute(
        "SELECT COUNT(*) FROM contrib_institution_year").fetchone()[0]
    print(f"    已切换批次 {st10['batch_id']}；等待发布的临时目录 "
          f"{Path(st10['staging_csv_dir']).name}")
    check("§5 已切换的新批次与旧 CSV 尚未混成一份",
          csv_dir_signature(P["rerun_csv"]) == old_csv
          and cstore.get_meta(con10, "batch_id") == st10["batch_id"]
          and st10["batch_id"] != old_batch)
    print(f"    此刻：库里是批次 {st10['batch_id']}（{published} 个机构×年份行），"
          f"CSV 仍对应旧批次 {old_batch}；两份完整结果，没有混合")
    # 重启恢复：完成 CSV 发布并核验
    rec = recover_pending(con10)
    print(f"    恢复：{rec['action']}；采用批次 {rec['kept']}")
    exp = verify_export(con10, P["rerun_csv"])
    check("§5 重启后自动完成发布并核验整批一致",
          rec["action"] == "finish_committed_publication" and exp["ok"]
          and cstore.get_state(con10)["status"] == "ready"
          and verify_run(con10)["ok"])
    check("§5 发布后 CSV 与当前库同一批次（分子分母一致）",
          cstore.read_manifest(P["rerun_csv"])["batch_id"] == st10["batch_id"]
          and csv_exact_totals(P["rerun_csv"]) == db_fractions(con10))
    check("§5 数据库切换到的新批次没有与旧 CSV 混成一份",
          csv_dir_signature(P["rerun_csv"]) != old_csv
          and cstore.read_manifest(P["rerun_csv"])["batch_id"] == st10["batch_id"]
          and exp["ok"])
    con10.close()

    # ============================================================ §6 稳定性/只读
    h("§6 只读与稳定性复核")
    conA = duckdb.connect(str(P["prime_db"]))
    base_sig = run_signature(conA)
    conA.close()
    shuffled = list(prime_lines)
    random.Random(7).shuffle(shuffled)
    tmp_cat = P["prime_catalog"].parent / "prime_shuffled.jsonl"
    write_catalog(tmp_cat, shuffled)
    _, conB = run_job(er_db=str(P["prime_er"]), round_no=1, catalog=str(tmp_cat),
                      priority=["crossref"], out_db=":memory:", out_dir=None)
    check("§6 乱序读入结果签名一致", run_signature(conB) == base_sig)
    conB.close()
    check("§6 素数机构库（含历史表）始终未被改写",
          er_content_signature(P["prime_er"]) == erfp_before)


def legacy_snapshot(path):
    """旧账目的精确分数全表快照（十进制字符串，不经过浮点）。"""
    import duckdb
    con = duckdb.connect(str(path), read_only=True)
    try:
        return legacy_fraction_map(con)
    finally:
        con.close()


def legacy_fraction_map(con):
    m = {}
    rows = con.execute(
        "SELECT group_key, author_pos, cast(share_num as varchar), cast(share_den as varchar) "
        "FROM contrib_authors ORDER BY group_key, author_pos").fetchall()
    for gk, pos, n, d in rows:
        m[("authors", gk, str(pos))] = (str(n), str(d))
    rows = con.execute(
        "SELECT group_key, author_pos, target_kind, entity_id, "
        "cast(share_num as varchar), cast(share_den as varchar) FROM contrib_share_targets").fetchall()
    for gk, pos, kind, eid, n, d in rows:
        m[("targets", gk, str(pos), kind, str(eid))] = (str(n), str(d))
    for eid, y, n, d in con.execute(
            "SELECT entity_id, year, cast(total_num as varchar), cast(total_den as varchar) "
            "FROM contrib_institution_year").fetchall():
        m[("inst", str(eid), str(y))] = (str(n), str(d))
    for y, n, d in con.execute(
            "SELECT year, cast(total_num as varchar), cast(total_den as varchar) "
            "FROM contrib_unassigned_year").fetchall():
        m[("unas", str(y))] = (str(n), str(d))
    return m


def db_fractions(con):
    inst = {(r[0], int(r[1])):
            Fraction(int(str(r[2])), int(str(r[3]))) for r in con.execute(
                "SELECT entity_id, year, cast(total_num as varchar), cast(total_den as varchar) "
                "FROM contrib_institution_year").fetchall()}
    unas = {int(r[0]): Fraction(int(str(r[1])), int(str(r[2]))) for r in con.execute(
        "SELECT year, cast(total_num as varchar), cast(total_den as varchar) "
        "FROM contrib_unassigned_year").fetchall()}
    return inst, unas


def current_batch_of(db_path):
    """不写库地读出“当前成功结果”的批次号（只读连接）。

    注意：未完成运行期间 contrib_state.batch_id 是“正在跑的批次”，
    当前结果批次以结果表自己的 contrib_meta.batch_id 为准。
    """
    import duckdb
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return cstore.get_meta(con, "batch_id")
    finally:
        con.close()


def bank_signature_of(db_path):
    import duckdb
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return run_signature(con)
    finally:
        con.close()


def build_synthetic_legacy(db_path, csvdir):
    """没有仓库旧库时，现造一个 HUGEINT 旧格式账目（走旧版 DDL/插入语义）。"""
    import duckdb
    if Path(db_path).exists():
        Path(db_path).unlink()
    con = duckdb.connect(str(db_path))
    con.execute("""
      CREATE TABLE contrib_lines (line_no INTEGER, status TEXT, reject_reason TEXT,
        supplier TEXT, paper_id TEXT, doi_raw TEXT, doi_norm TEXT, year_raw TEXT,
        year INTEGER, n_authors INTEGER, raw_json TEXT)""")
    con.execute("""
      CREATE TABLE contrib_groups (group_key TEXT PRIMARY KEY, group_kind TEXT, status TEXT,
        n_versions INTEGER, selected_supplier TEXT, selected_paper_id TEXT, selected_year INTEGER,
        select_reason TEXT, version_diffs_json TEXT)""")
    con.execute("""CREATE TABLE contrib_group_members (group_key TEXT, supplier TEXT,
        paper_id TEXT, line_no INTEGER, priority_rank INTEGER, content_hash TEXT,
        is_selected BOOLEAN)""")
    con.execute("""CREATE TABLE contrib_authors (group_key TEXT, author_pos INTEGER,
        author_name TEXT, year INTEGER, n_authors INTEGER, share_num HUGEINT, share_den HUGEINT,
        affil_refs_json TEXT, located_entities_json TEXT, unlocated_refs_json TEXT,
        n_distinct_located INTEGER, disposition TEXT, division_basis TEXT)""")
    con.execute("""CREATE TABLE contrib_share_targets (group_key TEXT, author_pos INTEGER,
        target_kind TEXT, entity_id TEXT, share_num HUGEINT, share_den HUGEINT)""")
    con.execute("""CREATE TABLE contrib_institution_year (entity_id TEXT, year INTEGER,
        total_num HUGEINT, total_den HUGEINT, n_shares INTEGER, n_papers INTEGER)""")
    con.execute("""CREATE TABLE contrib_unassigned_year (year INTEGER, total_num HUGEINT,
        total_den HUGEINT, n_authors INTEGER)""")
    con.execute("CREATE TABLE contrib_meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("INSERT INTO contrib_meta VALUES ('round_no','3'),('priority','')")
    con.execute("INSERT INTO contrib_group_members VALUES ('doi::x','s','p',1,0,'h',true)")
    con.execute("INSERT INTO contrib_lines VALUES (1,'ok','','s','p','','doi::x','2024',2024,2,'{}')")
    con.execute("""INSERT INTO contrib_groups VALUES
        ('doi::x','doi','counted',1,'s','p',2024,'single_version','[]')""")
    con.execute("""INSERT INTO contrib_authors VALUES
        ('doi::x',1,'A',2024,2,1,2,'[]','[\"ent_A\"]','[]',1,'located','ok')""")
    con.execute("INSERT INTO contrib_share_targets VALUES ('doi::x',1,'entity','ent_A',1,2)")
    con.execute("INSERT INTO contrib_institution_year VALUES ('ent_A',2024,1,2,1,1)")
    con.execute("INSERT INTO contrib_unassigned_year VALUES (2024,0,1,0)")
    con.close()
    Path(csvdir).mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
