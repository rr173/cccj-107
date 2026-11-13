#!/usr/bin/env python3
"""生成体数据整备的样本清单、目标网格与 3 个小 NIfTI（data/volprep/）。

  python3 scripts/make_volprep_sample.py

强度真值是世界坐标的线性函数：f(x,y,z) = 0.5x - 1.25y + 2.5z + 7。
样本文件：sub-01_T1.nii.gz（缩放强度 int16，scl_slope=0.5/scl_inter=-1.5）、
sub-01_seg.nii（标签）、sub-02_T1_4d.nii（3 帧 4D 强度，帧间偏移 100）。
"""
import json
from pathlib import Path

import numpy as np

import _bootstrap  # noqa: F401
import nibabel as nib

ROOT = _bootstrap.ROOT
OUT = ROOT / "data" / "volprep"
RAW = OUT / "raw"

A = np.array([[2.0, 0, 0, -7], [0, 2.0, 0, -6], [0, 0, 2.0, -5], [0, 0, 0, 1.0]])
SHAPE = (16, 14, 13)


def truth(x, y, z):
    return 0.5 * x - 1.25 * y + 2.5 * z + 7.0


def source_grid(shape, affine):
    idx = np.stack(np.meshgrid(*[np.arange(s) for s in shape], indexing="ij"), axis=-1)
    w = idx @ np.asarray(affine, dtype=np.float64)[:3, :3].T + np.asarray(affine)[:3, 3]
    return truth(w[..., 0], w[..., 1], w[..., 2]).astype(np.float32)


def write_nii(path, data, affine, *, units="mm", slope=None, inter=None):
    data = np.asarray(data)
    hdr = nib.Nifti1Header()
    hdr.set_data_dtype(data.dtype)
    img = nib.Nifti1Image(data, None, header=hdr)
    img.header.set_xyzt_units(units)
    img.header.set_sform(affine, code=2)
    img.header.set_qform(None, code=0)
    if slope is not None:
        img.header["scl_slope"] = float(slope)
        img.header["scl_inter"] = 0.0 if inter is None else float(inter)
    img._affine = None  # 保存时原样写头字段，不按 affine 重写
    nib.save(img, str(path))
    got = nib.load(str(path))
    assert int(got.header["sform_code"]) == 2, path
    if slope is not None:
        assert abs(float(got.dataobj.slope) - float(slope)) < 1e-6, path


def main():
    RAW.mkdir(parents=True, exist_ok=True)
    base = source_grid(SHAPE, A)
    # 缩放强度：raw = (f - inter) / slope = 2f + 3（在源体素中心处恰为整数）
    raw = np.rint(2.0 * base + 3.0).astype(np.int16)
    write_nii(RAW / "sub-01_T1.nii.gz", raw, A, slope=0.5, inter=-1.5)
    lab = (np.add.outer(np.arange(SHAPE[0]),
                        np.add.outer(np.arange(SHAPE[1]),
                                     np.arange(SHAPE[2]))) % 4 + 1).astype(np.int16)
    lab[0, 0, 0] = 0
    write_nii(RAW / "sub-01_seg.nii", lab, A)
    write_nii(RAW / "sub-02_T1_4d.nii",
              np.stack([base + 100.0 * f for f in range(3)], axis=3).astype(np.float32), A)

    (OUT / "target.json").write_text(json.dumps({
        "shape": [16, 14, 12],
        "affine": [[1.5, 0, 0, -6], [0, 1.5, 0, -5], [0, 0, 1.5, -4], [0, 0, 0, 1]],
    }, indent=2) + "\n", encoding="utf-8")
    (OUT / "batch.json").write_text(json.dumps({
        "batch_id": "sample_batch",
        "items": [
            {"path": "raw/sub-01_T1.nii.gz", "sample_key": "sub-01", "kind": "intensity"},
            {"path": "raw/sub-01_seg.nii", "sample_key": "sub-01", "kind": "label"},
            {"path": "raw/sub-02_T1_4d.nii", "sample_key": "sub-02", "kind": "intensity"},
        ],
    }, indent=2) + "\n", encoding="utf-8")
    print(f"样本已写入 {OUT}（raw/ 3 个文件 + batch.json + target.json）")


if __name__ == "__main__":
    main()
