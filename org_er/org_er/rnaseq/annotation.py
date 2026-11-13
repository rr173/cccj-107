"""GTF 注释解析：exon 行 -> 每基因按 (chrom, strand) 合并的外显子并集 -> 区间索引。

GTF 约定：坐标 1-based、两端包含。本模块内部统一为 0-based 半开
（``start0 = gtf_start - 1``，``end0 = gtf_end``）。

规则：

- 只看第三列为 ``exon`` 的行，基因由属性中的 ``gene_id`` 定义；
- 同一基因的外显子先按染色体、链方向分别取并集（重复列出、转录本间重叠都合并）；
- 同一 gene_id 出现在不一致的染色体或链上 -> :class:`AnnotationError`，整体拒绝；
- 基因之间允许外显子交叠，索引查询返回**全部**命中基因，绝不按行序选一个。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .intervaltree import IntervalIndex


class AnnotationError(ValueError):
    """注释文件不合法（字段损坏、gene_id 缺失或基因的染色体/链不一致等）。"""


@dataclass(frozen=True)
class GeneModel:
    gene_id: str
    chrom: str
    strand: str  # '+' / '-'
    exons: tuple[tuple[int, int], ...]  # 合并后的 0-based 半开区间，升序、互不相交


@dataclass
class Annotation:
    genes: dict[str, GeneModel]
    _indexes: dict[str, IntervalIndex] = field(default_factory=dict)

    def gene_ids_sorted(self) -> list[str]:
        return sorted(self.genes)

    def index(self, chrom: str) -> IntervalIndex | None:
        return self._indexes.get(chrom)

    def candidate_genes(self, chrom: str, blocks: list[tuple[int, int]]) -> set[str]:
        """返回覆盖位置与给定比对块（半开）有任一碱基交叠的全部 gene_id。"""
        idx = self._indexes.get(chrom)
        if idx is None or not blocks:
            return set()
        hits: set[str] = set()
        for bstart, bend in blocks:
            for gene_id in idx.overlap_query(bstart, bend):
                hits.add(gene_id)
        return hits


# ---------------------------------------------------------------- GTF 属性解析
def _parse_attributes(text: str, lineno: int) -> dict[str, str]:
    attrs: dict[str, str] = {}
    # GTF/GFF2: key "value";  允许多个属性以分号分隔，值必须带双引号
    tokens = [tok for tok in text.strip().split(";") if tok.strip()]
    for tok in tokens:
        pieces = tok.strip().split(" ", 1)
        if len(pieces) != 2:
            raise AnnotationError(f"GTF 第 {lineno} 行属性无法解析: {tok!r}")
        key, value = pieces
        value = value.strip()
        if len(value) < 2 or not (value.startswith('"') and value.endswith('"')):
            raise AnnotationError(f"GTF 第 {lineno} 行属性 {key!r} 的值未加双引号: {value!r}")
        attrs[key.strip()] = value[1:-1]
    return attrs


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:  # 相邻（start == prev end）也合并
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def load_gtf(path) -> Annotation:
    """读取 GTF，返回 :class:`Annotation`；任何结构问题抛 :class:`AnnotationError`。"""
    raw: dict[str, dict] = {}
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as e:
        raise AnnotationError(f"无法打开 GTF: {path}: {e}") from e
    with fh:
        for lineno, line in enumerate(fh, 1):
            if not line.strip() or line.startswith("#"):
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) != 9:
                raise AnnotationError(
                    f"GTF 第 {lineno} 行应有 9 个制表符分隔字段，实际 {len(cols)}")
            if cols[2] != "exon":
                continue
            chrom, _source, feature, gstart, gend, _score, strand, _frame, attr_text = cols
            if strand not in ("+", "-"):
                raise AnnotationError(
                    f"GTF 第 {lineno} 行 exon 的 strand 必须是 + 或 -，实际 {strand!r}")
            try:
                start1 = int(gstart)
                end1 = int(gend)
            except ValueError as e:
                raise AnnotationError(f"GTF 第 {lineno} 行坐标不是整数: {gstart!r},{gend!r}") from e
            if start1 < 1 or end1 < start1:
                raise AnnotationError(
                    f"GTF 第 {lineno} 行坐标非法（1-based 且 start<=end）: {start1},{end1}")
            attrs = _parse_attributes(attr_text, lineno)
            gene_id = attrs.get("gene_id")
            if not gene_id:
                raise AnnotationError(f"GTF 第 {lineno} 行 exon 缺少 gene_id 属性")
            rec = raw.get(gene_id)
            if rec is None:
                raw[gene_id] = {"chrom": chrom, "strand": strand,
                                "exons": [(start1 - 1, end1)]}
            else:
                if rec["chrom"] != chrom:
                    raise AnnotationError(
                        f"gene_id {gene_id!r} 出现在不一致的染色体上: "
                        f"{rec['chrom']!r} vs {chrom!r}（第 {lineno} 行）")
                if rec["strand"] != strand:
                    raise AnnotationError(
                        f"gene_id {gene_id!r} 出现在不一致的链上: "
                        f"{rec['strand']!r} vs {strand!r}（第 {lineno} 行）")
                rec["exons"].append((start1 - 1, end1))

    genes: dict[str, GeneModel] = {}
    by_chrom: dict[str, list[tuple[int, int, str]]] = {}
    for gene_id, rec in raw.items():
        merged = tuple(_merge_intervals(rec["exons"]))
        model = GeneModel(gene_id, rec["chrom"], rec["strand"], merged)
        genes[gene_id] = model
        for start, end in merged:
            by_chrom.setdefault(rec["chrom"], []).append((start, end, gene_id))

    indexes = {chrom: IntervalIndex(ivs) for chrom, ivs in by_chrom.items()}
    if not genes:
        raise AnnotationError("GTF 中没有任何 exon 行，注释为空")
    return Annotation(genes=genes, _indexes=indexes)
