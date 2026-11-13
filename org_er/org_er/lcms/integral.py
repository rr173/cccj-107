"""时间窗内的流式分段线性积分。

规则（与扫描点直接相加完全不同）：

- 同一极性中相邻扫描时间差 ``<= max_gap_seconds`` 才在二者之间作线性插值；
  更大的间隙没有可积分信号，**禁止跨间隙拉直线**，也绝不外推；
- 可用线段与目标时间窗取交集：端点落在扫描之间时只在对应可用线段内插值，
  不借窗外远处的点填缺口；空数组扫描产生的 0 仍参与连线；
- 在交集区间上积分 ``max(插值强度 - background, 0)``，单位 强度×秒；
- 线段穿过背景值时先求交点再积分剩余三角形，**不能先把两端截到 0 再做整
  个梯形**；
- 完整窗口都有线段覆盖才是 ``complete``；覆盖正时长但不完整为 ``partial``
  （只报实际覆盖部分的面积）；完全没有可积时长为 ``absent``（面积留空）。
  真实覆盖而信号全为 0 时面积为 0，与 absent 严格分开。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .targets import Target

_EPS_TIME = 1e-12


@dataclass
class TargetIntegral:
    target_id: str
    area: float = 0.0
    covered_seconds: float = 0.0
    window_seconds: float = 1.0
    n_segments: int = 0          # 参与积分的线段数
    n_scans: int = 0             # 落入处理流的扫描数（含窗外）
    has_any_segment: bool = False

    def coverage_ratio(self) -> float:
        if self.window_seconds <= 0.0:
            return 0.0
        return self.covered_seconds / self.window_seconds

    def status(self) -> str:
        if not self.has_any_segment or self.covered_seconds <= 0.0:
            return "absent"
        # 完整覆盖：窗内每个时刻都有线段（允许浮点误差）
        if self.covered_seconds + _EPS_TIME >= self.window_seconds:
            return "complete"
        return "partial"


class StreamingIntegrator:
    """每个目标一个实例；只保留上一个扫描时间与信号，O(目标数) 内存。"""

    def __init__(self, target: Target, max_gap_seconds: float):
        if not math.isfinite(max_gap_seconds) or max_gap_seconds < 0.0:
            raise ValueError(f"max_gap_seconds 须为非负有限值，实际 {max_gap_seconds}")
        self.target = target
        self.max_gap = float(max_gap_seconds)
        self.state = TargetIntegral(
            target_id=target.target_id,
            window_seconds=target.window_length,
        )
        self._last_t: float | None = None
        self._last_y: float = 0.0

    def add_scan(self, t: float, y: float) -> None:
        """喂入一个同极性扫描时刻与该目标的提取信号。"""
        st = self.state
        st.n_scans += 1
        if self._last_t is not None:
            dt = t - self._last_t
            if dt <= 0.0:
                # 严格递增已在时间序列校验处拦截，这里做防御性检查
                raise ValueError(f"扫描时间非严格递增: {self._last_t} -> {t}")
            if dt <= self.max_gap + _EPS_TIME:
                self._integrate_segment(self._last_t, self._last_y, t, y)
        self._last_t = t
        self._last_y = y

    def _integrate_segment(self, t0: float, y0: float, t1: float, y1: float) -> None:
        win_lo = self.target.window_start
        win_hi = self.target.window_end
        # 线段与闭时间窗取交集；无交集直接跳过（不借窗外点、不外推）
        a = max(t0, win_lo)
        b = min(t1, win_hi)
        if b - a <= _EPS_TIME:
            return
        dt = t1 - t0
        va = y0 + (y1 - y0) * (a - t0) / dt
        vb = y0 + (y1 - y0) * (b - t0) / dt
        area = _integral_above_background(a, va, b, vb, self.target.background)
        self.state.area += area
        self.state.covered_seconds += (b - a)
        self.state.n_segments += 1
        self.state.has_any_segment = True

    def finalize(self) -> TargetIntegral:
        st = self.state
        # 数值清理：负的极小值钳为 0
        if st.area < 0.0 and st.area > -1e-9:
            st.area = 0.0
        if st.covered_seconds < 0.0:
            st.covered_seconds = 0.0
        return st


def _integral_above_background(a: float, va: float, b: float, vb: float,
                               background: float) -> float:
    """区间 [a,b] 上 max(线性(t) - background, 0) 的积分。

    线段穿过背景值时先求交点，再对剩余三角形/梯形积分；绝不先把两端截零再
    做整个梯形（那会错误地把背景以下区域也按零信号梯形计入时长外的形状）。
    """
    fa = va - background
    fb = vb - background
    if fa <= 0.0 and fb <= 0.0:
        return 0.0
    if fa > 0.0 and fb > 0.0:
        return 0.5 * (fa + fb) * (b - a)
    # 一正一负（或恰为 0）：求背景交点 tc，只在正号一侧积三角形
    # 线性插值零点：tc = a + (b-a) * fa / (fa - fb)
    tc = a + (b - a) * fa / (fa - fb)
    if fa > 0.0 >= fb:
        return 0.5 * fa * (tc - a)
    return 0.5 * fb * (b - tc)
