#!/usr/bin/env python3
"""体数据整备端到端演示：自建合成 NIfTI，覆盖全部规则并核验。

直接运行：python3 scripts/run_volprep_demo.py

覆盖：
  §1 世界坐标线性函数真值（轴对齐 / 轴翻转 / 斜切旋转 / 米 / 微米单位）
  §2 4D 帧分离、缩放强度、qform 回退与几何记录
  §3 边界零权邻点含 NaN、越界掩码、标签半格、非法几何与坏清单项
  §4 小数组逐点标量参考核对切块结果（多种块大小）
  §5 不构造整卷坐标云（块上限实测）
  §6 产物读回不依赖原文件；旧机构/贡献命令继续可用
"""
import json
import math
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import numpy as np

import _bootstrap  # noqa: F401
import nibabel as nib
from org_er.volprep.geometry import load_target
from org_er.volprep.job import run_batch
from org_er.volprep.manifest import load_product
from org_er.volprep import reference
from org_er.volprep import resample as R

ROOT = _bootstrap.ROOT
DEMO = ROOT / "output" / "volprep_demo"
RAW = DEMO / "raw"

CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok)))
    print(f"      => {'PASS' if ok else 'FAIL'}" + (f"  ({detail})" if detail else ""))
    return ok


def h(title):
    print("\n" + "=" * 78)
    print(title)
    print("=" * 78)


def truth(x, y, z):
    """世界坐标（毫米）上的线性强度真值。"""
    return 0.5 * x - 1.25 * y + 2.5 * z + 7.0


# ------------------------------------------------------------------ 造数据
def write_vol(name, data, affine, *, units="mm", sform_code=2, qform_code=0,
              slope=None, inter=None, dtype=None, gz=False):
    """写一个合成 NIfTI。先把数据落成普通数组代理，再写头/缩放，保证字段不被重写。

    nibabel 的行为：``Nifti1Image(data, affine=...)`` 在保存时会重写 sform/qform
    与自动缩放；``Nifti1Image(data, affine=None, header=...)`` 则原样写出头字段，
    但构造器会把“待设置的缩放”清成 NaN，因此缩放要在构造之后直接写头。
    """
    data = np.asarray(data, dtype=dtype) if dtype is not None else np.asarray(data)
    aff = np.asarray(affine, dtype=np.float64)
    hdr = nib.Nifti1Header()
    hdr.set_data_dtype(data.dtype)
    img = nib.Nifti1Image(data, None, header=hdr)
    img.header.set_xyzt_units(units)
    img.header.set_sform(None if sform_code == 0 else aff, code=sform_code)
    img.header.set_qform(None if qform_code == 0 else aff, code=qform_code)
    if slope is not None:
        img.header["scl_slope"] = float(slope)
        img.header["scl_inter"] = 0.0 if inter is None else float(inter)
    # 关键：让保存使用头里现成的字段而不按 affine 重写（affine 置为 None）
    img._affine = None
    path = RAW / f"{name}.nii.gz" if gz else RAW / f"{name}.nii"
    nib.save(img, str(path))
    got = nib.load(str(path))
    assert int(got.header["sform_code"]) == int(sform_code), (
        name, int(got.header["sform_code"]), sform_code)
    if slope is not None:
        assert abs(float(got.dataobj.slope) - float(slope)) < 1e-6, (
            name, float(got.dataobj.slope), float(slope))
    return path


def patch_srows(path, rows):
    """直接改写 .nii 头里的 srow_x/y/z 字节，构造结构上“不合法”的几何。

    nibabel 保存时会把头缓存在 ``.nii`` 前 352 字节，因此必须用字节写入
    （``Path.write_bytes``）而不是文本/流式写入，才能保留被改动的字段。
    """
    assert str(path).endswith(".nii")
    blob = bytearray(Path(path).read_bytes())
    for n, row in enumerate(rows):
        blob[280 + 16 * n: 296 + 16 * n] = struct.pack("<4f", *row)
    Path(path).write_bytes(bytes(blob))


def world_grid(target):
    shape = target.shape
    idx = np.stack(np.meshgrid(np.arange(shape[0]), np.arange(shape[1]),
                               np.arange(shape[2]), indexing="ij"), axis=-1)
    idx = idx.reshape(-1, 3)
    w = idx @ target.affine[:3, :3].T + target.affine[:3, 3]
    return w


def truth_grid(target):
    w = world_grid(target)
    return truth(w[:, 0], w[:, 1], w[:, 2]).reshape(target.shape)


def source_world_grid(shape, affine):
    """源体素中心的世界坐标，形状 (nx,ny,nz,3)；仅用于合成数据/真值。"""
    idx = np.stack(np.meshgrid(*[np.arange(s) for s in shape], indexing="ij"), axis=-1)
    return idx @ np.asarray(affine, dtype=np.float64)[:3, :3].T + np.asarray(affine)[:3, 3]


def source_grid(shape, affine):
    w = source_world_grid(shape, affine)
    return truth(w[..., 0], w[..., 1], w[..., 2]).astype(np.float32)


A_STRAIGHT = np.array([[2.0, 0, 0, -7], [0, 2.0, 0, -6], [0, 0, 2.0, -5], [0, 0, 0, 1]])
SHAPE_STRAIGHT = (16, 14, 13)


def write_manifest(path, items, batch_id=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"batch_id": batch_id or path.parent.name, "items": items}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run_target(spec, name):
    p = DEMO / f"{name}.json"
    p.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return p


def load_products(index, out_dir, rec):
    out = []
    for f in rec["frames"]:
        arr, mask, meta = load_product(Path(out_dir) / "products" / f["meta_file"])
        out.append((arr, mask, meta))
    return out


def rec_for(index, sample_key, kind="intensity"):
    for r in index["samples"]:
        if r["sample_key"] == sample_key and r["kind"] == kind:
            return r
    return None


def max_err(arr, mask, target, extra=0.0):
    t = truth_grid(target) + extra
    return float(np.max(np.abs(arr[mask].astype(np.float64) - t[mask])))


# ------------------------------------------------------------------ 主流程
def main():
    if DEMO.exists():
        shutil.rmtree(DEMO)
    RAW.mkdir(parents=True)

    tgt_core = load_target({"shape": [16, 14, 12],
                            "affine": [[1.5, 0, 0, -6], [0, 1.5, 0, -5],
                                       [0, 0, 1.5, -4], [0, 0, 0, 1]]})

    h("§1 世界坐标线性真值：轴对齐 / 轴翻转 / 斜切旋转 / 米 / 微米")
    base = source_grid(SHAPE_STRAIGHT, A_STRAIGHT)
    write_vol("s1_axis", base, A_STRAIGHT, gz=True)
    # 轴翻转：x、z 方向反向，世界覆盖范围与 s1_axis 相同
    a_flip = np.array([[-2.0, 0, 0, 23], [0, 2.0, 0, -6], [0, 0, -2.0, 19], [0, 0, 0, 1]])
    write_vol("s1_flip", source_grid(SHAPE_STRAIGHT, a_flip), a_flip, gz=True)
    # 斜切 + 旋转：不能退化成“按 voxel size 拉伸数组”
    a_shear = np.array([[2.0, 0.6, 0.2, -8], [0.3, 2.1, -0.4, -6],
                        [0.1, 0.5, 2.2, -5], [0, 0, 0, 1]])
    write_vol("s1_shear", source_grid((20, 18, 16), a_shear), a_shear, gz=True)
    # 米 / 微米：文件里用各自单位，归一化后应和毫米网格一致
    metered = A_STRAIGHT.copy()
    metered[:3, :] *= 0.001
    write_vol("s1_meter", base, metered, units="meter", gz=True)
    a_um = A_STRAIGHT.copy()
    a_um[:3, :] *= 1000.0  # 1 mm = 1000 micron
    write_vol("s1_micron", base, a_um, units="micron", sform_code=0, qform_code=2)

    # qform-only 与“sform 坏了但 qform 有效”
    write_vol("s1_qform_only", base, A_STRAIGHT, sform_code=0, qform_code=2)
    p_fb = write_vol("s1_sform_bad_qform_ok", base, A_STRAIGHT, sform_code=2, qform_code=2)
    bad_rows = [[np.nan, 0, 0, 0], [0, 2, 0, -6], [0, 0, 2, -5]]
    patch_srows(p_fb, bad_rows)

    write_vol("s1_4d", np.stack([base + 100.0 * fr for fr in range(4)], axis=3),
              A_STRAIGHT, dtype=np.float32)
    # 缩放强度：int16 原始值 + slope=0.5 -> 与真值一致
    g = np.rint((source_grid(SHAPE_STRAIGHT, A_STRAIGHT) * 2.0 + 3.0)).astype(np.int16)
    # 真值为 f；f*2+3 在每个源体素中心恰为整数（本例 A_STRAIGHT 下可验证）
    write_vol("s1_scaled", g, A_STRAIGHT, slope=0.5, inter=-1.5, dtype=np.int16)

    tgt_json = run_target({"shape": list(tgt_core.shape),
                           "affine": tgt_core.affine.tolist()}, "target_core")
    items = [
        {"path": "raw/s1_axis.nii.gz", "sample_key": "s1_axis", "kind": "intensity"},
        {"path": "raw/s1_flip.nii.gz", "sample_key": "s1_flip", "kind": "intensity"},
        {"path": "raw/s1_shear.nii.gz", "sample_key": "s1_shear", "kind": "intensity"},
        {"path": "raw/s1_meter.nii.gz", "sample_key": "s1_meter", "kind": "intensity"},
        {"path": "raw/s1_micron.nii", "sample_key": "s1_micron", "kind": "intensity"},
        {"path": "raw/s1_qform_only.nii", "sample_key": "s1_qform_only", "kind": "intensity"},
        {"path": "raw/s1_sform_bad_qform_ok.nii", "sample_key": "s1_fallback", "kind": "intensity"},
        {"path": "raw/s1_4d.nii", "sample_key": "s1_4d", "kind": "intensity"},
        {"path": "raw/s1_scaled.nii", "sample_key": "s1_scaled", "kind": "intensity"},
    ]
    manifest = write_manifest(DEMO / "batch_core.json", items, "core")
    index = run_batch(manifest, tgt_json, DEMO / "core", chunk_voxels=1024)
    print(f"      批次 core: 完成 {index['n_completed']} 项，诊断 {index['n_diagnostics']} 项")
    check("core 批次全部完成且无诊断",
          index["n_completed"] == 9 and index["n_diagnostics"] == 0,
          f"{index['n_completed']}/9")

    for key, extra in (("s1_axis", 0.0), ("s1_flip", 0.0), ("s1_shear", 0.0),
                       ("s1_meter", 0.0), ("s1_micron", 0.0), ("s1_qform_only", 0.0),
                       ("s1_fallback", 0.0)):
        rec = rec_for(index, key)
        arr, mask, meta = load_products(index, DEMO / "core", rec)[0]
        if key == "s1_shear":
            frac = mask.mean()
            ok = frac > 0.5 and max_err(arr, mask.astype(bool), tgt_core) < 5e-3
            check("斜切/旋转几何按 affine 重采样（非按 voxel size 拉伸）", ok,
                  f"有效比例 {frac:.2%}，最大误差 {max_err(arr, mask.astype(bool), tgt_core):.2e}")
        else:
            all_valid = bool(mask.all())
            err = max_err(arr, mask.astype(bool), tgt_core)
            check(f"{key}: 有效点世界坐标线性真值一致", all_valid and err < 5e-3,
                  f"全有效={all_valid}，最大误差 {err:.2e}")

    # 斜切反例：按“体素尺寸相同 + 同一平移”的轴对齐几何去读同一份数组，
    # 即只按 voxel size 拉伸、忽略旋转/斜切。真值仍是世界坐标线性函数，
    # 把这份轴对齐结果插值到目标网格后与真值比较：误差应显著。
    rec = rec_for(index, "s1_shear")
    arr, mask, _ = load_products(index, DEMO / "core", rec)[0]
    naive_aff = np.diag([2.0, 2.0, 2.0, 1.0])
    naive_aff[:3, 3] = [0.0, 0.0, 0.0]
    shear_src = source_grid((20, 18, 16), a_shear)
    naive_out, naive_mask = R.resample_intensity(
        shear_src, naive_aff, tgt_core.affine, tgt_core.shape, chunk_voxels=1024)
    m = mask.astype(bool) & naive_mask.astype(bool)
    naive_err = float(np.max(np.abs(naive_out[m].astype(np.float64) - truth_grid(tgt_core)[m])))
    check("对照：只按 voxel size 拉伸（忽略旋转/斜切）会明显偏离真值", naive_err > 1.0,
          f"naive 最大误差 {naive_err:.2f}")
    check("按 affine 重采样的误差远小于 naive 拉伸", naive_err > 10 * max_err(
        arr, mask.astype(bool), tgt_core), f"naive {naive_err:.2f} vs affine "
        f"{max_err(arr, mask.astype(bool), tgt_core):.2e}")

    # 几何记录
    meta = load_products(index, DEMO / "core", rec_for(index, "s1_flip"))[0][2]
    geo = meta["source"]["geometry"]
    check("轴翻转几何记录正确（-x / +y / -z）",
          geo["axcodes"] == ["-x", "+y", "-z"] and geo["axis_flips"] == [True, False, True],
          str(geo["axcodes"]))
    meta = load_products(index, DEMO / "core", rec_for(index, "s1_meter"))[0][2]
    check("米 -> 毫米归一化：单位换算系数 1000，记录归一化矩阵",
          meta["source"]["geometry"]["unit_scale_to_mm"] == 1000.0
          and np.allclose(meta["source"]["geometry"]["affine"], A_STRAIGHT),
          f"scale={meta['source']['geometry']['unit_scale_to_mm']}")
    meta = load_products(index, DEMO / "core", rec_for(index, "s1_micron"))[0][2]
    check("微米 -> 毫米归一化：系数 0.001，几何来自 qform",
          meta["source"]["geometry"]["unit_scale_to_mm"] == 0.001
          and meta["source"]["geometry"]["geometry_source"] == "qform",
          meta["source"]["geometry"]["geometry_source"])
    meta = load_products(index, DEMO / "core", rec_for(index, "s1_fallback"))[0][2]
    rej = meta["source"]["geometry"]["rejected_candidates"]
    check("sform 非法时回退 qform，并保留 sform 拒绝原因",
          meta["source"]["geometry"]["geometry_source"] == "qform"
          and any(w == "sform" and "非有限" in why for w, why in rej),
          str(rej))

    # 4D 帧与缩放
    rec = rec_for(index, "s1_4d")
    check("4D 原序逐帧分离（4 帧，产物仍三维）",
          rec["n_frames"] == 4 and len(rec["frames"]) == 4
          and [f["frame_index"] for f in rec["frames"]] == [0, 1, 2, 3]
          and all(tuple(load_products(index, DEMO / "core", rec)[i][0].shape) == tgt_core.shape
                  for i in range(4)),
          f"n_frames={rec['n_frames']}")
    ok = True
    for i, (arr, mask, meta) in enumerate(load_products(index, DEMO / "core", rec)):
        ok &= bool(mask.all()) and max_err(arr, mask.astype(bool), tgt_core, 100.0 * i) < 5e-3
        ok &= meta["frame_index"] == i and meta["frame_count"] == 4
    check("4D 每帧真值（原序 + 帧偏移，第四维不当空间轴）", ok)

    rec = rec_for(index, "s1_scaled")
    arr, mask, meta = load_products(index, DEMO / "core", rec)[0]
    sc = meta["source"]["scaling"]
    err = max_err(arr, mask.astype(bool), tgt_core)
    check("源值应用头缩放系数后插值",
          sc["scaling_applied"] and abs(sc["effective_slope"] - 0.5) < 1e-12
          and abs(sc["effective_inter"] + 1.5) < 1e-12
          and bool(mask.all()) and err < 5e-3,
          f"slope={sc['effective_slope']}, inter={sc['effective_inter']}, err={err:.2e}")

    # -------------------------------------------------------------- §3 边界/标签/非法
    h("§2 边界零权邻点含 NaN、越界掩码、标签半格、非法几何、坏清单项")
    # 源 8^3，2 mm 体素，原点 -2 mm；NaN 放在 (1,1,0) 与 (1,2,1)
    nan_src = source_grid((8, 8, 8), np.array(
        [[2, 0, 0, -2], [0, 2, 0, -2], [0, 0, 2, -2], [0, 0, 0, 1]], dtype=float))
    nan_src[1, 1, 0] = np.nan
    nan_src[1, 2, 1] = np.nan
    nan_aff = np.array([[2, 0, 0, -2], [0, 2, 0, -2], [0, 0, 2, -2], [0, 0, 0, 1]], dtype=float)
    write_vol("s2_nan", nan_src, nan_aff)
    tgt_edge = load_target({"shape": [20, 5, 5],
                            "affine": [[1, 0, 0, -4], [0, 2, 0, -2], [0, 0, 2, -2],
                                       [0, 0, 0, 1]]})
    tgt_edge_json = run_target({"shape": list(tgt_edge.shape),
                                "affine": tgt_edge.affine.tolist()}, "target_edge")
    write_manifest(DEMO / "batch_edge.json",
                   [{"path": "raw/s2_nan.nii", "sample_key": "s2_nan", "kind": "intensity"}],
                   "edge")
    idx_edge = run_batch(DEMO / "batch_edge.json", tgt_edge_json, DEMO / "edge",
                         chunk_voxels=64)
    arr, mask, meta = load_products(idx_edge, DEMO / "edge", idx_edge["samples"][0])[0]

    # s(2,0,0) = (1,0,0)：NaN 邻点 (1,2,1)/(1,1,0) 权重均为 0 -> 有效
    v, ok = reference.reference_single_point(nan_src.astype(np.float64), nan_aff,
                                             tgt_edge.affine, 6, 0, 0)
    # 目标 i=6 -> world x=2 -> s_x=2.0；y=0 -> 0；z=0 -> 0，取 src[2,0,0]
    check("整数中心处零权邻点含 NaN 仍有效（不访问/不要求不存在邻居）",
          ok and mask[6, 0, 0] == 1 and np.isclose(arr[6, 0, 0], nan_src[2, 0, 0]),
          f"ref={v}, got={arr[6,0,0]}")
    # 目标 i=4 -> world x=0 -> s_x=1.0；y=0 -> 0；z=0 -> 0，取 src[1,0,0]。
    # (1,0,0) 的其余邻点里，(1,1,0)/(1,2,1) 含 NaN，但它们的权重在整数中心处为 0。
    v, ok = reference.reference_single_point(nan_src.astype(np.float64), nan_aff,
                                             tgt_edge.affine, 4, 0, 0)
    check("另一整数中心：NaN 零权邻点不影响有效值",
          ok and mask[4, 0, 0] == 1 and np.isclose(arr[4, 0, 0], nan_src[1, 0, 0]),
          f"ref={v}, got={arr[4,0,0]}, src={nan_src[1,0,0]}")
    # 参与邻点含 NaN -> 无效：目标 (5,1,0) -> s=(1.5,1.0,0)，插值用到 (1,1,0)=NaN
    check("参与邻点含 NaN -> 该点无效，输出 NaN（不把 NaN 当 0）",
          mask[5, 1, 0] == 0 and math.isnan(float(arr[5, 1, 0])))
    # 边界：目标 i=16 -> world 12 -> s=7.0（n-1）；i=17 -> s=7.5 越界
    v, ok = reference.reference_single_point(nan_src.astype(np.float64),
                                             nan_aff, tgt_edge.affine, 16, 0, 0)
    check("上边界 s=n-1 有效，退化维度不要求不存在的高邻点",
          ok and mask[16, 0, 0] == 1 and np.isclose(arr[16, 0, 0], nan_src[7, 0, 0]),
          f"ref={v}, got={arr[16,0,0]}")
    # 越界：i=0 -> s_x=-2；i=19 -> s_x=15
    check("中心落在闭区间 [0,n-1] 外 -> 无效掩码 0 且强度为 NaN",
          mask[0, 0, 0] == 0 and np.isnan(arr[0, 0, 0])
          and mask[19, 0, 0] == 0 and np.isnan(arr[19, 0, 0]),
          f"valid={int(mask.sum())}/{mask.size}")
    # 有效/无效混合：有效点真值仍准确（除 NaN 处）
    tgrid = truth_grid(tgt_edge)
    good = mask.astype(bool) & np.isfinite(tgrid) & (tgrid == tgrid)
    maxdiff = float(np.max(np.abs(arr[good] - tgrid[good])))
    check("边界批次有效点仍与线性真值一致", maxdiff < 5e-3, f"最大误差 {maxdiff:.2e}")

    # 标签半格
    lab = (np.add.outer(np.arange(6), np.add.outer(np.arange(5), np.arange(6))) * 7).astype(np.int16)
    lab[0, 0, 0] = 0  # 真实类别 0
    lab_aff = np.array([[2, 0, 0, 0], [0, 2, 0, 0], [0, 0, 2, 0], [0, 0, 0, 1]], dtype=float)
    write_vol("s2_label", lab, lab_aff, dtype=np.int16)
    a_tl = np.array([[2, 0, 0, 1], [0, 2, 0, 0], [0, 0, 2, 0], [0, 0, 0, 1]], dtype=float)
    tgt_lab_json = run_target({"shape": [6, 5, 6], "affine": a_tl.tolist()}, "target_label")
    write_manifest(DEMO / "batch_label.json",
                   [{"path": "raw/s2_label.nii", "sample_key": "s2_label", "kind": "label"}],
                   "label")
    idx_lab = run_batch(DEMO / "batch_label.json", tgt_lab_json, DEMO / "label",
                        chunk_voxels=32)
    arr_l, mask_l, meta_l = load_products(idx_lab, DEMO / "label", idx_lab["samples"][0])[0]
    # 每个目标体素在 x 上都恰好半格（s_x = i+0.5） -> 取较小下标 i
    ref_v, ref_ok, _ = reference.reference_volume_loop(
        lab.astype(np.int64), lab_aff, a_tl, (6, 5, 6), label=True)
    cmp = reference.compare_chunked(lab.astype(np.int64), lab_aff, a_tl, (6, 5, 6),
                                    label=True, chunk_sizes=(1, 4, 32, 64))
    check("标签切块结果 == 逐点标量参考（含半格规则）",
          all(r["ok"] for r in cmp)
          and bool(np.array_equal(mask_l.astype(bool), ref_ok))
          and bool(np.array_equal(arr_l[ref_ok], ref_v[ref_ok])),
          str([(r["chunk_voxels"], r["ok"]) for r in cmp]))
    check("输出类别是源类别子集（不插出新类别）",
          set(np.unique(arr_l[mask_l.astype(bool)]).tolist()).issubset(
              set(np.unique(lab).tolist()))
          and bool(np.all(arr_l[mask_l == 0] == 0)))
    # 越界标签为 0 且掩码 0；真实类别 0 掩码 1 可区分
    outside = mask_l == 0
    check("无效标签位置输出 0，与真实类别 0（掩码 1）区分",
          bool(np.all(arr_l[outside] == 0))
          and bool(np.any((arr_l == 0) & (mask_l == 1))))
    # 特定的半格点：目标 (0,0,0) -> s=(0.5,0,0) -> 应取 x=0
    check("恰好半格（0.5）选择较小下标 0", arr_l[0, 0, 0] == lab[0, 0, 0],
          f"got={arr_l[0,0,0]}")

    # 非法几何与坏清单项
    write_vol("s3_unknown_units", base, A_STRAIGHT, units="unknown")
    write_vol("s3_both_codes_zero", base, A_STRAIGHT, sform_code=0, qform_code=0)
    p_nf = write_vol("s3_nonfinite_sform", base, A_STRAIGHT)
    patch_srows(p_nf, [[np.inf, 0, 0, 0], [0, 2, 0, -6], [0, 0, 2, -5]])
    p_sing = write_vol("s3_singular_sform", base, A_STRAIGHT)
    patch_srows(p_sing, [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]])
    write_vol("s3_float_label", np.zeros((4, 4, 4), dtype=np.float32), A_STRAIGHT,
              dtype=np.float32)
    neg = lab[:4, :4, :4].copy()
    neg[0, 0, 0] = -1
    write_vol("s3_negative_label", neg, lab_aff, dtype=np.int16)
    write_vol("s3_scaled_label", lab[:4, :4, :4], lab_aff, dtype=np.int16,
              slope=2.0, inter=1.0)
    five = np.zeros((4, 4, 4, 2, 2), dtype=np.float32)
    write_vol("s3_five_d", five, A_STRAIGHT)
    two = np.zeros((4, 4), dtype=np.float32)
    write_vol("s3_two_d", two, A_STRAIGHT)

    items_bad = [
        {"path": "raw/s3_unknown_units.nii", "sample_key": "bad_units", "kind": "intensity"},
        {"path": "raw/s3_both_codes_zero.nii", "sample_key": "bad_codes", "kind": "intensity"},
        {"path": "raw/s3_nonfinite_sform.nii", "sample_key": "bad_nonfinite", "kind": "intensity"},
        {"path": "raw/s3_singular_sform.nii", "sample_key": "bad_singular", "kind": "intensity"},
        {"path": "raw/s3_float_label.nii", "sample_key": "bad_float_label", "kind": "label"},
        {"path": "raw/s3_negative_label.nii", "sample_key": "bad_negative_label", "kind": "label"},
        {"path": "raw/s3_scaled_label.nii", "sample_key": "bad_scaled_label", "kind": "label"},
        {"path": "raw/s3_five_d.nii", "sample_key": "bad_five_d", "kind": "intensity"},
        {"path": "raw/s3_two_d.nii", "sample_key": "bad_two_d", "kind": "intensity"},
        {"path": "raw/s3_axis_missing.nii", "sample_key": "bad_missing", "kind": "intensity"},
        {"path": "raw/s1_axis.nii.gz", "sample_key": "good_axis", "kind": "intensity"},
        # 清单级坏项
        {"path": "raw/s1_axis.nii.gz", "kind": "intensity"},
        {"path": "raw/s1_axis.nii.gz", "sample_key": "bad_kind", "kind": "mask"},
        {"path": "raw/s1_axis.nii.gz", "sample_key": "bad key/with space", "kind": "intensity"},
        {"path": "raw/s1_axis.nii.gz", "sample_key": "good_axis2", "kind": "intensity"},
        "not-an-object",
        {"path": "raw/s1_axis.nii.gz", "sample_key": "dup_key", "kind": "intensity"},
        {"path": "raw/s1_meter.nii.gz", "sample_key": "dup_key", "kind": "intensity"},
    ]
    write_manifest(DEMO / "batch_bad.json", items_bad, "bad")
    idx_bad = run_batch(DEMO / "batch_bad.json", tgt_json, DEMO / "bad", chunk_voxels=512)
    diag = idx_bad["diagnostics"]
    reasons = {d.get("sample_key") or d.get("path") or f"item{d['item']}": d["reason"]
               for d in diag}
    print(f"      批次 bad: 完成 {idx_bad['n_completed']}，诊断 {len(diag)}")
    for d in diag:
        print(f"        - item {d.get('item')} {d.get('sample_key') or d.get('path')}: "
              f"{d['reason']}")
    check("合法项照常完成，坏项逐条诊断不冒充样本",
          idx_bad["n_completed"] == 2
          and all(any(d.get("sample_key") == k for d in diag) for k in
                  ("bad_units", "bad_codes", "bad_nonfinite", "bad_singular",
                   "bad_float_label", "bad_negative_label", "bad_scaled_label",
                   "bad_five_d", "bad_two_d", "bad_missing", "dup_key")),
          f"completed={idx_bad['n_completed']}, diags={len(diag)}")
    check("坏项均带明确原因（未知单位/无有效几何/非有限/不可逆/类型/缩放/维数/缺失）",
          any("未知空间单位" in d["reason"] or "未知单位" in d["reason"] for d in diag)
          and any("没有可用的有效几何" in d["reason"] for d in diag)
          and any("非有限" in d["reason"] for d in diag)
          and any("不可逆" in d["reason"] for d in diag)
          and any("整数" in d["reason"] for d in diag)
          and any("负数" in d["reason"] for d in diag)
          and any("恒等" in d["reason"] for d in diag)
          and any("三维或四维" in d["reason"] for d in diag)
          and any("不存在" in d["reason"] for d in diag))
    check("清单坏项（缺 sample_key / 非法 kind / 不安全键 / 重复样本）被诊断",
          any("sample_key" in d["reason"] and "缺少" in d["reason"] for d in diag)
          and any("kind 须为" in d["reason"] for d in diag)
          and any("不安全字符" in d["reason"] for d in diag)
          and any("无法唯一关联" in d["reason"] for d in diag)
          and any("条目须为 JSON 对象" in d["reason"] for d in diag))
    # 好的那项产物可用
    rec_good = rec_for(idx_bad, "good_axis")
    ga, gm, gmeta = load_products(idx_bad, DEMO / "bad", rec_good)[0]
    check("坏批次中正常项产物仍可读且真值正确",
          bool(np.all(gm.astype(bool)) and max_err(ga, gm.astype(bool), tgt_core) < 5e-3))

    # 末行非仿射：NIfTI 头固定末行，无法从磁盘构造；直接校验生产校验器拒绝
    from org_er.volprep.geometry import validate_affine
    bad_aff = np.eye(4)
    bad_aff[3, 0] = 0.5
    ok, why = validate_affine(bad_aff, record="unit")
    check("非仿射末行被校验器拒绝（不默认单位矩阵）", ok is None and "末行" in why, why)

    # -------------------------------------------------------------- §4 参考
    h("§3 小数组逐点标量参考核对切块结果（独立实现，不调用生产插值）")
    small_src = source_grid((5, 6, 4), np.array([[2.2, 0.5, 0.1, -1],
                                                 [0.2, 1.9, -0.3, 2],
                                                 [-0.1, 0.4, 2.5, 3], [0, 0, 0, 1]]))
    small_aff = np.array([[2.2, 0.5, 0.1, -1], [0.2, 1.9, -0.3, 2],
                          [-0.1, 0.4, 2.5, 3], [0, 0, 0, 1]])
    small_tgt_aff = np.array([[1.1, 0.3, 0, -3], [0.1, 1.4, 0.2, 0],
                              [0, 0.2, 1.6, 1], [0, 0, 0, 1]])
    reps = reference.compare_chunked(small_src, small_aff, small_tgt_aff, (7, 6, 5),
                                     chunk_sizes=(1, 2, 3, 5, 64, 1 << 16))
    for r in reps:
        print(f"        chunk={r['chunk_voxels']:>6}: mask_eq={r['mask_equal']} "
              f"values_eq={r['values_equal']} max|diff|={r['max_abs_diff']}")
    check("强度：所有块大小与逐点标量参考一致", all(r["ok"] for r in reps),
          f"{len(reps)} 种块大小")

    small_lab = (np.add.outer(np.arange(5), np.add.outer(
        np.arange(6), np.arange(4))).astype(np.int64) * 9)
    reps_l = reference.compare_chunked(small_lab, small_aff, small_tgt_aff, (7, 6, 5),
                                       label=True, chunk_sizes=(1, 2, 3, 64))
    check("标签：所有块大小与逐点标量参考一致（半格取小）",
          all(r["ok"] for r in reps_l),
          str([(r["chunk_voxels"], r["mask_equal"], r["values_equal"]) for r in reps_l]))
    # 恰好半格的显式点：参考也逐点核对
    s = reference.to_source(small_aff, reference.to_world(small_tgt_aff, 1, 1, 1))
    ref_v, ref_ok = reference.ref_label_point(small_lab, small_aff, small_tgt_aff, 1, 1, 1)
    inside = all(0.0 <= s[d] <= small_lab.shape[d] - 1 for d in range(3))
    check("逐点标量参考在任意目标点可直接给出标签类别（含越界判定）",
          ref_ok == inside and (not inside or isinstance(ref_v, int)),
          f"s={np.round(s, 4).tolist()} -> valid={ref_ok}, label={ref_v}")

    # -------------------------------------------------------------- §5 块上限
    h("§4 按目标切块：不构造整卷/四维坐标云")
    calls = {"n": 0, "max": 0}
    orig = R.source_continuous_indices

    def wrapped(*a, **k):
        out = orig(*a, **k)
        calls["n"] += 1
        calls["max"] = max(calls["max"], int(out.shape[0]))
        return out

    R.source_continuous_indices = wrapped
    try:
        big_tgt_shape = (40, 36, 30)  # 43200 目标体素
        _, _ = R.resample_intensity(small_src, small_aff, small_tgt_aff, big_tgt_shape,
                                    chunk_voxels=1000)
    finally:
        R.source_continuous_indices = orig
    check("块坐标顶点数不超过块上限（与大体积无关）",
          calls["max"] <= 1000 and calls["n"] > 1,
          f"调用 {calls['n']} 块，单块最多 {calls['max']} 个目标点")
    plan = R.plan_chunks((40, 36, 30), 1000)
    check("块计划体积不超过上限且覆盖完整",
          bool(np.prod(plan.chunk_shape) <= 1000 and plan.n_chunks == calls["n"]),
          f"chunk_shape={plan.chunk_shape}, n_chunks={plan.n_chunks}")

    # -------------------------------------------------------------- §6 读回
    h("§5 产物读回不依赖原文件；旧机构/贡献命令继续可用")
    # 把原始数据与清单副本移走，仍可读回全部产物
    shutil.move(str(RAW), str(DEMO / "raw_offline"))
    (DEMO / "batch_core.json").rename(DEMO / "batch_core.json.offline")
    (DEMO / "core" / "manifest.copy.json").rename(DEMO / "core" / "manifest.copy.json.offline")
    rb_ok = True
    n_products = 0
    for rec in idx_bad["samples"] + index["samples"]:
        which = DEMO / ("bad" if rec in idx_bad["samples"] else "core")
        for f in rec["frames"]:
            try:
                arr, mask, meta = load_product(which / "products" / f["meta_file"])
                n_products += 1
                rb_ok &= arr.shape == tuple(meta["target"]["shape"])
                rb_ok &= meta["sample_key"] == rec["sample_key"]
            except Exception:
                rb_ok = False
    check("删除/移走原始 NIfTI 与清单副本后，产物仍可完整读回",
          rb_ok and n_products > 0, f"读回 {n_products} 件产物")
    # 索引也可读回
    from org_er.volprep.job import read_batch
    idx_back = read_batch(DEMO / "core")
    check("批次索引可读回（样本键/帧号/源几何/目标几何齐全）",
          idx_back["n_completed"] == 9 and
          all(k in idx_back["samples"][0]["frames"][0] for k in
              ("frame_index", "meta_file", "array_file", "mask_file")))
    # 产物带校验和，篡改可被发现
    p = DEMO / "core" / "products" / index["samples"][0]["frames"][0]["array_file"]
    saved = p.read_bytes()
    p.write_bytes(saved + b"\x00")
    tamper_detected = False
    try:
        load_product(DEMO / "core" / "products" / index["samples"][0]["frames"][0]["meta_file"])
    except ValueError as e:
        tamper_detected = "校验和不符" in str(e)
    finally:
        p.write_bytes(saved)
    check("产物内容校验和可发现篡改", tamper_detected)

    # 旧命令：机构验证与贡献命令帮助
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "verify.py")],
                       cwd=str(ROOT), capture_output=True, text=True)
    check("既有机构管线核验命令仍通过", r.returncode == 0,
          r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")
    r2 = subprocess.run([sys.executable, str(ROOT / "scripts" / "contrib.py"), "--help"],
                        cwd=str(ROOT), capture_output=True, text=True)
    check("既有贡献作业命令仍可用", r2.returncode == 0 and "--er-db" in r2.stdout)

    # 命令行入口实跑（独立目录）
    h("§6 命令行入口（scripts/volprep.py）实跑")
    cli = DEMO / "cli"
    cli.mkdir()
    write_manifest(cli / "batch.json",
                   [{"path": "../raw_offline/s1_axis.nii.gz", "sample_key": "cli_axis",
                     "kind": "intensity"}], "cli")
    (cli / "target.json").write_text(tgt_json.read_text(encoding="utf-8"), encoding="utf-8")
    r3 = subprocess.run([sys.executable, str(ROOT / "scripts" / "volprep.py"),
                         "--manifest", str(cli / "batch.json"),
                         "--target", str(cli / "target.json"),
                         "--out", str(cli / "out")],
                        cwd=str(ROOT), capture_output=True, text=True)
    print(r3.stdout.strip())
    if r3.returncode != 0:
        print(r3.stderr.strip())
    check("命令行批处理入口可运行", r3.returncode == 0 and (cli / "out" / "index.json").is_file())
    # CLI 读回
    idx_cli = read_batch(cli / "out")
    arr_c, mask_c, _ = load_product(cli / "out" / "products" /
                                    idx_cli["samples"][0]["frames"][0]["meta_file"])
    check("CLI 产物读回且值正确",
          bool(mask_c.all()) and max_err(arr_c, mask_c.astype(bool), tgt_core) < 5e-3)

    # -------------------------------------------------------------- 汇总
    h("汇总")
    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"共 {len(CHECKS)} 项断言，通过 {passed} 项。")
    if passed != len(CHECKS):
        for name, ok in CHECKS:
            if not ok:
                print("  FAIL:", name)
    print("总体:", "PASS" if passed == len(CHECKS) else "FAIL")
    raise SystemExit(0 if passed == len(CHECKS) else 1)


if __name__ == "__main__":
    main()
