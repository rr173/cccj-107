"""扫描级目标离子提取与流式状态。

对每个扫描和同极性目标，取闭区间 ``mz×(1±ppm/1e6)`` 内**所有**质荷峰的强度
之和，形成该目标在扫描时刻的信号；没有命中是实际观测到 0，不是缺测。m/z 非
递减，重复 m/z 的峰都计入。两种极性的扫描可交错，但各自参与分析的 MS1 时间
必须严格递增——不静默排序、不合并重复时间。

流式处理：除参数定义与目标配置外，只保留每种极性的上一个扫描时间及各目标的
上一个信号（交给积分器），不积存全部谱图或完整色谱；明细可顺序写磁盘。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .errors import IntegrationError
from .mzml import DecodedScan
from .targets import Target


def extract_signal(mz: np.ndarray, intensity: np.ndarray, target: Target) -> float:
    """闭 m/z 窗口内全部峰的强度之和（无命中 -> 0.0，真实观测零）。

    m/z 非递减，用两次 searchsorted 定界；重复 m/z 全部计入。
    """
    if mz.size == 0:
        return 0.0
    lo = target.mz_low
    hi = target.mz_high
    left = int(np.searchsorted(mz, lo, side="left"))
    right = int(np.searchsorted(mz, hi, side="right"))
    if right <= left:
        return 0.0
    total = float(intensity[left:right].sum(dtype=np.float64))
    # 输入强度已校验有限非负；和仍须有限（防溢出语义，IEEE 和正常不会 NaN）
    if not np.isfinite(total):
        raise IntegrationError(
            f"目标 {target.target_id!r} 在 m/z 窗 [{lo}, {hi}] 的强度和非有限")
    return total


@dataclass
class PolaritiesState:
    """每种极性只保留上一个扫描时间；重复/回退时间即输入失败。"""
    last_time: dict = field(default_factory=dict)  # polarity -> float

    def check_advance(self, scan: DecodedScan) -> None:
        prev = self.last_time.get(scan.polarity)
        if prev is not None and not scan.time_seconds > prev:
            raise IntegrationError(
                f"{scan.polarity} 极性的 MS1 时间未严格递增: "
                f"上一扫描 t={prev}s，当前 spectrum[{scan.index}] t={scan.time_seconds}s"
                "（不静默排序或合并重复时间）")
        self.last_time[scan.polarity] = scan.time_seconds


def targets_by_polarity(targets: list[Target]) -> dict[str, list[Target]]:
    out: dict[str, list[Target]] = {}
    for t in targets:
        out.setdefault(t.polarity, []).append(t)
    return out
