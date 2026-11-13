"""CIGAR 校验与参考覆盖块推导。

操作语义（参考位置推进 / 读段位置推进 / 是否贡献参考覆盖）::

    M = X ：推进参考、推进读段、**贡献覆盖**
    D     ：推进参考、不推进读段、不贡献覆盖（删除，参考上的碱基无读段证据）
    N     ：推进参考、不推进读段、不贡献覆盖（跨过内含子）
    I     ：不推进参考、推进读段、不贡献覆盖
    S     ：不推进参考、推进读段、不贡献覆盖（软裁剪）
    H = P ：均不推进（硬裁剪/填充），不贡献覆盖

因此一条比对真正贡献证据的参考位置是所有 M/=/X 操作的并集；绝不能把
``reference_start`` 到 ``reference_end`` 整段当覆盖，否则内含子（N）里的基因
会被误计。

:func:`coverage_blocks` 同时校验操作串合法性与各类消耗长度，非法记录抛
:class:`CigarError`，由计数主流程归入 ``malformed`` 并保留定位信息。
"""
from __future__ import annotations

import re

# 参考推进 / 读段推进 / 贡献覆盖
_REF_CONSUMING = frozenset("MDN=X")
_QUERY_CONSUMING = frozenset("MIS=X")
_COVERING = frozenset("M=X")
_CLIP = frozenset("SH")
_CIGAR_RE = re.compile(r"(\d+)([MIDNSHP=X])")


class CigarError(ValueError):
    """CIGAR 操作串不合法或与读段/参考消耗长度不一致。"""


def parse_cigar(cigar: str):
    """解析文本 CIGAR（如 ``10M2D5M``）为 ``[(len, op), ...]``。

    空串或含无法识别的操作/长度均抛 :class:`CigarError`。
    """
    if not cigar or cigar == "*":
        raise CigarError(f"缺少 CIGAR 操作串: {cigar!r}")
    pieces = _CIGAR_RE.findall(cigar)
    joined = "".join(n + op for n, op in pieces)
    if joined != cigar:
        raise CigarError(f"CIGAR 含无法识别的操作或格式错误: {cigar!r}")
    ops = [(int(n), op) for n, op in pieces]
    if any(n <= 0 for n, _ in ops):
        raise CigarError(f"CIGAR 操作长度必须为正整数: {cigar!r}")
    return ops


def coverage_blocks(reference_start: int, cigar: str,
                    query_length: int | None = None) -> list[tuple[int, int]]:
    """从比对起点与 CIGAR 推导**贡献覆盖**的参考半开块列表（已排序、互不相交）。

    ``query_length`` 给定时校验读段消耗（M/I/S/=X 之和）与之相等；
    H 与 P 不参与任何消耗。参考消耗必须为正（至少一个 M/=/X/D/N）。
    """
    if isinstance(reference_start, bool) or not isinstance(reference_start, int) \
            or reference_start < 0:
        raise CigarError(f"比对起点非法: {reference_start!r}")
    ops = parse_cigar(cigar)
    # 与 pysam 路径一致：S/H 只能属于首部或尾部的连续裁剪块
    n_ops = len(ops)
    first_core = 0
    while first_core < n_ops and ops[first_core][1] in ("S", "H"):
        first_core += 1
    last_core = n_ops - 1
    while last_core >= first_core and ops[last_core][1] in ("S", "H"):
        last_core -= 1
    if any(op in ("S", "H") for _n, op in ops[first_core:last_core + 1]):
        raise CigarError("裁剪操作 S/H 只允许出现在 CIGAR 两端的连续裁剪块中")
    ref_pos = reference_start
    query_used = 0
    ref_used = 0
    blocks: list[tuple[int, int]] = []
    for length, op in ops:
        if op in _COVERING:
            blocks.append((ref_pos, ref_pos + length))
            ref_pos += length
            query_used += length
            ref_used += length
        elif op in ("D", "N"):
            ref_pos += length
            ref_used += length
        elif op in ("I", "S"):
            query_used += length
        # H、P：什么都不推进
    if ref_used <= 0:
        raise CigarError(f"CIGAR 不消耗任何参考位置，无法定位比对: {cigar!r}")
    if query_length is not None and query_used != query_length:
        raise CigarError(
            f"CIGAR 读段消耗 {query_used} 与 SEQ 长度 {query_length} 不一致: {cigar!r}")
    return blocks


def pysam_coverage(alignment) -> list[tuple[int, int]]:
    """pysam AlignedSegment 的等价实现：先校验再由 CIGAR 元组推导覆盖块。

    校验项：操作集合合法、参考/读段消耗与记录声明一致、裁剪只出现在两端。
    """
    cig = alignment.cigartuples
    if not cig:
        raise CigarError("记录缺少 CIGAR（cigartuples 为空）")
    ref_pos = alignment.reference_start  # 0-based 半开起点
    if ref_pos is None or ref_pos < 0:
        raise CigarError(f"记录 reference_start 非法: {ref_pos!r}")
    query_used = 0
    ref_used = 0
    blocks: list[tuple[int, int]] = []
    n = len(cig)
    for i, (code, length) in enumerate(cig):
        if length <= 0:
            raise CigarError(f"CIGAR 操作长度必须为正: op={code} len={length}")
        # pysam/htslib 操作码：M=0 I=1 D=2 N=3 S=4 H=5 P=6 =7 X=8 B=9
        if code in (0, 7, 8):                     # M = X
            blocks.append((ref_pos, ref_pos + length))
            ref_pos += length
            query_used += length
            ref_used += length
        elif code in (2, 3):                      # D N
            ref_pos += length
            ref_used += length
        elif code in (1, 4):                      # I S
            query_used += length
        elif code in (5, 6):                      # H P
            pass
        else:
            raise CigarError(f"未知 CIGAR 操作码: {code}（B=9 不允许出现在记录 CIGAR 中）")
    # 裁剪（S/H）只能出现在首部或尾部的连续裁剪块中：允许开头/结尾多个连续
    # 裁剪操作（如 5H3S10M2I5S），但内部位置一旦出现 S/H 即非法。
    n = len(cig)
    first_core = 0
    while first_core < n and cig[first_core][0] in (4, 5):
        first_core += 1
    last_core = n - 1
    while last_core >= first_core and cig[last_core][0] in (4, 5):
        last_core -= 1
    for i in range(first_core, last_core + 1):
        if cig[i][0] in (4, 5):
            raise CigarError("裁剪操作 S/H 只允许出现在 CIGAR 两端的连续裁剪块中")
    if ref_used <= 0:
        raise CigarError("CIGAR 不消耗任何参考位置")
    declared_query = alignment.query_length
    if declared_query is not None and declared_query >= 0 and query_used != declared_query:
        # pysam 的 query_length 只计 query-consuming（M/I/S/=X），H 不计
        raise CigarError(
            f"CIGAR 读段消耗 {query_used} 与 query_length {declared_query} 不一致")
    declared_end = alignment.reference_end
    if declared_end is not None and ref_pos != declared_end:
        raise CigarError(
            f"CIGAR 参考消耗终点 {ref_pos} 与 reference_end {declared_end} 不一致")
    return blocks


def merge_blocks(blocks: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并若干记录的覆盖块：双端重叠位置只算一次，相邻块也合并。"""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(blocks):
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged
