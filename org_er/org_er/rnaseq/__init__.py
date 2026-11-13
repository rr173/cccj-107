"""RNA-seq 片段计数子包：GTF 注释 -> 按 QNAME 排序的 SAM/BAM -> gene×sample 整数计数矩阵。

设计约束（见 scripts/run_rnaseq_demo.py 与 README）：

- 一条双端片段无论多少条 primary 记录，最多贡献 1 次计数；
- 覆盖位置由 CIGAR 逐操作推导，剪接间隙（N）与删除（D）不贡献覆盖；
- 基因间允许外显子交叠，歧义片段整体记 ambiguous，不按长度投票、不分摊小数；
- 注释只存合并后的半开区间与区间树，存储与染色体长度无关，大坐标稀疏注释同样轻量。
"""

STATUSES = (
    "malformed", "unmapped", "duplicate", "multimapping", "low_mapq",
    "chimeric", "no_feature", "ambiguous", "assigned",
)
"""互斥去向（判定优先级即此元组顺序）。"""

IGNORED_ONLY = "ignored_only"
"""整个 QNAME 组没有任何 primary 记录时的单列状态，不计入正常片段去向。"""

LIBRARY_MODES = ("unstranded", "forward", "reverse")
