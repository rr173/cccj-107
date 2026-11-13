"""独立小数组逐点标量参考：核对切块重采样结果，不调用生产插值函数。

参考实现完全按定义逐点手算（纯 Python 标量循环）：
  * 目标整数下标 i,j,k -> 世界坐标 w = T @ [i,j,k,1]（体素中心语义）；
  * 源连续下标 s = inv(S) @ w；
  * 强度：闭区间判定 -> 8 个邻点按权重累加，零权邻点跳过、缺失邻点不归一化、
    参与邻点非有限即无效；
  * 标签：最近中心，恰好半格取较小下标。
只依赖 numpy 的数组存取与 4×4 求逆（几何本身），插值逻辑一行也不复用生产代码。
"""
from __future__ import annotations

import numpy as np

CHUNK_SIZES = (1, 2, 3, 5, 64, 1 << 16)


def to_world(target_affine, i, j, k):
    p = np.asarray(target_affine, dtype=np.float64) @ np.array([i, j, k, 1.0])
    return p[:3]


def to_source(src_affine, world):
    inv = np.linalg.inv(np.asarray(src_affine, dtype=np.float64))
    return (inv @ np.array([world[0], world[1], world[2], 1.0]))[:3]


def _inside(s, shape):
    for d in range(3):
        if not (0.0 <= s[d] <= float(shape[d]) - 1.0):
            return False
    return True


def ref_intensity_point(src, src_affine, target_affine, i, j, k):
    """返回 (value, valid)；无效 value=None（生产侧应输出 NaN）。"""
    s = to_source(src_affine, to_world(target_affine, i, j, k))
    if not _inside(s, src.shape):
        return None, False
    base = [int(np.floor(s[d])) for d in range(3)]
    frac = [float(s[d] - base[d]) for d in range(3)]
    acc = 0.0
    for di in (0, 1):
        for dj in (0, 1):
            for dk in (0, 1):
                off = (di, dj, dk)
                w = 1.0
                for d in range(3):
                    w *= frac[d] if off[d] else (1.0 - frac[d])
                if w == 0.0:
                    continue  # 零权邻点：不访问、不要求存在
                cell = (base[0] + di, base[1] + dj, base[2] + dk)
                if not all(0 <= cell[d] < src.shape[d] for d in range(3)):
                    continue  # 缺失邻点：丢弃且不重新归一化
                v = float(src[cell[0], cell[1], cell[2]])
                if v != v or v in (float("inf"), float("-inf")):
                    return None, False  # 参与邻点非有限 -> 整点无效
                acc += w * v
    return acc, True


def ref_label_point(src, src_affine, target_affine, i, j, k):
    s = to_source(src_affine, to_world(target_affine, i, j, k))
    if not _inside(s, src.shape):
        return 0, False
    out = []
    for d in range(3):
        lo = int(np.floor(s[d]))
        frac = float(s[d] - lo)
        # 恰好半格取较小下标（严格比较，参考侧不带容差）
        if frac > 0.5:
            out.append(min(lo + 1, src.shape[d] - 1))
        else:
            out.append(max(lo, 0))
    return int(src[out[0], out[1], out[2]]), True


def reference_volume_loop(src, src_affine, target_affine, target_shape, *, label=False):
    """逐点标量参考整卷结果（小数组用）。返回 (values, valid, n_points)。"""
    shape = tuple(int(s) for s in target_shape)
    values = np.zeros(shape, dtype=np.float64 if not label else np.int64)
    valid = np.zeros(shape, dtype=bool)
    fn = ref_label_point if label else ref_intensity_point
    n = 0
    for k in range(shape[2]):
        for j in range(shape[1]):
            for i in range(shape[0]):
                v, ok = fn(src, src_affine, target_affine, i, j, k)
                n += 1
                if ok:
                    values[i, j, k] = v
                    valid[i, j, k] = True
                elif not label:
                    values[i, j, k] = np.nan
    return values, valid, n


def compare_chunked(src, src_affine, target_affine, target_shape, *,
                    label=False, atol=1e-6, chunk_sizes=CHUNK_SIZES):
    """对不同块大小的生产结果与参考逐点比较；返回每个块大小的差异报告。"""
    from org_er.volprep.resample import resample_intensity, resample_label

    ref_val, ref_ok, n_points = reference_volume_loop(
        src, src_affine, target_affine, target_shape, label=label)
    reports = []
    for cs in chunk_sizes:
        if label:
            got, mask = resample_label(src, src_affine, target_affine, target_shape,
                                       chunk_voxels=cs)
        else:
            got, mask = resample_intensity(src, src_affine, target_affine, target_shape,
                                           chunk_voxels=cs)
        got_ok = mask.astype(bool)
        mask_same = bool(np.array_equal(got_ok, ref_ok))
        if label:
            # 无效点生产侧为 0；有效点必须与参考类别完全一致
            val_same = bool(np.array_equal(got[got_ok], ref_val[ref_ok])) and bool(
                np.all(got[~got_ok] == 0))
            max_diff = None
        else:
            if got_ok.any():
                max_diff = float(np.max(np.abs(got[got_ok].astype(np.float64)
                                               - ref_val[got_ok])))
            else:
                max_diff = 0.0
            nan_ok = bool(np.all(np.isnan(got[~got_ok]))) if (~got_ok).any() else True
            val_same = bool(mask_same and nan_ok and max_diff <= atol)
        reports.append({
            "chunk_voxels": int(cs),
            "n_points": int(n_points),
            "mask_equal": mask_same,
            "values_equal": val_same,
            "max_abs_diff": max_diff,
            "n_valid": int(ref_ok.sum()),
            "ok": bool(mask_same and val_same),
        })
    return reports


def reference_single_point(src, src_affine, target_affine, i, j, k, *, label=False):
    """单个目标点参考（演示逐点标量核对时直接调用）。"""
    return (ref_label_point if label else ref_intensity_point)(
        src, src_affine, target_affine, i, j, k)
