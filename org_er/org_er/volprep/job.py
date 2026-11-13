"""批次整备作业：清单 -> 逐项读取/校验几何 -> 分块重采样 -> 产物 + 索引。

产物目录（整批一次发布）::

    <out>/
      index.json            批次索引：目标几何、逐项结果、帧清单、诊断
      diagnostics.json      全部坏项（清单级 / 读取级），不冒充已完成样本
      manifest.copy.json    本次使用的清单快照（相对路径原样保留）
      products/
        <stem>.npy          浮点 float32（强度）或 int32（标签）
        <stem>.mask.npy     uint8 掩码（1=有效，0=无效）
        <stem>.json         该帧的元数据：样本键、帧号、源/目标几何、缩放等

每个 item 单独处理：坏项只记诊断，正常项继续。产物写入暂存目录，全部成功后才
整体换入目标目录；失败时上一次成功结果保持原样。读回只依赖产物目录本身
（``load_product`` / ``load_index``），不需要原始 NIfTI 文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .geometry import (GeometryError, choose_source_geometry, ensure_integer_labels,
                       intensity_scaling, label_scaling, load_target)
from .manifest import (ManifestItem, iter_sample_products, load_index, parse_manifest,
                       product_stem, sha256_file)
from .resample import DEFAULT_CHUNK_VOXELS, plan_chunks, resample_intensity, resample_label

MASK_NOTE = ("mask=1 -> 有效位置；mask=0 -> 无效位置。强度无效位置为 NaN；"
             "标签无效位置为 0，必须用掩码与真实类别 0 区分")


# ------------------------------------------------------------------ 单项诊断
@dataclass
class ItemDiagnosis:
    item: int
    path: str
    sample_key: str
    kind: str
    reason: str
    stage: str = "read"

    def as_dict(self) -> dict:
        return {"item": int(self.item), "path": self.path, "sample_key": self.sample_key,
                "kind": self.kind, "status": "rejected", "stage": self.stage,
                "reason": self.reason}


def _source_shape_meta(shape) -> dict:
    spatial = [int(s) for s in shape[:3]]
    return {
        "spatial_shape": spatial,
        "n_frames": int(shape[3]) if len(shape) == 4 else 1,
        "dims": len(shape),
    }


def _frame_unscaled(img, frame: int) -> np.ndarray:
    """读取一帧的**原始存储值**（不应用头缩放），缩放由调用方按自己的规则应用。

    nibabel 的数据代理由 ``np.asanyarray(dataobj[...])`` 时会自动套用
    ``scl_slope/scl_inter``；这里改走 ``get_unscaled``，避免重复缩放。
    仅物化源数组（允许），不构造四维坐标云。
    """
    try:
        raw = img.dataobj.get_unscaled()
    except AttributeError:  # 内存数组等非代理数据
        raw = np.asanyarray(img.dataobj)
    raw = np.asanyarray(raw)
    if len(img.shape) == 4:
        return raw[..., frame]
    return raw


def _frame_array(img, frame: int) -> np.ndarray:
    """兼容旧调用名（内部一律用 _frame_unscaled）。"""
    return _frame_unscaled(img, frame)


# ------------------------------------------------------------------ 单项处理
def process_item(item: ManifestItem, target, staging: Path, chunk_voxels: int) -> dict:
    """处理一个清单项；任何校验失败抛 GeometryError/ValueError 等，由调用方诊断。"""
    import nibabel as nib

    source_sha = sha256_file(item.abs_path)
    # 逐项预校验：先做所有“整项级”判定（几何/单位/类型/缩放/维数），
    # 任何一项失败都不写产物、不进入逐帧工作。
    try:
        img = nib.load(str(item.abs_path))
    except Exception as e:
        raise ValueError(f"无法读取 NIfTI: {e}") from e
    # nibabel 图像没有上下文管理器：改用它的 proxy 关闭钩子（若存在），
    # 避免逐项加载时积累文件句柄。
    try:
        return _process_item_open(item, img, target, staging, chunk_voxels, source_sha)
    finally:
        close = getattr(img.dataobj, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def _process_item_open(item, img, target, staging, chunk_voxels, source_sha) -> dict:
    shape = tuple(int(s) for s in img.shape)
    if len(shape) not in (3, 4):
        raise ValueError(f"仅接受三维或四维文件，实际 {len(shape)} 维 shape={shape}")
    if any(s <= 0 for s in shape):
        raise ValueError(f"维度须为正，实际 shape={shape}")

    geom = choose_source_geometry(img.header, shape)

    if item.kind == "label":
        if np.dtype(img.get_data_dtype()).kind not in ("i", "u"):
            raise ValueError(f"标签存储类型须为整数，实际 {img.get_data_dtype()}")
        scaling = label_scaling(img.dataobj, img.header)
    else:
        scaling = intensity_scaling(img.dataobj, img.header)
        if np.dtype(img.get_data_dtype()).kind not in ("i", "u", "f"):
            raise ValueError(f"强度存储类型须为实数，实际 {img.get_data_dtype()}")

    n_frames = shape[3] if len(shape) == 4 else 1
    sinv = np.linalg.inv(geom.affine)
    frames = []
    written: list[Path] = []
    try:
        for frame in range(n_frames):
            raw = _frame_unscaled(img, frame)
            if item.kind == "label":
                if raw.ndim != 3:
                    raise ValueError(f"标签帧须为三维，实际 {raw.shape}")
                ensure_integer_labels(raw, raw.dtype)
                arr, mask = resample_label(raw, geom.affine, target.affine, target.shape,
                                           chunk_voxels=chunk_voxels)
            else:
                if raw.ndim != 3:
                    raise ValueError(f"强度帧须为三维，实际 {raw.shape}")
                scaled = scaling.apply(np.asarray(raw))
                if np.iscomplexobj(scaled):
                    raise ValueError("强度值不可为复数")
                arr, mask = resample_intensity(scaled, geom.affine, target.affine,
                                               target.shape, chunk_voxels=chunk_voxels)

            stem = f"i{item.index:04d}__{product_stem(item.sample_key, item.kind, frame)}"
            array_file = f"{stem}.npy"
            mask_file = f"{stem}.mask.npy"
            array_path = staging / "products" / array_file
            mask_path = staging / "products" / mask_file
            np.save(array_path, arr, allow_pickle=False)
            np.save(mask_path, mask, allow_pickle=False)
            written.extend([array_path, mask_path])

            meta = {
                "sample_key": item.sample_key,
                "kind": item.kind,
                "frame_index": int(frame),
                "frame_count": int(n_frames),
                "manifest": {"item_index": int(item.index), "path": item.path},
                "source": {
                    "sha256": source_sha,
                    "shape": list(shape),
                    **_source_shape_meta(shape),
                    "storage_dtype": str(np.dtype(img.get_data_dtype())),
                    "geometry": geom.meta(),
                    "scaling": scaling.meta(),
                },
                "target": target.meta(),
                "array_file": array_file,
                "mask_file": mask_file,
                "array_sha256": sha256_file(array_path),
                "mask_sha256": sha256_file(mask_path),
                "array_dtype": str(arr.dtype),
                "mask_dtype": str(mask.dtype),
                "mask_meaning": MASK_NOTE,
                "valid_voxels": int(mask.sum()),
                "total_voxels": int(mask.size),
                "chunk_voxels": int(chunk_voxels),
            }
            if item.kind == "intensity":
                finite = arr[np.isfinite(arr)]
                meta["intensity_stats"] = {
                    "n_valid": int(mask.sum()),
                    "n_invalid": int(mask.size - mask.sum()),
                    "min": float(finite.min()) if finite.size else None,
                    "max": float(finite.max()) if finite.size else None,
                }
            else:
                vals, counts = np.unique(arr, return_counts=True)
                meta["label_classes"] = {str(int(v)): int(c) for v, c in zip(vals, counts)}
                meta["label_class_zero_note"] = (
                    "label=0 可能表示真实类别 0（mask=1），也可能是无效位置（mask=0）")
            meta_path = staging / "products" / f"{stem}.json"
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2,
                                            allow_nan=False), encoding="utf-8")
            written.append(meta_path)
            frames.append({
                "frame_index": int(frame),
                "stem": stem,
                "array_file": array_file,
                "mask_file": mask_file,
                "meta_file": f"{stem}.json",
                "valid_voxels": int(mask.sum()),
            })
    except BaseException:
        # 该 item 失败：删掉它已写出的半成品，不冒充已完成样本
        for p in written:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        raise

    return {
        "item_index": int(item.index),
        "path": item.path,
        "sample_key": item.sample_key,
        "kind": item.kind,
        "status": "ok",
        "source_sha256": source_sha,
        "source_shape": list(shape),
        "n_frames": int(n_frames),
        "geometry_source": geom.geom_source,
        "spatial_unit": geom.source_spatial_unit,
        "frames": frames,
        "products": ["products/" + f["meta_file"] for f in frames],
        "target_shape": [int(s) for s in target.shape],
        "chunk_voxels": int(chunk_voxels),
        "chunk_shape": list(plan_chunks(target.shape, chunk_voxels).chunk_shape),
    }


# ------------------------------------------------------------------ 批作业
def _promote(staging: Path, out_dir: Path) -> None:
    """把暂存目录换入目标目录；旧目录先退为 .prev-<id>，成功后删除。"""
    prev = None
    if out_dir.exists():
        prev = out_dir.with_name(out_dir.name + ".prev-" + uuid.uuid4().hex[:8])
        os.replace(out_dir, prev)
    os.replace(staging, out_dir)
    if prev is not None:
        shutil.rmtree(prev, ignore_errors=True)


def run_batch(manifest_path, target_path, out_dir, *, chunk_voxels: int = DEFAULT_CHUNK_VOXELS,
              max_items=None) -> dict:
    """执行一整批整备；返回索引 dict（已落盘）。"""
    manifest = parse_manifest(manifest_path)
    target_spec_bytes = Path(target_path).read_bytes()
    try:
        target_spec = json.loads(target_spec_bytes)
    except json.JSONDecodeError as e:
        raise ValueError(f"目标网格 JSON 不合法: {e}") from e
    try:
        target = load_target(target_spec)
    except GeometryError as e:
        raise ValueError(f"目标网格被拒绝: {e}") from e
    if chunk_voxels < 1:
        raise ValueError("chunk_voxels 须为正整数")

    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.with_name(out_dir.name + ".staging-" + uuid.uuid4().hex[:8])
    (staging / "products").mkdir(parents=True, exist_ok=False)

    diagnostics = [dict(p) for p in manifest.problems]
    completed = []
    items = manifest.items if max_items is None else manifest.items[:int(max_items)]

    # 同一样本键的同一类型只允许一份清单项（否则产物无法唯一关联样本）
    seen = {}
    for it in items:
        seen.setdefault((it.sample_key, it.kind), []).append(it.index)
    ambiguous = {k: v for k, v in seen.items() if len(v) > 1}

    try:
        for it in items:
            if (it.sample_key, it.kind) in ambiguous:
                diagnostics.append(ItemDiagnosis(
                    it.index, it.path, it.sample_key, it.kind,
                    f"sample_key+kind 在清单中重复出现（items {ambiguous[(it.sample_key, it.kind)]}），"
                    "无法唯一关联产物", "manifest").as_dict())
                continue
            if not it.abs_path.is_file():
                diagnostics.append(ItemDiagnosis(
                    it.index, it.path, it.sample_key, it.kind,
                    f"文件不存在: {it.path}", "read").as_dict())
                continue
            try:
                rec = process_item(it, target, staging, chunk_voxels)
            except (GeometryError, ValueError, OSError) as e:
                diagnostics.append(ItemDiagnosis(
                    it.index, it.path, it.sample_key, it.kind, str(e),
                    "geometry" if isinstance(e, GeometryError) else "resample").as_dict())
                continue
            completed.append(rec)

        index = {
            "batch_id": manifest.batch_id,
            "manifest_path_name": manifest.path.name,
            "target": target.meta(),
            "target_json_sha256": sha256_file(target_path),
            "target_json_bytes_sha256": hashlib.sha256(target_spec_bytes).hexdigest(),
            "chunk_voxels": int(chunk_voxels),
            "n_items_listed": len(manifest.items),
            "n_items_attempted": len(items),
            "n_completed": len(completed),
            "n_diagnostics": len(diagnostics),
            "mask_meaning": MASK_NOTE,
            "products_dir": "products",
            "samples": completed,
            "diagnostics": diagnostics,
        }
        (staging / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8")
        (staging / "diagnostics.json").write_text(
            json.dumps({"batch_id": manifest.batch_id, "diagnostics": diagnostics},
                       ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        (staging / "manifest.copy.json").write_text(
            manifest.path.read_text(encoding="utf-8"), encoding="utf-8")
        _promote(staging, out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return index


def read_batch(out_dir) -> dict:
    """读回批次索引（不打开任何原始文件）。"""
    return load_index(Path(out_dir) / "index.json")


def read_sample(out_dir, sample_key, kind="intensity") -> list:
    """读回某样本某类型的全部帧产物 [(frame, array, mask, meta)]。"""
    return list(iter_sample_products(out_dir, sample_key, kind))
