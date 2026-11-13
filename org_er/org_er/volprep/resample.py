"""按目标切块的重采样核心：三线性（强度）与最近邻（标签）。

设计约束
--------
* **不构造整个四维坐标云**：对目标网格按块（chunk）取整数下标，块体积有上限；
  每次只为一块目标体素生成坐标，再映射回源连续下标进行插值。
* **源连续下标**：``s = inv(src_affine) @ target_affine @ [i,j,k,1]``。
  世界坐标与源下标均按“体素整数下标指中心”约定，世界单位毫米。
* **强度三线性**：
  - 目标中心落在闭区间 ``[0, n-1]`` 之外 -> 无效（输出 NaN，掩码 0）；
  - 只在**权重非零**的邻点上取值；恰好位于边界平面上时，越界方向的邻点权重为 0，
    因此不要求不存在的邻居（边界退化维度安全）；
  - 参与邻点的值（已应用头缩放）含 NaN/Inf -> 该点无效（不把 NaN 当 0）；
  - 有效点输出 ``Σ w_i * v_i``，权重之和为 1（不做剩余权重归一化）；
  - 若某邻点权重非零但源上不存在（只可能发生在“闭区间外”的点，已被前置判定
    排除），该邻点被跳过，剩余权重保持原值。
* **标签最近邻**：取最近的整数中心；恰好半格（两中心距离相等）取较小下标；
  无效位置输出 0，与真实类别 0 由掩码区分。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

import numpy as np

# 每块目标体素上限：坐标云/权重数组均为小块，与四维体积大小无关
DEFAULT_CHUNK_VOXELS = 1 << 16
# 标签“恰好半格”判定的相对容差（只吸收浮点噪声，真正的距离比较不变）
_TIE_TOL = 1e-12


@dataclass(frozen=True)
class ChunkPlan:
    chunk_shape: tuple
    n_chunks: int


def plan_chunks(shape, max_voxels: int = DEFAULT_CHUNK_VOXELS) -> ChunkPlan:
    """把目标三维 shape 切成若干块（尽量立方，每块 <= max_voxels）。"""
    shape = tuple(int(s) for s in shape)
    if max_voxels < 1:
        raise ValueError("max_voxels 须为正整数")
    if any(s <= 0 for s in shape):
        raise ValueError(f"目标 shape 须为正: {shape}")
    side = max(1, int(max_voxels ** (1.0 / 3.0)))
    # 保证块体积不超过上限：cube root 的取整可能向上溢出（如 500 -> 7.9 -> 8）
    while side > 1 and side ** 3 > max_voxels:
        side -= 1
    while (side + 1) ** 3 <= max_voxels:
        side += 1
    chunk = tuple(min(s, side) for s in shape)
    n = 1
    for s, c in zip(shape, chunk):
        n *= -(-s // c)  # ceil
    return ChunkPlan(chunk_shape=chunk, n_chunks=n)


def iter_chunk_slices(shape, chunk_shape) -> Iterator[tuple]:
    """按确定顺序产出每块的三维切片（z 最慢、x 最快）。"""
    starts = [range(0, s, c) for s, c in zip(shape, chunk_shape)]
    for z0 in starts[2]:
        for y0 in starts[1]:
            for x0 in starts[0]:
                yield (
                    slice(x0, min(x0 + chunk_shape[0], shape[0])),
                    slice(y0, min(y0 + chunk_shape[1], shape[1])),
                    slice(z0, min(z0 + chunk_shape[2], shape[2])),
                )


def _grid_indices(slices) -> np.ndarray:
    """块内整数体素下标，形状 (n, 3)，n = 块体积（C 序，与 reshape 回块一致）。"""
    gx = np.arange(slices[0].start, slices[0].stop, dtype=np.float64)
    gy = np.arange(slices[1].start, slices[1].stop, dtype=np.float64)
    gz = np.arange(slices[2].start, slices[2].stop, dtype=np.float64)
    xx, yy, zz = np.meshgrid(gx, gy, gz, indexing="ij")  # (nx, ny, nz)
    return np.column_stack((xx.ravel(), yy.ravel(), zz.ravel()))


def source_continuous_indices(target_affine, src_affine_inv, slices) -> np.ndarray:
    """目标块整数下标 -> 源连续下标（形状 (n, 3)）。

    以 3×4 组合矩阵 ``inv(S)[:3,:] @ T`` 一次性完成，不为整个体积建坐标云。
    """
    idx = _grid_indices(slices)  # 目标整数下标（中心），(n, 3)
    m34 = np.asarray(src_affine_inv, dtype=np.float64)[:3, :] @ np.asarray(
        target_affine, dtype=np.float64)
    return idx @ m34[:, :3].T + m34[:, 3]


def _inside_closed(c: np.ndarray, shape) -> np.ndarray:
    """闭区间 [0, n-1] 判定（逐维），形状 (n,) 布尔。"""
    ok = np.ones(c.shape[0], dtype=bool)
    for d in range(3):
        ok &= (c[:, d] >= 0.0) & (c[:, d] <= float(shape[d]) - 1.0)
    return ok


# --------------------------------------------------------------------- 强度
def trilinear_sample(src, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """对源三维数组做三线性采样，返回 (values, valid)。

    src 必须是**已应用头缩放**的三维数组；c 为 (n,3) 源连续下标。
    无效点的 values 为 NaN，valid=False。
    """
    n = c.shape[0]
    values = np.full(n, np.nan, dtype=np.float64)
    valid = np.zeros(n, dtype=bool)
    inside = _inside_closed(c, src.shape)
    if not inside.any():
        return values, valid

    pos_all = np.flatnonzero(inside)
    ci = c[pos_all]
    base = np.floor(ci).astype(np.int64)
    frac = ci - base
    acc = np.zeros(pos_all.shape[0], dtype=np.float64)
    bad = np.zeros(pos_all.shape[0], dtype=bool)

    for corner in range(8):
        off = ((corner >> 0) & 1, (corner >> 1) & 1, (corner >> 2) & 1)
        w = np.ones(pos_all.shape[0], dtype=np.float64)
        for d in range(3):
            w = w * (frac[:, d] if off[d] else (1.0 - frac[:, d]))
        nz = w > 0.0
        if not nz.any():
            continue  # 退化维度：零权邻点不访问、不要求存在
        ijk = base[nz] + np.asarray(off, dtype=np.int64)
        ok = np.ones(int(nz.sum()), dtype=bool)
        for d in range(3):
            ok &= (ijk[:, d] >= 0) & (ijk[:, d] < src.shape[d])
        sub = np.zeros(int(nz.sum()), dtype=np.float64)
        sub_bad = np.zeros(int(nz.sum()), dtype=bool)
        if ok.any():
            sel = ijk[ok]
            vals = np.asarray(src[sel[:, 0], sel[:, 1], sel[:, 2]], dtype=np.float64)
            finite = np.isfinite(vals)
            if not finite.all():
                sub_bad[np.flatnonzero(ok)[~finite]] = True
            sub[ok] = np.where(finite, vals, 0.0) * (w[nz][ok])
        # 权重非零但源上不存在的邻点：丢弃且不重新归一化
        acc[nz] += sub
        bad[nz] |= sub_bad

    values[pos_all] = np.where(bad, np.nan, acc)
    valid[pos_all] = ~bad
    return values, valid


# --------------------------------------------------------------------- 标签
def nearest_label_sample(src, c: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """最近中心采样；恰好半格取较小下标；无效点值为 0 且 valid=False。"""
    n = c.shape[0]
    values = np.zeros(n, dtype=np.int64)
    valid = np.zeros(n, dtype=bool)
    inside = _inside_closed(c, src.shape)
    if not inside.any():
        return values, valid

    pos_all = np.flatnonzero(inside)
    ci = c[pos_all]
    base = np.floor(ci).astype(np.int64)
    frac = ci - base
    # 最近整数中心：比较到两中心的距离。恰好相等（半格）取较小下标 base；
    # 用相对容差只吸收浮点噪声，不改变真正的距离比较。
    to_lo = frac                       # = 到 base 的距离
    to_hi = 1.0 - frac                 # = 到 base+1 的距离
    tie_tol = _TIE_TOL * np.maximum(1.0, np.abs(ci))
    go_up = to_hi < (to_lo - tie_tol)
    idx = base + go_up
    for d in range(3):
        idx[:, d] = np.clip(idx[:, d], 0, int(src.shape[d]) - 1)
    vals = np.asarray(src[idx[:, 0], idx[:, 1], idx[:, 2]])
    values[pos_all] = vals.astype(np.int64, copy=False)
    valid[pos_all] = True
    return values, valid

# --------------------------------------------------------------------- 整卷
def _resample(src, src_affine, target_affine, target_shape, chunk_voxels, *, nearest):
    if np.ndim(src) != 3:
        raise ValueError(f"源数组须为三维，实际 shape={np.shape(src)}")
    shape = tuple(int(s) for s in target_shape)
    if nearest:
        out = np.zeros(shape, dtype=np.int32)
    else:
        out = np.full(shape, np.nan, dtype=np.float32)
    mask = np.zeros(shape, dtype=np.uint8)
    if any(s == 0 for s in shape):
        return out, mask
    plan = plan_chunks(shape, chunk_voxels)
    sinv = np.linalg.inv(np.asarray(src_affine, dtype=np.float64))
    sampler = nearest_label_sample if nearest else trilinear_sample
    for sl in iter_chunk_slices(shape, plan.chunk_shape):
        c = source_continuous_indices(target_affine, sinv, sl)
        vals, valid = sampler(src, c)
        blk_shape = tuple(sl[d].stop - sl[d].start for d in range(3))
        if nearest:
            out[sl] = vals.reshape(blk_shape).astype(np.int32, copy=False)
        else:
            out[sl] = vals.reshape(blk_shape).astype(np.float32, copy=False)
        mask[sl] = valid.reshape(blk_shape).astype(np.uint8)
    return out, mask


def resample_intensity(src, src_affine, target_affine, target_shape,
                       chunk_voxels: int = DEFAULT_CHUNK_VOXELS):
    """整卷强度重采样（分块），返回 (float32 数组, uint8 掩码)。"""
    return _resample(src, src_affine, target_affine, target_shape, chunk_voxels, nearest=False)


def resample_label(src, src_affine, target_affine, target_shape,
                   chunk_voxels: int = DEFAULT_CHUNK_VOXELS):
    """整卷标签重采样（分块），返回 (int32 数组, uint8 掩码)。"""
    return _resample(src, src_affine, target_affine, target_shape, chunk_voxels, nearest=True)
