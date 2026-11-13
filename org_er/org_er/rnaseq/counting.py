"""按 QNAME 分组的片段级基因计数引擎。

输入：:class:`~org_er.rnaseq.annotation.Annotation` 与按 **QNAME 字节序**排序的
SAM/BAM 记录流。输出每个片段的互斥去向、候选基因、判定所依据的比对块，以及
gene×sample 整数计数。

关键不变量：

- 记录流只保留当前 QNAME 组，组结束立即判定，不缓存全文件；
- 一个 QNAME 最终只产生一项结果；双端片段合并覆盖后至多给一个基因 +1；
- 去向互斥且优先级固定：malformed > unmapped > duplicate > multimapping >
  low_mapq > chimeric > no_feature > ambiguous > assigned；
- 没有 primary 记录的组单列 ``ignored_only``，不与上述去向混淆。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import IGNORED_ONLY, LIBRARY_MODES, STATUSES
from .annotation import Annotation
from .cigar import CigarError, coverage_blocks, merge_blocks, pysam_coverage


class CountingError(ValueError):
    """计数作业级错误（样本表、参数等）。"""


class InputOrderError(CountingError):
    """输入未按 QNAME 字节序排列：立即终止，不做隐式重排。"""


class AlignmentReadError(CountingError):
    """SAM/BAM 读取失败或记录结构损坏，带文件与记录定位信息。"""


# ------------------------------------------------------------------ 记录适配
@dataclass
class SamRecord:
    """SAM 文本记录与 pysam BAM 记录的统一视图（坐标均为 0-based 半开）。"""

    qname: str
    flag: int
    rname: str | None
    pos0: int | None
    mapq: int
    cigar: str
    seq_len: int
    nh: int
    ordinal: int
    rnext: str | None = None
    pnext0: int | None = None
    _alignment: object = None  # BAM 路径保留原对象，供 pysam 侧独立校验

    @property
    def paired(self) -> bool:
        return bool(self.flag & 0x1)

    @property
    def unmapped(self) -> bool:
        return bool(self.flag & 0x4)

    @property
    def mate_unmapped(self) -> bool:
        return bool(self.flag & 0x8)

    @property
    def reverse(self) -> bool:
        return bool(self.flag & 0x10)

    @property
    def first(self) -> bool:
        return bool(self.flag & 0x40)

    @property
    def last(self) -> bool:
        return bool(self.flag & 0x80)

    @property
    def secondary(self) -> bool:
        return bool(self.flag & 0x100)

    @property
    def duplicate(self) -> bool:
        return bool(self.flag & 0x400)

    @property
    def supplementary(self) -> bool:
        return bool(self.flag & 0x800)


def coverage_of(rec: SamRecord) -> list[tuple[int, int]]:
    """推导一条已比对 primary 记录的参考覆盖块（非法 CIGAR 抛 CigarError）。"""
    if rec._alignment is not None:
        return pysam_coverage(rec._alignment)
    return coverage_blocks(rec.pos0, rec.cigar, rec.seq_len)


# ------------------------------------------------------------------ SAM 文本读取
def _parse_sam_line(line: str, ordinal: int) -> SamRecord:
    cols = line.rstrip("\n").split("\t")
    if len(cols) < 11:
        raise AlignmentReadError(
            f"SAM 第 {ordinal} 条记录只有 {len(cols)} 个字段（至少需要 11 个）")
    try:
        flag = int(cols[1])
        pos1 = int(cols[3])
        mapq = int(cols[4])
    except ValueError as e:
        raise AlignmentReadError(f"SAM 第 {ordinal} 条记录的 FLAG/POS/MAPQ 不是整数") from e
    if flag < 0 or flag > 0xFFFF or mapq < 0 or mapq > 255:
        raise AlignmentReadError(f"SAM 第 {ordinal} 条记录 FLAG/MAPQ 超出合法范围")
    qname, rname, cigar, rnext, pnext1 = cols[0], cols[2], cols[5], cols[6], cols[7]
    if not qname:
        raise AlignmentReadError(f"SAM 第 {ordinal} 条记录 QNAME 为空")
    try:
        pnext = int(pnext1)
    except ValueError as e:
        raise AlignmentReadError(f"SAM 第 {ordinal} 条记录 PNEXT 不是整数") from e
    nh = 1
    for tag in cols[11:]:
        parts = tag.split(":", 2)
        if len(parts) != 3:
            raise AlignmentReadError(f"SAM 第 {ordinal} 条记录 TAG 字段损坏: {tag!r}")
        if parts[0] == "NH":
            try:
                nh = int(parts[2])
            except ValueError as e:
                raise AlignmentReadError(
                    f"SAM 第 {ordinal} 条记录 NH 标签不是整数: {parts[2]!r}") from e
    return SamRecord(
        qname=qname, flag=flag,
        rname=None if rname == "*" else rname,
        pos0=None if pos1 == 0 else pos1 - 1,
        mapq=mapq, cigar=cigar,
        seq_len=0 if cols[9] == "*" else len(cols[9]),
        nh=nh, ordinal=ordinal,
        rnext=None if rnext == "*" else rnext,
        pnext0=None if pnext == 0 else pnext - 1,
    )


def iter_sam_records(path):
    """逐行读取 SAM 文本（跳过 @ 头），保持流式，记录序号从 1 开始。"""
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError as e:
        raise AlignmentReadError(f"无法打开 SAM {path}: {e}") from e
    with fh:
        ordinal = 0
        for line in fh:
            if line.startswith("@"):
                continue
            if not line.strip():
                continue
            ordinal += 1
            yield _parse_sam_line(line, ordinal)


def iter_bam_records(path):
    """通过 pysam 流式读取 BAM，统一成 :class:`SamRecord`；读取错误带定位信息。"""
    try:
        import pysam
    except ImportError as e:  # pragma: no cover
        raise CountingError("读取 BAM 需要 pysam，请先安装（requirements-rnaseq.txt）") from e
    try:
        bam = pysam.AlignmentFile(path, "rb", require_index=False)
    except (ValueError, OSError) as e:
        raise AlignmentReadError(f"BAM 无法打开 {path}: {e}") from e
    ordinal = 0
    with bam:
        try:
            for aln in bam.fetch(until_eof=True):
                ordinal += 1
                try:
                    qname = aln.query_name
                    if isinstance(qname, bytes):
                        qname = qname.decode("utf-8")
                    nh = dict(aln.get_tags()).get("NH", 1) if aln.get_tags() else 1
                    yield SamRecord(
                        qname=qname, flag=int(aln.flag),
                        rname=aln.reference_name,
                        pos0=None if aln.is_unmapped else int(aln.reference_start),
                        mapq=int(aln.mapping_quality),
                        cigar=aln.cigarstring or "*",
                        seq_len=0 if aln.query_length is None else int(aln.query_length),
                        nh=int(nh), ordinal=ordinal,
                        rnext=aln.next_reference_name,
                        pnext0=None if aln.next_reference_start < 0
                        else int(aln.next_reference_start),
                        _alignment=aln,
                    )
                except (ValueError, TypeError) as e:
                    raise AlignmentReadError(
                        f"BAM {path} 第 {ordinal} 条记录结构损坏: {e}") from e
        except (OSError, ValueError, StopIteration) as e:
            raise AlignmentReadError(
                f"BAM {path} 在第 {ordinal} 条记录附近读取失败: {e}") from e


def iter_records(path):
    if str(path).lower().endswith(".bam"):
        yield from iter_bam_records(path)
    else:
        yield from iter_sam_records(path)


# ------------------------------------------------------------------ QNAME 分组
def iter_qname_groups(path):
    """流式产出 ``(qname, [SamRecord, ...])``，并强制字节序非递减。

    一旦发现新 QNAME 的字节序小于当前组，抛 :class:`InputOrderError`，
    保留两个名字与记录序号，调用方必须以非零状态结束，不允许隐式重排。
    """
    current: str | None = None
    group: list[SamRecord] = []
    for rec in iter_records(path):
        if current is None:
            current, group = rec.qname, [rec]
            continue
        if rec.qname == current:
            group.append(rec)
            continue
        if rec.qname.encode("utf-8") < current.encode("utf-8"):
            raise InputOrderError(
                f"{path}: 记录未按 QNAME 字节序排列：第 {rec.ordinal} 条 {rec.qname!r} "
                f"排在 {current!r} 之后（字节序应非递减）；请先用 samtools sort -n 排序，"
                "本工具不会隐式重排")
        yield current, group
        current, group = rec.qname, [rec]
    if group:
        yield current, group


# ------------------------------------------------------------------ 单组判定
@dataclass
class FragmentResult:
    qname: str
    status: str
    reasons: list[str] = field(default_factory=list)
    candidate_genes: list[str] = field(default_factory=list)
    assigned_gene: str | None = None
    orientation: str | None = None
    merged_blocks: list[tuple[int, int]] = field(default_factory=list)
    mates: list[dict] = field(default_factory=list)
    ignored: list[dict] = field(default_factory=list)
    record_ordinals: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "qname": self.qname,
            "status": self.status,
            "reasons": self.reasons,
            "candidate_genes": self.candidate_genes,
            "assigned_gene": self.assigned_gene,
            "orientation": self.orientation,
            "merged_coverage_blocks": [[s, e] for s, e in self.merged_blocks],
            "primary_records": self.mates,
            "ignored_records": self.ignored,
            "record_ordinals": self.record_ordinals,
        }


def _mate_dict(rec: SamRecord, role: str) -> dict:
    return {
        "role": role,
        "ordinal": rec.ordinal,
        "flag": rec.flag,
        "rname": rec.rname,
        "pos_0based": rec.pos0,
        "mapq": rec.mapq,
        "nh": rec.nh,
        "reverse": rec.reverse,
        "unmapped": rec.unmapped,
        "duplicate": rec.duplicate,
        "cigar": rec.cigar,
    }


def classify_group(qname: str, recs: list[SamRecord], annotation: Annotation,
                   library_mode: str, min_mapq: int) -> FragmentResult:
    if library_mode not in LIBRARY_MODES:
        raise CountingError(f"未知文库模式 {library_mode!r}，允许值: {LIBRARY_MODES}")
    result = FragmentResult(qname=qname, status="", record_ordinals=[r.ordinal for r in recs])

    primaries, ignored = [], []
    for rec in recs:
        if rec.secondary or rec.supplementary:
            tags = []
            if rec.secondary:
                tags.append("secondary")
            if rec.supplementary:
                tags.append("supplementary")
            ignored.append({"ordinal": rec.ordinal, "flags": tags, "rname": rec.rname})
        else:
            primaries.append(rec)
    result.ignored = ignored

    if not primaries:
        result.status = IGNORED_ONLY
        result.reasons.append("组内没有 primary 记录（仅 secondary/supplementary，已全部忽略）")
        return result

    # ------------------------------------------------------------ 结构校验 -> malformed
    structure_errors: list[str] = []
    paired_flags = {r.paired for r in primaries}
    if len(paired_flags) != 1:
        structure_errors.append("primary 记录的 0x1 配对标志互相矛盾")
    is_paired = paired_flags.pop() if len(paired_flags) == 1 else primaries[0].paired

    firsts = [r for r in primaries if r.first]
    lasts = [r for r in primaries if r.last]
    if is_paired:
        if len(primaries) != 2 or len(firsts) != 1 or len(lasts) != 1 \
                or any(r.first == r.last for r in primaries):
            structure_errors.append(
                f"配对片段的 primary 结构非法（共 {len(primaries)} 条，"
                f"first-mate {len(firsts)} 条，second-mate {len(lasts)} 条；"
                "缺端/重复端/标志矛盾）")
    else:
        if len(primaries) != 1:
            structure_errors.append(
                f"未声明配对的 QNAME 出现 {len(primaries)} 条 primary 记录")
        if any(r.first or r.last for r in primaries):
            structure_errors.append("未声明配对（无 0x1）的记录却携带 first/second-mate 标志")

    for rec in primaries:
        if rec.unmapped:
            if rec.rname is not None or rec.pos0 is not None or rec.cigar != "*":
                structure_errors.append(
                    f"第 {rec.ordinal} 条记录声明未比对却携带 RNAME/POS/CIGAR")
        else:
            if rec.rname is None or rec.pos0 is None or rec.cigar == "*":
                structure_errors.append(
                    f"第 {rec.ordinal} 条记录声明已比对却缺少 RNAME/POS/CIGAR")
        if rec.nh < 1:
            structure_errors.append(f"第 {rec.ordinal} 条记录 NH={rec.nh} 非法（须 >=1）")

    if structure_errors:
        result.status = "malformed"
        result.reasons.extend(structure_errors)
        result.mates = [_mate_dict(r, _role(r, is_paired)) for r in primaries]
        return result

    if is_paired:
        first_rec = firsts[0]
        last_rec = lasts[0]
        result.mates = [_mate_dict(first_rec, "first"), _mate_dict(last_rec, "second")]
        direction_rec = first_rec  # 双端以 first-mate 方向为准
    else:
        first_rec = last_rec = primaries[0]
        result.mates = [_mate_dict(primaries[0], "unpaired")]
        direction_rec = primaries[0]
    result.orientation = "-" if direction_rec.reverse else "+"

    # CIGAR 校验（对所有已比对 primary；未比对记录无 CIGAR 可验）
    coverages: dict[int, list[tuple[int, int]]] = {}
    for rec in primaries:
        if rec.unmapped:
            continue
        try:
            coverages[id(rec)] = coverage_of(rec)
        except CigarError as e:
            result.status = "malformed"
            result.reasons.append(f"第 {rec.ordinal} 条记录 CIGAR 不合法: {e}")
            return result

    # ------------------------------------------------------------ 固定优先级的过滤链
    if any(r.unmapped for r in primaries):
        result.status = "unmapped"
        result.reasons.append("存在未比对的 primary 记录: " +
                              ", ".join(f"#{r.ordinal}" for r in primaries if r.unmapped))
        return result
    if any(r.duplicate for r in primaries):
        result.status = "duplicate"
        result.reasons.append("primary 记录带 duplicate(0x400) 标志: " +
                              ", ".join(f"#{r.ordinal}" for r in primaries if r.duplicate))
        return result
    if any(r.nh > 1 for r in primaries):
        result.status = "multimapping"
        result.reasons.append("NH>1（多比对）: " +
                              ", ".join(f"#{r.ordinal}(NH={r.nh})"
                                        for r in primaries if r.nh > 1))
        return result
    if any(r.mapq < min_mapq for r in primaries):
        result.status = "low_mapq"
        result.reasons.append(
            f"MAPQ 低于阈值 {min_mapq}: " +
            ", ".join(f"#{r.ordinal}(MAPQ={r.mapq})"
                      for r in primaries if r.mapq < min_mapq))
        return result
    if is_paired and first_rec.rname != last_rec.rname:
        result.status = "chimeric"
        result.reasons.append(
            f"双端落在不同染色体: first={first_rec.rname!r} second={last_rec.rname!r}")
        return result

    # ------------------------------------------------------------ 合并覆盖并归属
    all_blocks: list[tuple[int, int]] = []
    for rec in primaries:
        all_blocks.extend(coverages[id(rec)])
    merged = merge_blocks(all_blocks)
    result.merged_blocks = merged
    chrom = first_rec.rname
    raw_hits = annotation.candidate_genes(chrom, merged)

    def strand_ok(gene_id: str) -> bool:
        if library_mode == "unstranded":
            return True
        gene_strand = annotation.genes[gene_id].strand
        if library_mode == "forward":  # 只接受与片段方向相同的链
            return gene_strand == result.orientation
        return gene_strand != result.orientation  # reverse：相反链

    candidates = sorted(g for g in raw_hits if strand_ok(g))
    result.candidate_genes = candidates
    if not candidates:
        result.status = "no_feature"
        result.reasons.append(
            f"覆盖块未落入任何基因的外显子并集（模式={library_mode}，方向={result.orientation}）")
        return result
    if len(candidates) > 1:
        result.status = "ambiguous"
        result.reasons.append(
            f"覆盖同时命中 {len(candidates)} 个基因，整体记歧义，不分摊: "
            + ",".join(candidates))
        return result
    result.status = "assigned"
    result.assigned_gene = candidates[0]
    result.reasons.append(f"唯一候选基因 {candidates[0]}，片段计数 +1")
    return result


def _role(rec: SamRecord, is_paired: bool) -> str:
    if not is_paired:
        return "unpaired"
    return "first" if rec.first else ("second" if rec.last else "invalid")


# ------------------------------------------------------------------ 样本级汇总
def count_sample(path, annotation: Annotation, library_mode: str, min_mapq: int):
    """流式处理一个样本，返回 (计数 dict, 汇总 dict, [FragmentResult])。"""
    counts = {gene_id: 0 for gene_id in annotation.genes}
    summary = {s: 0 for s in STATUSES}
    summary[IGNORED_ONLY] = 0
    rows: list[FragmentResult] = []
    for qname, group in iter_qname_groups(path):
        res = classify_group(qname, group, annotation, library_mode, min_mapq)
        rows.append(res)
        summary[res.status] += 1
        if res.status == "assigned":
            counts[res.assigned_gene] += 1
    return counts, summary, rows
