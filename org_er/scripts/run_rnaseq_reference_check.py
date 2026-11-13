#!/usr/bin/env python3
"""独立参考解释器：用**逐碱基 Python 集合**把计数从头再算一遍。

与生产代码完全隔离——不 import org_er.rnaseq 的任何模块，自带：

- GTF 行解析（直接按制表符切列，1-based 包含 -> 0-based 半开）；
- 正则版 CIGAR 解码（逐操作把参考碱基塞进 set，N/D 不塞）；
- SAM 文本解析（BAM 仅借 pysam 枚举记录，flag/CIGAR/归属判定全部走本文件逻辑）；
- QNAME 分组、配对结构检查、互斥状态判定与链筛选。

随后与生产产物（counts.csv / summary.csv / fragments/*.jsonl）逐片段、逐基因
交叉核对；任何不一致以非零状态退出。
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import _bootstrap  # noqa: F401

CIGAR_TOKEN = re.compile(r"(\d+)([MIDNSHP=X])")
COVER_OPS = {"M", "=", "X"}
STATUS_ORDER = ("malformed", "unmapped", "duplicate", "multimapping", "low_mapq",
                "chimeric", "no_feature", "ambiguous", "assigned")
IGNORED_ONLY = "ignored_only"
PAIRED, UNMAPPED, REVERSE, FIRST, LAST, SECONDARY, DUP, SUPP = 0x1, 0x4, 0x10, 0x40, 0x80, 0x100, 0x400, 0x800


# ------------------------------------------------------------------ 自带 GTF 解析
def ref_load_gtf(path: Path):
    """返回 (gene -> (chrom, strand, set(外显子 0-based 碱基))) 与每基因区间列表。"""
    genes: dict[str, dict] = {}
    raw_intervals: dict[str, list] = defaultdict(list)
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            if line.startswith("#") or not line.strip():
                continue
            c = line.rstrip("\n").split("\t")
            assert len(c) == 9, f"GTF 第 {lineno} 行字段数 {len(c)}"
            if c[2] != "exon":
                continue
            m = re.search(r'gene_id "([^"]+)"', c[8])
            assert m, f"GTF 第 {lineno} 行无 gene_id"
            gene = m.group(1)
            s, e = int(c[3]) - 1, int(c[4])  # 1-based 包含 -> 0-based 半开
            if gene in genes:
                assert genes[gene]["chrom"] == c[0], f"{gene} 染色体不一致"
                assert genes[gene]["strand"] == c[6], f"{gene} 链不一致"
            else:
                genes[gene] = {"chrom": c[0], "strand": c[6], "bases": set()}
            raw_intervals[gene].append((s, e))
    for gene, ivs in raw_intervals.items():  # 逐碱基展开（仅小范围夹具使用）
        bases = genes[gene]["bases"]
        for s, e in ivs:
            bases.update(range(s, e))
    return genes


# ------------------------------------------------------------------ 自带 CIGAR 解码
def ref_coverage_bases(pos0: int, cigar: str, seq_len: int):
    """逐操作推导参考覆盖碱基集合，同时做合法性与消耗校验。"""
    tokens = CIGAR_TOKEN.findall(cigar)
    assert "".join(n + op for n, op in tokens) == cigar, f"CIGAR 非法: {cigar}"
    ref, query, used_ref = pos0, 0, 0
    bases = set()
    for n_txt, op in tokens:
        n = int(n_txt)
        assert n > 0, f"CIGAR 长度非正: {cigar}"
        if op in COVER_OPS:
            bases.update(range(ref, ref + n))
            ref += n
            query += n
            used_ref += n
        elif op in ("D", "N"):
            ref += n
            used_ref += n
        elif op in ("I", "S"):
            query += n
        elif op in ("H", "P"):
            pass
        else:  # pragma: no cover - 正则已限定操作集合
            raise AssertionError(f"未知操作 {op}")
    assert used_ref > 0, f"CIGAR 不消耗参考位置: {cigar}"
    assert query == seq_len, f"CIGAR 读段消耗 {query} != SEQ {seq_len}: {cigar}"
    return bases


# ------------------------------------------------------------------ 自带 SAM 读取
def ref_iter_sam(path: Path):
    with open(path, encoding="utf-8") as fh:
        ordinal = 0
        for line in fh:
            if line.startswith("@") or not line.strip():
                continue
            ordinal += 1
            c = line.rstrip("\n").split("\t")
            assert len(c) >= 11, f"第 {ordinal} 条记录字段不足"
            nh = 1
            for tag in c[11:]:
                k, _typ, v = tag.split(":", 2)
                if k == "NH":
                    nh = int(v)
            yield {
                "qname": c[0], "flag": int(c[1]), "rname": None if c[2] == "*" else c[2],
                "pos0": None if c[3] == "0" else int(c[3]) - 1,
                "mapq": int(c[4]), "cigar": c[5],
                "seq_len": 0 if c[9] == "*" else len(c[9]), "nh": nh, "ordinal": ordinal,
            }


def ref_iter_bam(path: Path):
    """BAM 只用 pysam 枚举；flag/CIGAR 文本/SEQ 长度解释仍走本文件自己的逻辑。"""
    import pysam
    with pysam.AlignmentFile(str(path), "rb") as bam:
        ordinal = 0
        for aln in bam.fetch(until_eof=True):
            ordinal += 1
            tags = dict(aln.get_tags())
            yield {
                "qname": aln.query_name, "flag": int(aln.flag),
                "rname": aln.reference_name,
                "pos0": None if aln.is_unmapped else int(aln.reference_start),
                "mapq": int(aln.mapping_quality),
                "cigar": aln.cigarstring or "*",
                "seq_len": 0 if aln.query_length is None else int(aln.query_length),
                "nh": int(tags.get("NH", 1)), "ordinal": ordinal,
            }


def ref_iter(path: Path):
    if str(path).lower().endswith(".bam"):
        yield from ref_iter_bam(path)
    else:
        yield from ref_iter_sam(path)


# ------------------------------------------------------------------ 自带分组与判定
def ref_classify(records, genes, library, min_mapq):
    primaries = [r for r in records if not (r["flag"] & (SECONDARY | SUPP))]
    ignored = [r for r in records if r["flag"] & (SECONDARY | SUPP)]
    if not primaries:
        return IGNORED_ONLY, [], None, set(), len(ignored)

    paired_flags = {bool(r["flag"] & PAIRED) for r in primaries}
    malformed = []
    if len(paired_flags) != 1:
        malformed.append("配对标志矛盾")
    is_paired = next(iter(paired_flags)) if len(paired_flags) == 1 else bool(primaries[0]["flag"] & PAIRED)
    firsts = [r for r in primaries if r["flag"] & FIRST]
    lasts = [r for r in primaries if r["flag"] & LAST]
    if is_paired:
        if len(primaries) != 2 or len(firsts) != 1 or len(lasts) != 1:
            malformed.append("配对结构非法（缺端/重复端）")
    elif len(primaries) != 1 or any(r["flag"] & (FIRST | LAST) for r in primaries):
        malformed.append("未配对记录结构非法")
    for r in primaries:
        unmapped = bool(r["flag"] & UNMAPPED)
        if unmapped:
            if r["rname"] is not None or r["pos0"] is not None or r["cigar"] != "*":
                malformed.append("未比对记录携带位置")
        elif r["rname"] is None or r["pos0"] is None or r["cigar"] == "*":
            malformed.append("已比对记录缺位置")
        if r["nh"] < 1:
            malformed.append("NH 非法")

    cover: dict[int, set] = {}
    for r in primaries:
        if not (r["flag"] & UNMAPPED):
            try:
                cover[id(r)] = ref_coverage_bases(r["pos0"], r["cigar"], r["seq_len"])
            except AssertionError as e:
                malformed.append(str(e))
    if malformed:
        return "malformed", malformed, None, set(), len(ignored)

    direction_rec = firsts[0] if is_paired else primaries[0]
    orientation = "-" if direction_rec["flag"] & REVERSE else "+"

    def first_hit_status():
        if any(r["flag"] & UNMAPPED for r in primaries):
            return "unmapped"
        if any(r["flag"] & DUP for r in primaries):
            return "duplicate"
        if any(r["nh"] > 1 for r in primaries):
            return "multimapping"
        if any(r["mapq"] < min_mapq for r in primaries):
            return "low_mapq"
        if is_paired and firsts[0]["rname"] != lasts[0]["rname"]:
            return "chimeric"
        return None

    early = first_hit_status()
    if early:
        return early, [f"参考解释器判定 {early}"], None, set(), len(ignored)

    merged: set = set()
    for r in primaries:
        merged |= cover[id(r)]
    chrom = direction_rec["rname"]
    candidates = set()
    for gene, model in genes.items():
        if model["chrom"] != chrom or not (model["bases"] & merged):
            continue
        if library == "forward" and model["strand"] != orientation:
            continue
        if library == "reverse" and model["strand"] == orientation:
            continue
        candidates.add(gene)
    if not candidates:
        return "no_feature", ["参考解释器：无候选"], orientation, set(), len(ignored)
    if len(candidates) > 1:
        return "ambiguous", ["参考解释器：多候选"], orientation, candidates, len(ignored)
    gene = next(iter(candidates))
    return "assigned", ["参考解释器：唯一候选"], orientation, candidates, len(ignored), gene


def ref_count(alignment: Path, genes, library, min_mapq):
    counts = {g: 0 for g in genes}
    summary = {s: 0 for s in STATUS_ORDER}
    summary[IGNORED_ONLY] = 0
    per_fragment = {}
    current, group, prev_bytes = None, [], None
    for rec in ref_iter(alignment):
        key = rec["qname"]
        if current is not None and key != current:
            assert key.encode() >= prev_bytes, "参考解释器检测到 QNAME 乱序"
            verdict = ref_classify(group, genes, library, min_mapq)
            _absorb(verdict, current, counts, summary, per_fragment)
            group = []
        current, prev_bytes = key, key.encode()
        group.append(rec)
    if group:
        verdict = ref_classify(group, genes, library, min_mapq)
        _absorb(verdict, current, counts, summary, per_fragment)
    return counts, summary, per_fragment


def _absorb(verdict, qname, counts, summary, per_fragment):
    status = verdict[0]
    summary[status] += 1
    per_fragment[qname] = verdict
    if status == "assigned":
        counts[verdict[5]] += 1


# ------------------------------------------------------------------ 交叉核对
def run_check(gtf: Path, sample_sheet: Path, out_dir: Path, min_mapq: int) -> int:
    genes = ref_load_gtf(gtf)
    specs = json.loads((out_dir / "run.json").read_text(encoding="utf-8"))["samples"]
    if str(sample_sheet).endswith(".json"):
        sheet = json.loads(Path(sample_sheet).read_text(encoding="utf-8"))["samples"]
        sheet_map = {s["sample"]: s for s in sheet}
    else:
        with open(sample_sheet, newline="", encoding="utf-8") as fh:
            sheet_map = {r["sample"]: r for r in csv.DictReader(fh, delimiter="\t")}
    base = Path(sample_sheet).parent

    failures = []
    ref_matrix = {}
    for meta in specs:
        name = meta["sample"]
        align_path = (base / sheet_map[name]["alignment"]).resolve()
        counts, summary, per_frag = ref_count(align_path, genes, meta["library"], min_mapq)
        ref_matrix[name] = counts

        # 1) 与生产 summary.csv 逐状态核对
        with open(out_dir / "summary.csv", encoding="utf-8") as fh:
            prod_summary = {row["sample"]: row for row in csv.DictReader(fh)}
        ps = prod_summary[name]
        for status in (*STATUS_ORDER, IGNORED_ONLY):
            if int(ps[status]) != summary[status]:
                failures.append(
                    f"{name} 状态 {status}: 生产={ps[status]} 参考={summary[status]}")

        # 2) 与生产 fragments JSONL 逐片段状态、候选、归属核对
        prod_frags = {}
        with open(out_dir / "fragments" / f"{name}.jsonl", encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                prod_frags[row["qname"]] = row
        if set(prod_frags) != set(per_frag):
            failures.append(f"{name} 片段集合不一致: "
                            f"生产={sorted(prod_frags)} 参考={sorted(per_frag)}")
        for qname, verdict in per_frag.items():
            row = prod_frags.get(qname)
            if row is None:
                continue
            ref_status = verdict[0]
            if row["status"] != ref_status:
                failures.append(f"{name}/{qname} 状态: 生产={row['status']} 参考={ref_status}")
                continue
            if ref_status == "assigned":
                if row["assigned_gene"] != verdict[5]:
                    failures.append(f"{name}/{qname} 归属基因不一致")
            if ref_status in ("ambiguous", "assigned"):
                ref_cand = verdict[3]
                if set(row["candidate_genes"]) != ref_cand:
                    failures.append(
                        f"{name}/{qname} 候选不一致: 生产={row['candidate_genes']} 参考={sorted(ref_cand)}")

    # 3) 与 counts.csv 逐基因核对（含零计数基因）
    with open(out_dir / "counts.csv", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        prod_samples = header[1:]
        matrix_genes = set()
        for row in reader:
            gene = row[0]
            matrix_genes.add(gene)
            for name, val in zip(prod_samples, row[1:]):
                if int(val) != ref_matrix[name][gene]:
                    failures.append(
                        f"矩阵 {gene}/{name}: 生产={val} 参考={ref_matrix[name][gene]}")
    missing = set(genes) - matrix_genes
    if missing:
        failures.append(f"矩阵缺少基因（含零计数）: {sorted(missing)}")

    # 4) 矩阵总和 == assigned
    for name in ref_matrix:
        if sum(ref_matrix[name].values()) != summary_of(out_dir, name, "assigned"):
            failures.append(f"{name} 矩阵总和 != assigned")

    if failures:
        print("独立交叉核对失败：", file=sys.stderr)
        for f in failures:
            print("  -", f, file=sys.stderr)
        return 1
    print(f"独立逐碱基解释器核对通过：{len(specs)} 个样本、{len(genes)} 个基因，"
          "逐片段状态/候选/归属与矩阵全部一致")
    return 0


def summary_of(out_dir: Path, sample: str, status: str) -> int:
    with open(out_dir / "summary.csv", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["sample"] == sample:
                return int(row[status])
    raise KeyError(sample)


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("用法: run_rnaseq_reference_check.py <gtf> <sample_sheet.json> <out_dir>")
    gtf, sheet, out = map(Path, sys.argv[1:4])
    sys.exit(run_check(gtf, sheet, out, min_mapq=10))
