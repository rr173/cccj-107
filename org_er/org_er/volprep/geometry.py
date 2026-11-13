"""NIfTI 几何与强度的读取、校验和单位换算。

约定
----
* **体素整数下标指体素中心**（NIfTI 语义），世界坐标单位为毫米（mm）。
* 4×4 affine 的最后一行必须是 ``[0, 0, 0, 1]``（非仿射末行直接拒绝）；
  有效仿射还要求元素有限、3×3 部分可逆。
* 选择顺序：``sform_code > 0`` 且 sform 有效 -> 用 sform；
  否则 ``qform_code > 0`` 且 qform 有效 -> 用 qform；两者都不行 -> 拒绝。
  **绝不退回到单位矩阵**。
* 空间单位：``mm`` 原样；``meter`` ×1000；``micron`` ×0.001；其它（含 unknown）
  拒绝。时间单位与空间无关，忽略。
* 强度缩放：采用 ``scl_slope / scl_inter``（``slope`` 为 0 或非有限表示“无缩放”，
  按恒等处理，与 NIfTI 规范及 nibabel 一致）。原始存储值与缩放系数一起保留，
  必要时可无歧义复现。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

# 空间单位 -> 毫米换算系数
SPATIAL_UNIT_TO_MM = {"mm": 1.0, "meter": 1000.0, "micron": 0.001}
KNOWN_SPATIAL_UNITS = frozenset(SPATIAL_UNIT_TO_MM) | {"unknown"}

# 轴方向码 -> 轴字母（NIfTI 规范，仅用于元数据描述）
_XYZ = ("x", "y", "z")


class GeometryError(ValueError):
    """几何不合法：拒绝该输入，绝不默认单位矩阵。"""


class UnitError(GeometryError):
    """未知/缺失空间单位。"""


def _as_affine(mat) -> np.ndarray:
    a = np.asarray(mat, dtype=np.float64)
    if a.shape != (4, 4):
        raise GeometryError(f"affine 形状须为 4×4，实际 {a.shape}")
    return a


def validate_affine(mat, *, record=""):
    """校验一个候选 affine；返回 (affine, None) 或 (None, 原因)。

    规则：形状 4×4、有限、末行为 [0,0,0,1]、3×3 部分可逆。
    """
    try:
        a = _as_affine(mat)
    except GeometryError as e:
        return None, str(e)
    tag = f"{record}: " if record else ""
    if not np.all(np.isfinite(a)):
        return None, f"{tag}affine 含非有限值"
    if not np.allclose(a[3, :], [0.0, 0.0, 0.0, 1.0], rtol=0.0, atol=1e-7):
        return None, f"{tag}affine 末行非 [0,0,0,1]: {a[3, :].tolist()}"
    try:
        det = float(np.linalg.det(a[:3, :3]))
    except np.linalg.LinAlgError:  # pragma: no cover - 防御
        return None, f"{tag}affine 3×3 部分不可逆"
    if not math.isfinite(det) or abs(det) < 1e-12:
        return None, f"{tag}affine 3×3 部分不可逆（det={det!r}）"
    return a, None


def _axcodes(affine_3x3: np.ndarray) -> tuple:
    """每个体素轴（源空间轴）的最接近世界轴方向，如 ('-x','+y','+z')。"""
    out = []
    for j in range(3):
        col = affine_3x3[:, j]
        k = int(np.argmax(np.abs(col)))
        if abs(col[k]) == 0.0:
            out.append("?")
        else:
            out.append(("+" if col[k] > 0 else "-") + _XYZ[k])
    return tuple(out)


def _permutation(affine_3x3: np.ndarray) -> tuple:
    """(perm, flips)：每个体素轴最接近的世界轴及其正负号（仅作描述）。

    允许旋转/斜切/轴交换；对真正轴交换它是精确的，对一般旋转/斜切只是最接近的标签，
    重采样本身始终按完整 affine 计算，不依赖这两个描述字段。
    """
    perm = []
    flips = []
    for j in range(3):
        col = affine_3x3[:, j]
        k = int(np.argmax(np.abs(col)))
        perm.append(k)
        flips.append(bool(col[k] < 0))
    return tuple(perm), tuple(flips)


def parse_spatial_unit(units) -> float:
    """NIfTI ``xyzt_units`` 的 xyz 部分 -> 毫米系数；未知/缺失拒绝。"""
    u = (units[0] if isinstance(units, (tuple, list)) else str(units)).strip().lower()
    if u not in KNOWN_SPATIAL_UNITS:
        raise UnitError(f"未知空间单位 {u!r}（支持 mm / meter / micron；未知单位拒绝）")
    if u == "unknown":
        raise UnitError("空间单位为 unknown（未知单位），无法确定毫米尺度，拒绝")
    return SPATIAL_UNIT_TO_MM[u]


@dataclass(frozen=True)
class SourceGeometry:
    """一个输入文件实际采用的几何（全部已归一化为毫米、体素中心语义）。"""

    geom_source: str            # 'sform' | 'qform'
    sform_code: int
    qform_code: int
    affine_original: np.ndarray          # 文件中的 4×4（未做单位换算）
    affine: np.ndarray                   # 归一化到毫米的 4×4（实际用于重采样）
    source_spatial_unit: str             # 文件声明的空间单位
    unit_scale_to_mm: float              # 归一化乘数
    axcodes: tuple                       # 体素轴 -> 世界轴方向描述
    axis_permutation: tuple
    axis_flips: tuple
    rejected_candidates: tuple = ()      # ((which, reason), ...) 供诊断

    def meta(self) -> dict:
        return {
            "geometry_source": self.geom_source,
            "sform_code": int(self.sform_code),
            "qform_code": int(self.qform_code),
            "spatial_unit": self.source_spatial_unit,
            "unit_scale_to_mm": self.unit_scale_to_mm,
            "affine_original": [[float(x) for x in row] for row in self.affine_original],
            "normalized_affine_mm": [[float(x) for x in row] for row in self.affine],
            "affine": [[float(x) for x in row] for row in self.affine],
            "affine_note": ("affine 为实际用于重采样的毫米归一化矩阵；"
                            "affine_original 为文件头原值"),
            "index_convention": "integer_voxel_index_is_center",
            "axcodes": list(self.axcodes),
            "axis_permutation": list(self.axis_permutation),
            "axis_flips": [bool(f) for f in self.axis_flips],
            "rejected_candidates": [list(r) for r in self.rejected_candidates],
        }


def choose_source_geometry(header, shape) -> SourceGeometry:
    """按 sform->qform 顺序选出有效几何并归一化到毫米；否则抛 GeometryError。"""
    sform_code = int(header["sform_code"])
    qform_code = int(header["qform_code"])
    rejected = []

    selected = None
    if sform_code > 0:
        mat = header.get_sform(coded=False)
        ok, why = validate_affine(mat, record="sform")
        if ok is None:
            rejected.append(("sform", why))
        else:
            selected = ("sform", ok)
    else:
        rejected.append(("sform", "sform_code=0，未提供有效 sform"))

    if selected is None and qform_code > 0:
        try:
            mat = header.get_qform(coded=False)
        except Exception as e:  # pragma: no cover - nibabel 对非法 qfac 抛错
            rejected.append(("qform", f"qform 无法重建: {e}"))
            mat = None
        if mat is not None:
            ok, why = validate_affine(mat, record="qform")
            if ok is None:
                rejected.append(("qform", why))
            else:
                selected = ("qform", ok)
    elif selected is None:
        rejected.append(("qform", "qform_code=0，未提供有效 qform"))

    if selected is None:
        reasons = "；".join(f"{w}: {r}" for w, r in rejected)
        raise GeometryError(
            f"没有可用的有效几何（sform_code={sform_code}, qform_code={qform_code}）：{reasons}"
            "；拒绝退回到单位矩阵")

    which, affine_orig = selected
    unit_scale = parse_spatial_unit(header.get_xyzt_units()[0])
    # 单位换算只作用于世界坐标（前 3 行的平移列），线性部分列乘以尺度
    affine_mm = affine_orig.copy()
    affine_mm[:3, :3] *= unit_scale
    affine_mm[:3, 3] *= unit_scale
    affine_mm[3, :] = [0.0, 0.0, 0.0, 1.0]

    perm, flips = _permutation(affine_mm[:3, :3])
    return SourceGeometry(
        geom_source=which,
        sform_code=sform_code,
        qform_code=qform_code,
        affine_original=affine_orig,
        affine=affine_mm,
        source_spatial_unit=str(header.get_xyzt_units()[0]),
        unit_scale_to_mm=unit_scale,
        axcodes=_axcodes(affine_mm[:3, :3]),
        axis_permutation=perm,
        axis_flips=flips,
        rejected_candidates=tuple(rejected),
    )


@dataclass(frozen=True)
class TargetGeometry:
    """目标分析网格（JSON 描述），一律毫米、体素中心语义。"""

    shape: tuple
    affine: np.ndarray
    spatial_unit: str = "mm"

    def meta(self) -> dict:
        return {
            "shape": [int(s) for s in self.shape],
            "affine": [[float(x) for x in row] for row in self.affine],
            "spatial_unit": "mm",
            "index_convention": "integer_voxel_index_is_center",
            "world_unit": "mm",
        }

    def inverse(self) -> np.ndarray:
        return np.linalg.inv(self.affine)


def load_target(spec) -> TargetGeometry:
    """从目标网格 JSON 描述构造 TargetGeometry；不合法即拒绝。"""
    if not isinstance(spec, dict):
        raise GeometryError("目标网格描述须为 JSON 对象")
    if "shape" not in spec or "affine" not in spec:
        raise GeometryError("目标网格描述须同时给出 shape 与 affine")
    shape = spec["shape"]
    if not isinstance(shape, (list, tuple)) or len(shape) != 3:
        raise GeometryError(f"目标 shape 须为 3 个整数，实际 {shape!r}")
    for s in shape:
        if isinstance(s, bool) or not isinstance(s, int) or s <= 0:
            raise GeometryError(f"目标 shape 元素须为正整数，实际 {shape!r}")
    unit = str(spec.get("spatial_unit", spec.get("units", "mm"))).strip().lower()
    if unit not in ("mm", "millimeter", "millimetre"):
        raise GeometryError(f"目标网格空间单位须为 mm（描述本身按毫米），实际 {unit!r}")
    aff, why = validate_affine(spec["affine"], record="target affine")
    if aff is None:
        raise GeometryError(why)
    return TargetGeometry(shape=tuple(int(s) for s in shape), affine=aff, spatial_unit="mm")


def target_from_affine(affine, shape) -> TargetGeometry:
    """程序内直接构造目标网格（测试/演示用，仍走同一校验）。"""
    return load_target({"shape": list(shape), "affine": np.asarray(affine).tolist()})


@dataclass(frozen=True)
class IntensityScaling:
    slope: float
    inter: float
    on_disk_slope: float
    on_disk_inter: float
    effective: bool               # 文件头是否声明了非恒等缩放

    def apply(self, raw: np.ndarray) -> np.ndarray:
        if not self.effective:
            return raw.astype(np.float64, copy=False)
        return raw.astype(np.float64) * self.slope + self.inter

    def meta(self) -> dict:
        return {
            "effective_slope": float(self.slope),
            "effective_inter": float(self.inter),
            "header_after_load_slope": _json_float(self.on_disk_slope),
            "header_after_load_inter": _json_float(self.on_disk_inter),
            "scaling_applied": bool(self.effective),
            "scaling_note": (
                "identity (文件头无可用的 scl_slope，或已归一化为恒等)"
                if not self.effective else
                "raw_stored_value * effective_slope + effective_inter"),
            "scaling_source": ("nifti_scl_slope/scl_inter，经 nibabel 延迟缩放代理读取；"
                               "header_after_load_* 是加载后头字段（无缩放时归一化为 NaN）"),
        }


def _json_float(x):
    """JSON 元数据里的浮点：非有限值写成字符串（严格 JSON 不允许 NaN/Infinity）。"""
    x = float(x)
    if math.isfinite(x):
        return x
    return "NaN" if math.isnan(x) else ("Infinity" if x > 0 else "-Infinity")


def declared_scaling(header) -> tuple:
    """头字段里的 (scl_slope, scl_inter) 原样（加载后可能已被归一化为 NaN）。"""
    return float(header["scl_slope"]), float(header["scl_inter"])


def effective_scaling(dataobj) -> tuple:
    """数据代理实际使用的 (slope, inter)。

    nibabel 把“无缩放”（slope 缺失/0/非有限）归一化为 1.0/0.0，这里读取的是
    真实生效的系数，绝不会把缺失的缩放误当成有效缩放。
    """
    slope = float(getattr(dataobj, "slope", 1.0))
    inter = float(getattr(dataobj, "inter", 0.0))
    if not math.isfinite(slope) or not math.isfinite(inter):
        raise GeometryError(f"缩放系数非有限: slope={slope!r}, inter={inter!r}")
    return slope, inter


def intensity_scaling(dataobj, header) -> IntensityScaling:
    """强度文件的缩放：记录生效系数，供重采样使用。"""
    slope, inter = effective_scaling(dataobj)
    ds, di = declared_scaling(header)
    effective = not (slope == 1.0 and inter == 0.0)
    return IntensityScaling(slope, inter, ds, di, effective)


def label_scaling(dataobj, header) -> IntensityScaling:
    """标签文件的缩放必须恒等，否则拒绝。"""
    slope, inter = effective_scaling(dataobj)
    if slope != 1.0 or inter != 0.0:
        raise GeometryError(
            f"标签缩放必须为恒等，实际生效 scl_slope={slope!r}, scl_inter={inter!r}")
    ds, di = declared_scaling(header)
    return IntensityScaling(1.0, 0.0, ds, di, False)


def ensure_integer_labels(raw: np.ndarray, dtype) -> None:
    """原始标签存储必须为整数类型（不含布尔），值域非负由调用方检查。"""
    dt = np.dtype(dtype)
    if dt.kind not in ("i", "u"):
        raise GeometryError(f"标签文件存储类型须为整数，实际 {dt}")
    if dt.kind == "i" and raw.size and int(raw.min()) < 0:
        raise GeometryError(f"标签值含负数（最小 {int(raw.min())}），要求非负整数")
