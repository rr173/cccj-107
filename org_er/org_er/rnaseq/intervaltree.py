"""半开区间 [start, end) 上的静态区间树。

按区间中点递归建树：节点持有**覆盖该中点**的全部区间（按 start 升序、
end 降序排列，支持二分剪枝），完全落在左/右半区的区间递归进子节点。

查询与存储都只依赖区间本身：坐标可以稀疏到任意大（例如 10^15 量级），
节点数与区间数同阶，绝不按染色体长度展开数组或 Python 碱基集合。
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right


class IntervalIndex:
    """不可变区间索引：构建一次，重复点查询/交叠查询。

    每个区间是 ``(start, end, payload)``，半开、``start < end``。
    点查询返回所有满足 ``start <= point < end`` 的 payload；
    交叠查询返回所有与 ``[qstart, qend)`` 相交的 payload。
    """

    __slots__ = ("_center", "_by_start", "_by_end", "_payloads", "_left", "_right", "size")

    def __init__(self, intervals):
        """``intervals`` 为 (start, end, payload) 可迭代对象；空集合建空树。"""
        ivs = sorted(intervals, key=lambda t: (t[0], -t[1], t[2]))
        self.size = len(ivs)
        if not ivs:
            self._center = None
            self._by_start = ()
            self._by_end = ()
            self._payloads = ()
            self._left = None
            self._right = None
            return
        lo = min(iv[0] for iv in ivs)
        hi = max(iv[1] for iv in ivs)
        center = (lo + hi) // 2
        here, left, right = [], [], []
        for start, end, payload in ivs:
            if start <= center < end:
                here.append((start, end, payload))
            elif end <= center:
                left.append((start, end, payload))
            else:  # start > center
                right.append((start, end, payload))
        # here 已随 ivs 按 start 升序；另建一份按 end 降序，供点/范围查询剪枝。
        here_sorted = sorted(here, key=lambda t: (t[0], -t[1], t[2]))
        self._center = center
        self._by_start = [t[0] for t in here_sorted]
        ends_desc = sorted(here, key=lambda t: (-t[1], t[0], t[2]))
        self._by_end = [t[1] for t in ends_desc]
        self._payloads = (
            [t[2] for t in here_sorted],
            [t[2] for t in ends_desc],
        )
        self._left = IntervalIndex(left) if left else None
        self._right = IntervalIndex(right) if right else None

    # ------------------------------------------------------------ 查询
    def point_query(self, point: int):
        """返回覆盖 ``point`` 的全部 payload（可能重复，由调用方去重）。"""
        out = []
        self._point(point, out)
        return out

    def _point(self, point: int, out: list) -> None:
        if self._center is None:
            return
        if point == self._center:
            out.extend(self._payloads[0])
            return
        if point < self._center:
            # start 升序：start > point 的区间不可能覆盖 point
            starts, payloads = self._by_start, self._payloads[0]
            cut = bisect_right(starts, point)
            out.extend(payloads[:cut])
            if self._left is not None:
                self._left._point(point, out)
        else:
            # ends_desc 按 end 降序：end <= point 的区间（半开，不覆盖 point）排在尾部，
            # 手工二分定位第一个 end <= point 的位置。
            ends, payloads = self._by_end, self._payloads[1]
            lo, hi = 0, len(ends)
            while lo < hi:
                mid = (lo + hi) // 2
                if ends[mid] > point:
                    lo = mid + 1
                else:
                    hi = mid
            out.extend(payloads[:lo])
            if self._right is not None:
                self._right._point(point, out)

    def overlap_query(self, qstart: int, qend: int):
        """返回与半开区间 ``[qstart, qend)`` 相交的全部 payload（可能重复）。"""
        out = []
        self._overlap(qstart, qend, out)
        return out

    def _overlap(self, qstart: int, qend: int, out: list) -> None:
        if self._center is None:
            return
        if qstart <= self._center < qend:
            # 查询区间覆盖中点：本节点所有区间都与它相交
            out.extend(self._payloads[0])
            if self._left is not None:
                self._left._overlap(qstart, qend, out)
            if self._right is not None:
                self._right._overlap(qstart, qend, out)
            return
        if qend <= self._center:
            # 与本节点区间相交要求 start < qend；start 升序，可二分截断
            starts, payloads = self._by_start, self._payloads[0]
            cut = bisect_left(starts, qend)
            out.extend(payloads[:cut])
            if self._left is not None:
                self._left._overlap(qstart, qend, out)
        else:  # qstart > center
            # 与本节点区间相交要求 end > qstart；end 降序，手工二分截断
            ends, payloads = self._by_end, self._payloads[1]
            lo, hi = 0, len(ends)
            while lo < hi:
                mid = (lo + hi) // 2
                if ends[mid] > qstart:
                    lo = mid + 1
                else:
                    hi = mid
            out.extend(payloads[:lo])
            if self._right is not None:
                self._right._overlap(qstart, qend, out)
