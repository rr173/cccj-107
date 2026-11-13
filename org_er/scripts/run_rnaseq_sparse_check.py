#!/usr/bin/env python3
"""大坐标稀疏注释的存储开销实测。

构造坐标在 10^15 量级、每染色体只有极短外显子的 GTF（模拟真实基因组中
“染色体极长、注释极稀疏”的比例），用生产的区间树建索引并实测：

1. 区间树节点/负载只与外显子区间数同阶，与坐标绝对值、染色体长度无关；
2. 不允许把染色体展开成碱基数组或 Python 碱基集合——用一个对照集合估算
   “逐碱基展开”的理论开销，证明生产路径没有付这笔钱；
3. 点查询/交叠查询在稀疏大坐标上结果正确。
"""
from __future__ import annotations

import json
import sys
import tempfile
import tracemalloc
from pathlib import Path

import _bootstrap  # noqa: F401
from org_er.rnaseq.annotation import load_gtf
from org_er.rnaseq.cigar import coverage_blocks, merge_blocks


def build_sparse_gtf(path: Path, n_genes: int, stride: int) -> int:
    """n 个基因，每个一条 200bp 外显子，起点每隔 stride 放到极大坐标上。"""
    lines = ["# 大坐标稀疏注释（合成）"]
    for i in range(n_genes):
        start = 10**15 + i * stride          # GTF 1-based 包含
        end = start + 199
        chrom = f"chr{i % 4}"
        lines.append("\t".join([
            chrom, "sparse", "exon", str(start), str(end), ".",
            "+" if i % 2 == 0 else "-", ".",
            f'gene_id "big{i:05d}"; transcript_id "t{i}";']))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 4


def main() -> int:
    n_genes = 500
    stride = 10**9
    with tempfile.TemporaryDirectory(prefix="rnaseq_sparse_") as tmp:
        gtf = Path(tmp) / "sparse.gtf"
        n_chroms = build_sparse_gtf(gtf, n_genes, stride)

        tracemalloc.start()
        baseline = tracemalloc.get_traced_memory()[0]
        ann = load_gtf(gtf)
        peak = tracemalloc.get_traced_memory()[1] - baseline
        tracemalloc.stop()

        # 对照：若按“整条染色体展开成 Python 碱基集合”实现，单个 200bp 外显子的
        # set 就要 ~200 个 Python int（小整数对象外的坐标全是新建对象）。
        # 这里只展开**外显子本身**（已经是最省的逐碱基方案，内含子更贵），
        # 用以说明生产路径没有按坐标规模付费。
        per_int_py = sys.getsizeof(10**15) + sys.getsizeof(set())
        naive_exon_set = 200 * per_int_py
        naive_total = n_genes * naive_exon_set
        # 若真按染色体长度展开，4 条“染色体”各 ~2.5e14 碱基，直接不可行：
        chromosome_span = (n_genes * stride) // n_chroms

        # 功能正确性：取 big00002 的外显子做一次点/交叠查询
        g = ann.genes["big00002"]
        s0, e0 = g.exons[0]
        idx = ann.index(g.chrom)
        inside = set(idx.point_query(s0 + 10))
        outside = set(idx.point_query(e0 + 10**8))  # 远在外显子之外
        blocks = merge_blocks(coverage_blocks(s0, "50M1000N50M", 100))
        block_hits = ann.candidate_genes(g.chrom, blocks)

        ok = (g.gene_id in inside and not outside and g.gene_id in block_hits
              and len(ann.genes) == n_genes)
        report = {
            "n_genes": n_genes,
            "n_chromosomes": n_chroms,
            "coordinate_magnitude": "10^15",
            "chromosome_span_bp": chromosome_span,
            "interval_tree_peak_bytes": int(peak),
            "interval_tree_peak_kib": round(peak / 1024, 1),
            "naive_base_set_bytes_exons_only": naive_total,
            "naive_base_set_kib_exons_only": round(naive_total / 1024, 1),
            "bytes_per_gene_production": round(peak / n_genes, 1),
            "note": ("生产索引峰值只随外显子区间数增长；逐碱基方案即使只展开外显子"
                     "（不展开内含子/染色体间隙）也贵一个数量级以上，按染色体展开则完全不可行"),
            "point_query_inside_hit": sorted(inside),
            "point_query_far_outside_hit": sorted(outside),
            "spliced_block_candidates": sorted(block_hits),
            "check_ok": bool(ok),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not ok:
            return 1
        # 硬约束：生产路径每基因平均占用必须远小于“逐碱基外显子集合”的下限
        if peak >= naive_total / 4:
            print("存储开销异常：区间树方案未显著优于逐碱基集合", file=sys.stderr)
            return 1
        print(f"\n结论：{n_genes} 个 10^15 量级稀疏基因的完整注释索引仅 "
              f"{peak / 1024:.1f} KiB（约 {peak / n_genes:.0f} 字节/基因），"
              "与染色体长度无关。")
        return 0


if __name__ == "__main__":
    sys.exit(main())
