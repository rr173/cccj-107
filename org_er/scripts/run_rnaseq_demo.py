#!/usr/bin/env python3
"""RNA-seq 计数端到端演示与回归（全部断言通过才退出 0）。

链路：生成人工夹具 -> 生产计数（SAM/BAM 各跑一次）-> 独立逐碱基解释器交叉核对
-> 大坐标稀疏注释存储实测 -> 故障夹具（乱序/截断 BAM/坏 GTF/重复样本名）验证
非零退出 -> 读回矩阵与 --explain 查片段。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401
from org_er.rnaseq.annotation import AnnotationError, load_gtf
from org_er.rnaseq.counting import AlignmentReadError, InputOrderError
from org_er.rnaseq.outputs import read_counts, read_fragment, run

ROOT = _bootstrap.ROOT
DATA = ROOT / "data" / "rnaseq"
OUT = ROOT / "output" / "rnaseq"
SCRIPTS = ROOT / "scripts"
PY = sys.executable
ASSERTIONS = 0


def check(cond: bool, msg: str) -> None:
    global ASSERTIONS
    ASSERTIONS += 1
    if not cond:
        raise AssertionError(msg)
    print(f"  [{ASSERTIONS:02d}] OK {msg}")


def expected_s1() -> dict:
    """完整 s1（SAM 文本路径，含 23 个片段组；BAM 副本缺 fragM/fragN 两条坏 CIGAR）。"""
    return {
        "fragA": ("assigned", "gA"), "fragB": ("assigned", "gA"),
        "fragC": ("assigned", "gA"), "fragD": ("assigned", "gC"),
        "fragE": ("assigned", "gB"), "fragF": ("ambiguous", None),
        "fragG": ("ambiguous", None), "fragH": ("assigned", "gE"),
        "fragI": ("assigned", "gF"), "fragJ": ("ignored_only", None),
        "fragK": ("malformed", None), "fragL": ("malformed", None),
        "fragM": ("malformed", None), "fragN": ("malformed", None),
        "fragO": ("unmapped", None), "fragP": ("duplicate", None),
        "fragQ": ("multimapping", None), "fragR": ("low_mapq", None),
        "fragS": ("chimeric", None), "fragT": ("no_feature", None),
        "fragU": ("assigned", "gA"), "fragV": ("malformed", None),
        "fragW": ("unmapped", None),
    }


def expected_s1_bam() -> dict:
    """BAM 路径：htslib 无法承载 fragM/fragN 的非法 CIGAR，夹具生成时已剔除。"""
    full = expected_s1()
    return {k: v for k, v in full.items() if k not in ("fragM", "fragN")}


def expected_s2() -> dict:
    """forward：只接受与片段方向同链的基因。"""
    return {"fragF1": ("assigned", "gA"), "fragF2": ("assigned", "gD"),
            "fragF3": ("no_feature", None), "fragF4": ("no_feature", None),
            "fragF5": ("assigned", "gA"), "fragF6": ("multimapping", None)}


def expected_s3() -> dict:
    """reverse：只接受与片段方向相反链的基因。"""
    return {"fragR1": ("assigned", "gD"), "fragR2": ("assigned", "gA"),
            "fragR3": ("assigned", "gF"), "fragR4": ("assigned", "gB"),
            "fragR5": ("assigned", "gF"), "fragR6": ("assigned", "gE")}


def assert_fragments(out_dir: Path, sample: str, expected: dict) -> None:
    seen = {}
    with open(out_dir / "fragments" / f"{sample}.jsonl", encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            seen[row["qname"]] = (row["status"], row["assigned_gene"])
    check(set(seen) == set(expected), f"{sample}: 片段集合与预期一致（{len(seen)} 个 QNAME）")
    for qname, (status, gene) in expected.items():
        check(seen[qname][0] == status, f"{sample}/{qname} -> {status}")
        if gene:
            check(seen[qname][1] == gene, f"{sample}/{qname} 归属 {gene}")


def main() -> int:
    print("== 1. 生成人工夹具 ==")
    subprocess.run([PY, str(SCRIPTS / "make_rnaseq_sample.py")], check=True)
    gtf = DATA / "demo.gtf"

    print("== 2. 生产计数：BAM 样本 s1 + SAM 样本 s2/s3 ==")
    info = run(gtf, DATA / "samples.json", OUT, min_mapq=10)
    check(info["global_check"]["ok"], "全局校验：矩阵总和 == assigned 合计")

    print("== 2b. 生产计数：完整 s1 的 SAM 文本路径（含非法 CIGAR 片段）==")
    full_sheet = DATA / "samples_full_sam.json"
    full_sheet.write_text(json.dumps({"samples": [
        {"sample": "s1full", "alignment": "s1_unstranded.sam", "library": "unstranded"},
        {"sample": "s2", "alignment": "s2_forward.sam", "library": "forward"},
        {"sample": "s3", "alignment": "s3_reverse.sam", "library": "reverse"}]},
        ensure_ascii=False), encoding="utf-8")
    out_full = OUT.parent / "rnaseq_full_sam"
    info_full = run(gtf, full_sheet, out_full, min_mapq=10)

    print("== 3. 逐片段状态/归属断言 ==")
    assert_fragments(OUT, "s1", expected_s1_bam())
    assert_fragments(out_full, "s1full", expected_s1())
    assert_fragments(OUT, "s2", expected_s2())
    assert_fragments(OUT, "s3", expected_s3())

    print("== 4. 矩阵内容断言（含零计数基因 gG/gH）==")
    samples, gene_ids, matrix = read_counts(OUT / "counts.csv")
    check(samples == ["s1", "s2", "s3"], "矩阵样本列顺序")
    check(gene_ids == sorted(gene_ids), "矩阵基因行按 gene_id 排序")
    check({"gG", "gH"} <= set(gene_ids)
          and all(matrix[s][g] == 0 for s in samples for g in ("gG", "gH")),
          "零计数基因 gG/gH 保留在矩阵中")
    # BAM s1：assigned 为 fragA/B/C/D/E/H/I/U 共 8（fragM/N 不在 BAM 中）
    check(matrix["s1"]["gA"] == 4, "s1/gA = 4（fragA,B,C,U；fragF 歧义不计）")
    check(matrix["s1"]["gB"] == 1 and matrix["s1"]["gC"] == 1, "s1/gB=1（嵌套基因）, gC=1（删除）")
    check(matrix["s1"]["gE"] == 1 and matrix["s1"]["gF"] == 1, "s1/gE=1, gF=1（歧义片段不双计）")
    check(matrix["s1"]["gD"] == 0, "s1/gD=0（unstranded 下与 gA 共同命中的片段记歧义）")
    check(matrix["s2"]["gA"] == 2 and matrix["s2"]["gD"] == 1
          and all(matrix["s2"][g] == 0 for g in ("gB", "gF", "gE")),
          "s2 forward：gA=2（正向同链）, gD=1（反向同链），两个 no_feature 不计数")
    check(matrix["s3"]["gD"] == 1 and matrix["s3"]["gF"] == 2 and matrix["s3"]["gB"] == 1
          and matrix["s3"]["gA"] == 1 and matrix["s3"]["gE"] == 1,
          "s3 reverse：反向取反链（gD/gF/gB），反向读段则取 + 链（gA/gE），gF=2")
    for s in samples:
        check(sum(matrix[s].values()) == info["samples"][[m["sample"] for m in info["samples"]].index(s)]["matrix_sum"],
              f"{s}: 矩阵总和等于该样本 assigned")
    # 完整 SAM 路径：多两个 malformed（fragM/fragN），assigned 计数与 BAM 相同
    _, _, matrix_full = read_counts(out_full / "counts.csv")
    check(matrix_full["s1full"] == matrix["s1"], "SAM 与 BAM 的 s1 计数完全一致（坏 CIGAR 只影响状态列）")

    print("== 5. 互斥状态汇总断言 ==")
    import csv
    with open(OUT / "summary.csv", encoding="utf-8") as fh:
        summary = {r["sample"]: r for r in csv.DictReader(fh)}
    s1 = summary["s1"]
    check(s1["malformed"] == "3", "s1(BAM) malformed=3（K,L,V；M/N 无法进 BAM）")
    check(s1["ambiguous"] == "2" and s1["assigned"] == "8", "s1(BAM) ambiguous=2, assigned=8")
    check(s1["ignored_only"] == "1" and s1["duplicate"] == "1"
          and s1["multimapping"] == "1" and s1["low_mapq"] == "1"
          and s1["chimeric"] == "1" and s1["no_feature"] == "1"
          and s1["unmapped"] == "2", "s1(BAM) 其余互斥状态各 1（unmapped=2）")
    total = int(s1["total_groups"])
    check(total == sum(int(s1[k]) for k in s1
                       if k not in ("sample", "total_groups", "check", "matrix_sum")),
          "s1(BAM) 互斥分区合计 == 片段组总数（每 QNAME 恰好一项）")
    with open(out_full / "summary.csv", encoding="utf-8") as fh:
        full_summary = {r["sample"]: r for r in csv.DictReader(fh)}
    fs1 = full_summary["s1full"]
    check(fs1["malformed"] == "5" and fs1["total_groups"] == "23" and fs1["assigned"] == "8",
          "s1full(SAM) malformed=5（含坏 CIGAR 的 M/N）、23 组、assigned 仍为 8")
    check(fs1["check"] == "ok", "s1full(SAM) 互斥分区校验 ok")

    print("== 6. 独立逐碱基解释器交叉核对（BAM 与完整 SAM 各跑一遍）==")
    rc = subprocess.run([PY, str(SCRIPTS / "run_rnaseq_reference_check.py"),
                         str(gtf), str(DATA / "samples.json"), str(OUT)])
    check(rc.returncode == 0, "独立解释器核对 BAM 路径（不用生产区间树/CIGAR 代码）")
    rc = subprocess.run([PY, str(SCRIPTS / "run_rnaseq_reference_check.py"),
                         str(gtf), str(full_sheet), str(out_full)])
    check(rc.returncode == 0, "独立解释器核对完整 SAM 路径（含非法 CIGAR 片段）")

    print("== 7. 大坐标稀疏注释存储实测 ==")
    rc = subprocess.run([PY, str(SCRIPTS / "run_rnaseq_sparse_check.py")])
    check(rc.returncode == 0, "稀疏大坐标：存储与染色体长度无关且查询正确")

    print("== 8. 故障夹具：必须非零退出且不产出假矩阵 ==")
    with tempfile.TemporaryDirectory(prefix="rnaseq_fail_") as tmp:
        tmp = Path(tmp)
        try:
            run(gtf, _sheet(DATA, tmp, "bad1", "unsorted.sam"), tmp / "o1")
            check(False, "乱序输入应抛 InputOrderError")
        except InputOrderError as e:
            check("字节序" in str(e) and "fragA" in str(e), f"乱序错误带定位信息: {e}")
        check(not (tmp / "o1").exists(), "乱序失败后没有产物目录")

        try:
            run(gtf, _sheet(DATA, tmp, "bad2", "truncated.bam"), tmp / "o2")
            check(False, "截断 BAM 应抛 AlignmentReadError")
        except AlignmentReadError as e:
            check("truncated.bam" in str(e), f"BAM 读取失败带文件定位: {e}")

        # 非法 CIGAR（文本 SAM 路径）：生产代码必须识别为 malformed 而非崩溃或漏检
        out_mc = tmp / "o_mc"
        run(gtf, _sheet(DATA, tmp, "mc", "malformed_cigar.sam"), out_mc)
        check(all(read_fragment(out_mc, "mc", q)["status"] == "malformed"
                  for q in ("fragM", "fragN", "fragN2")),
              "非法 CIGAR（未知操作/消耗不符/内部裁剪）全部归入 malformed 并保留原因")

        bad_gtf = tmp / "bad.gtf"
        bad_gtf.write_text(
            'chr1\tdemo\texon\t100\t150\t.\t+\t.\tgene_id "gX";\n'
            'chr2\tdemo\texon\t100\t150\t.\t-\t.\tgene_id "gX";\n',
            encoding="utf-8")
        try:
            load_gtf(bad_gtf)
            check(False, "基因跨染色体/链不一致应整体报错")
        except AnnotationError as e:
            check("gX" in str(e), f"注释一致性错误: {e}")

        dup_sheet = tmp / "dup.json"
        dup_sheet.write_text(json.dumps({"samples": [
            {"sample": "x", "alignment": str(DATA / "s2_forward.sam"), "library": "forward"},
            {"sample": "x", "alignment": str(DATA / "s3_reverse.sam"), "library": "reverse"}]},
            ensure_ascii=False), encoding="utf-8")
        try:
            run(gtf, dup_sheet, tmp / "o3")
            check(False, "重复样本名应被拒绝")
        except Exception as e:
            check("重复" in str(e), f"样本表校验: {e}")

    print("== 9. CLI 非零退出码与 --explain 读回 ==")
    cli = SCRIPTS / "rnaseq.py"
    with tempfile.TemporaryDirectory(prefix="rnaseq_cli_") as cli_tmp:
        cli_tmp = Path(cli_tmp)
        rc = subprocess.run([PY, str(cli), "--gtf", str(gtf),
                             "--samples", str(_sheet(DATA, cli_tmp, "cli_bad", "unsorted.sam")),
                             "--out", str(cli_tmp / "should_not_exist")])
        check(rc.returncode == 3, f"乱序 CLI 退出码 3（实际 {rc.returncode}）")
        rc = subprocess.run([PY, str(cli), "--gtf", str(gtf),
                             "--samples", str(_sheet(DATA, cli_tmp, "cli_bad2", "truncated.bam")),
                             "--out", str(cli_tmp / "should_not_exist2")])
        check(rc.returncode == 4, f"截断 BAM CLI 退出码 4（实际 {rc.returncode}）")
        check(not any(cli_tmp.glob("should_not_exist*")), "失败时没有产出假矩阵目录")
    rc = subprocess.run([PY, str(cli), "--explain", "--out", str(OUT),
                         "--sample", "s1", "--qname", "fragC"],
                        capture_output=True, text=True)
    check(rc.returncode == 0 and '"gA"' in rc.stdout and "20M75N30M" in rc.stdout,
          "--explain 读回 fragC 的判定依据（含剪接 CIGAR 与候选基因）")
    row = read_fragment(OUT, "s1", "fragP")
    check(row["status"] == "duplicate" and row["candidate_genes"] == [],
          "API 读回 fragP：duplicate 优先级高于 multimapping/low_mapq")

    print(f"\n全部 {ASSERTIONS} 项断言通过。产物目录: {OUT}")
    print("  counts.csv / summary.csv / fragments/*.jsonl / run.json")
    return 0


def _sheet(data_dir: Path, out_dir: Path, sample: str, alignment: str) -> Path:
    p = out_dir / f"{sample}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"samples": [
        {"sample": sample, "alignment": str((data_dir / alignment).resolve()),
         "library": "unstranded"}]}, ensure_ascii=False), encoding="utf-8")
    return p


if __name__ == "__main__":
    sys.exit(main())
