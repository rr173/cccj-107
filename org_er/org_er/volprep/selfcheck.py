"""体数据整备的进程内自检：坏批次逐项诊断、原子发布行为。

这些情形不需要完整演示（不重新合成体数据），只验证作业层的行为：
坏项不产生产物、正常项不受影响、失败时上一次成功结果保持不动。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import numpy as np

from .geometry import GeometryError, load_target, validate_affine
from .job import process_item, run_batch
from .manifest import ManifestItem, parse_manifest, product_stem


def _write_nii(path, data, affine, *, units="mm", sform_code=2, qform_code=0,
               slope=None, inter=None):
    import nibabel as nib

    hdr = nib.Nifti1Header()
    hdr.set_data_dtype(np.asarray(data).dtype)
    img = nib.Nifti1Image(np.asarray(data), None, header=hdr)
    img.header.set_xyzt_units(units)
    img.header.set_sform(None if sform_code == 0 else affine, code=sform_code)
    img.header.set_qform(None if qform_code == 0 else affine, code=qform_code)
    if slope is not None:
        img.header["scl_slope"] = float(slope)
        img.header["scl_inter"] = 0.0 if inter is None else float(inter)
    nib.save(img, str(path))
    return path


def check_manifest_schema(tmp: Path) -> list:
    """清单坏行在解析阶段就被诊断，且不影响合法条目。"""
    problems = []
    good = tmp / "good.nii"
    _write_nii(good, np.zeros((4, 4, 4), np.float32), np.eye(4))
    doc = {
        "batch_id": "schema",
        "items": [
            {"path": "good.nii", "sample_key": "ok", "kind": "intensity"},
            {"path": "good.nii", "kind": "intensity"},
            {"path": "good.nii", "sample_key": "bad_kind", "kind": "mask"},
            {"path": "good.nii", "sample_key": "a b", "kind": "intensity"},
            "x",
        ],
    }
    mf_path = tmp / "manifest.json"
    mf_path.write_text(json.dumps(doc), encoding="utf-8")
    mf = parse_manifest(mf_path)
    if [i.sample_key for i in mf.items] != ["ok"]:
        problems.append(f"合法条目筛选错误: {[i.sample_key for i in mf.items]}")
    reasons = " ".join(p["reason"] for p in mf.problems)
    for needle in ("缺少 sample_key", "kind 须为", "不安全字符", "条目须为 JSON 对象"):
        if needle not in reasons:
            problems.append(f"缺诊断: {needle}")
    return problems


def check_item_failure_cleanup(tmp: Path) -> list:
    """坏项不落任何产物；同批正常项照常完成。"""
    problems = []
    target = load_target({"shape": [6, 6, 6],
                          "affine": [[2, 0, 0, 0], [0, 2, 0, 0], [0, 0, 2, 0], [0, 0, 0, 1]]})
    staging = tmp / "stage"
    (staging / "products").mkdir(parents=True)
    bad = tmp / "bad.nii"
    _write_nii(bad, np.zeros((4, 4, 4), np.float32), np.eye(4), units="unknown")
    try:
        process_item(ManifestItem(0, "bad.nii", "bad", "intensity", bad), target,
                     staging, 256)
    except GeometryError as e:
        if "未知单位" not in str(e):
            problems.append(f"未知单位诊断不符: {e}")
    else:
        problems.append("未知单位未被拒绝")
    if list((staging / "products").iterdir()):
        problems.append("坏项留下了半成品产物")
    out = tmp / "out"
    idx = run_batch(tmp / "manifest_bad.json", tmp / "target.json", out, chunk_voxels=256)
    if idx["n_completed"] != 1:
        problems.append(f"正常项未完成: {idx}")
    return problems


def check_atomic_publish(tmp: Path) -> list:
    """成功发布后旧目录换新；失败运行不破坏上一次结果。"""
    problems = []
    out = tmp / "atomic_out"
    out.mkdir()
    (out / "sentinel.txt").write_text("old", encoding="utf-8")
    target_json = tmp / "target.json"
    if not target_json.exists():
        target_json.write_text(json.dumps({"shape": [4, 4, 4], "affine": np.eye(4).tolist()}),
                               encoding="utf-8")
    idx = run_batch(tmp / "manifest_bad.json", target_json, out, chunk_voxels=256)
    if not (out / "index.json").is_file():
        problems.append("成功批次未发布 index.json")
    if (out / "sentinel.txt").exists():
        problems.append("旧目录内容残留（应按目录整体换入）")
    # 失败运行（目标非法）不应删除已有结果
    before = (out / "index.json").read_text(encoding="utf-8")
    bad_target = tmp / "bad_target.json"
    bad_target.write_text(json.dumps({"shape": [4, 4, 4],
                                      "affine": np.zeros((4, 4)).tolist()}), encoding="utf-8")
    try:
        run_batch(tmp / "manifest_bad.json", bad_target, out, chunk_voxels=256)
    except ValueError:
        pass
    if (out / "index.json").read_text(encoding="utf-8") != before:
        problems.append("失败运行改动了上一次成功结果")
    if list(tmp.glob("atomic_out.staging-*")):
        problems.append("失败运行留下了暂存目录")
    return problems


def check_invalid_targets(tmp: Path) -> list:
    problems = []
    for spec, needle in (
        ({"shape": [4, 4], "affine": np.eye(4).tolist()}, "3 个整数"),
        ({"shape": [4, 4, 4]}, "shape 与 affine"),
        ({"shape": [4, 4, 4], "affine": np.diag([1.0, 1.0, 0.0, 1.0]).tolist()}, "不可逆"),
        ({"shape": [4, 4, 4], "affine": np.eye(4).tolist(), "spatial_unit": "meter"}, "mm"),
        ({"shape": [4, -1, 4], "affine": np.eye(4).tolist()}, "正整数"),
    ):
        try:
            load_target(spec)
        except GeometryError as e:
            if needle not in str(e):
                problems.append(f"目标网格诊断不符: {spec} -> {e}")
        else:
            problems.append(f"非法目标网格未被拒绝: {spec}")
    bad_aff = np.eye(4)
    bad_aff[3, 0] = 1.0
    ok, why = validate_affine(bad_aff)
    if ok is not None:
        problems.append("非仿射末行未被拒绝")
    return problems


def check_metadata_strict_json(tmp: Path) -> list:
    """产物元数据必须是严格 JSON（不允许 NaN/Infinity 字面量）。"""
    problems = []
    for p in sorted((tmp / "out" / "products").glob("*.json")):
        try:
            json.loads(p.read_text(encoding="utf-8"),
                       parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))
        except ValueError as e:
            problems.append(f"{p.name} 不是严格 JSON: {e}")
    return problems


def selfcheck(tmp_parent=None) -> list:
    """运行全部进程内自检，返回问题列表（空 = 通过）。"""
    problems = []
    tmp = Path(tempfile.mkdtemp(prefix="volprep-selftest-", dir=tmp_parent))
    try:
        (tmp / "manifest_bad.json").write_text(json.dumps({
            "batch_id": "bad",
            "items": [
                {"path": "good.nii", "sample_key": "good", "kind": "intensity"},
                {"path": "bad.nii", "sample_key": "bad", "kind": "intensity"},
                {"path": "missing.nii", "sample_key": "missing", "kind": "intensity"},
            ],
        }), encoding="utf-8")
        (tmp / "target.json").write_text(json.dumps({"shape": [4, 4, 4],
                                                     "affine": np.eye(4).tolist()}),
                                         encoding="utf-8")
        _write_nii(tmp / "good.nii", np.arange(64, dtype=np.float32).reshape(4, 4, 4),
                   np.eye(4))
        _write_nii(tmp / "bad.nii", np.zeros((4, 4, 4), np.float32), np.eye(4),
                   units="unknown")
        problems += check_manifest_schema(tmp)
        problems += check_invalid_targets(tmp)
        problems += check_item_failure_cleanup(tmp)
        problems += check_atomic_publish(tmp)
        problems += check_metadata_strict_json(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems


if __name__ == "__main__":  # pragma: no cover
    probs = selfcheck()
    for p in probs:
        print("FAIL:", p)
    print("volprep selfcheck:", "PASS" if not probs else "FAIL")
    raise SystemExit(0 if not probs else 1)
