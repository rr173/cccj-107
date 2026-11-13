"""批次清单解析与产物读回（与原始 NIfTI 文件解耦）。

清单格式（JSON 对象）::

    {
      "batch_id": "可选，缺省用 run 目录名",
      "items": [
        {"path": "raw/sub-01_T1.nii.gz", "sample_key": "sub-01", "kind": "intensity"},
        {"path": "raw/sub-01_seg.nii",   "sample_key": "sub-01", "kind": "label"},
        {"path": "raw/sub-02_4d.nii",    "sample_key": "sub-02", "kind": "intensity"}
      ]
    }

* ``path`` 为相对清单所在目录的文件名；也接受绝对路径（不推荐，产物不依赖它）。
* ``sample_key`` 唯一；同一 key 可同时有 intensity 与 label 两个产物。
* ``kind`` 仅 ``intensity`` / ``label``。四维文件逐帧产出，帧号按原序从 0 开始。
* 清单坏项单独记入诊断，不写成产物；其余项照常处理。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

ITEM_KINDS = ("intensity", "label")


@dataclass
class ManifestItem:
    index: int
    path: str
    sample_key: str
    kind: str
    abs_path: Path


@dataclass
class Manifest:
    path: Path
    batch_id: str
    items: list = field(default_factory=list)
    problems: list = field(default_factory=list)   # [{"item": i, "reason": ...}]

    @property
    def ok(self) -> bool:
        return not self.problems


_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]+$")


def slugify(text: str) -> str:
    """把样本键/文件名变成安全 slug；仅用于文件名，不改写样本键本身。"""
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("._-")
    return s or "item"


def parse_manifest(path) -> Manifest:
    """解析并逐项校验清单；结构级错误抛 ValueError，条目级错误进 problems。"""
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"清单不存在: {p}")
    except json.JSONDecodeError as e:
        raise ValueError(f"清单不是合法 JSON: {p}: {e}")
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        raise ValueError("清单须为含 items 数组的 JSON 对象")
    batch_id = str(raw.get("batch_id") or p.parent.name or "batch")
    mf = Manifest(path=p, batch_id=batch_id)

    seen_keys = {}
    for i, item in enumerate(raw["items"]):
        if not isinstance(item, dict):
            mf.problems.append({"item": i, "reason": f"条目须为 JSON 对象: {item!r}"})
            continue
        rel = item.get("path")
        key = item.get("sample_key")
        kind = item.get("kind")
        reasons = []
        if not isinstance(rel, str) or not rel.strip():
            reasons.append("缺少 path")
        if not isinstance(key, str) or not key.strip():
            reasons.append("缺少 sample_key")
        elif not _SAFE_KEY.match(key):
            reasons.append(f"sample_key 含不安全字符（仅允许字母数字 . _ -）: {key!r}")
        if kind not in ITEM_KINDS:
            reasons.append(f"kind 须为 {'/'.join(ITEM_KINDS)}，实际 {kind!r}")
        if reasons:
            mf.problems.append({"item": i, "path": rel, "sample_key": key,
                                "reason": "；".join(reasons)})
            continue
        # 同一样本键可有多帧/多类型；同一 (key, kind) 由同一条目给出才冲突。
        # 清单不声明帧数，四维帧数在读取后才知道，因此重复检测在作业阶段完成。
        seen_keys.setdefault(key, []).append(i)
        abs_path = Path(rel)
        if not abs_path.is_absolute():
            abs_path = (p.parent / abs_path).resolve()
        mf.items.append(ManifestItem(index=i, path=rel, sample_key=key, kind=kind,
                                     abs_path=abs_path))
    return mf


# ------------------------------------------------------------------ 产物命名
def product_stem(sample_key: str, kind: str, frame: int) -> str:
    return f"{slugify(sample_key)}__{kind}__f{int(frame):03d}"


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ 读回
def load_product(meta_path):
    """读回一件产物：返回 (array, mask, meta)。不需要原始 NIfTI 文件。

    meta 内的数组/掩码以相对 meta 文件的文件名记录，因此整个产物目录可整体搬迁。
    同时核对文件 sha256（元数据里记录的是内容摘要）。
    """
    import numpy as np

    meta_path = Path(meta_path)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    base = meta_path.parent
    arr = np.load(base / meta["array_file"], allow_pickle=False)
    mask = np.load(base / meta["mask_file"], allow_pickle=False)
    for entry, name in ((meta["array_file"], meta["array_sha256"]),
                        (meta["mask_file"], meta["mask_sha256"])):
        if sha256_file(base / entry) != name:
            raise ValueError(f"产物校验和不符: {entry}")
    if arr.shape != tuple(meta["target"]["shape"]):
        raise ValueError(f"数组 shape {arr.shape} 与元数据目标 shape 不符")
    if mask.shape != arr.shape or mask.dtype.kind != "u":
        raise ValueError("掩码形状/类型与数组不符")
    if meta["kind"] == "intensity" and mask.sum():
        if np.isnan(arr[mask == 0]).sum() != int((mask == 0).sum()):
            raise ValueError("无效强度位置应为 NaN")
    return arr, mask, meta


def load_index(index_path):
    """读取批次索引（所有成功产物及其诊断），返回 dict。"""
    return json.loads(Path(index_path).read_text(encoding="utf-8"),
                      parse_constant=_reject_json_constant)


def _reject_json_constant(name):
    """严格 JSON：产物元数据不允许 NaN/Infinity 字面量（非有限值应为字符串）。"""
    raise ValueError(f"元数据含非严格 JSON 常量: {name}")


def iter_sample_products(out_dir, sample_key, kind):
    """按批次索引取出某样本某类型的全部帧产物 [(frame, array, mask, meta)]。

    只需要产物目录；不打开原始 NIfTI，也不依赖清单文件。
    """
    out_dir = Path(out_dir)
    index = load_index(out_dir / "index.json")
    for rec in index["samples"]:
        if rec["sample_key"] != sample_key or rec["kind"] != kind:
            continue
        for f in sorted(rec["frames"], key=lambda x: x["frame_index"]):
            arr, mask, meta = load_product(out_dir / index["products_dir"] / f["meta_file"])
            yield f["frame_index"], arr, mask, meta
