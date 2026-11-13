"""目标表与文件清单的解析和严格校验。

目标表每项含：唯一 ``target_id``、``mz``（正）、``ppm``（正）、极性
（positive/negative）、时间窗起止秒数（窗长为正）、非负常数 ``background``。
所有目标值必须有限。清单给样本键与 mzML 路径，路径相对**清单所在目录**解析。
目标表/清单支持 JSON 与 CSV/TSV。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from .errors import TargetError

POLARITY_POSITIVE = "positive"
POLARITY_NEGATIVE = "negative"
_POLARITY_ALIASES = {
    "positive": POLARITY_POSITIVE, "pos": POLARITY_POSITIVE, "+": POLARITY_POSITIVE,
    "p": POLARITY_POSITIVE,
    "negative": POLARITY_NEGATIVE, "neg": POLARITY_NEGATIVE, "-": POLARITY_NEGATIVE,
    "n": POLARITY_NEGATIVE,
}
_TARGET_FIELDS = ("target_id", "mz", "ppm", "polarity",
                  "window_start_seconds", "window_end_seconds", "background")


@dataclass(frozen=True)
class Target:
    target_id: str
    mz: float
    ppm: float
    polarity: str
    window_start: float
    window_end: float
    background: float

    @property
    def mz_low(self) -> float:
        return self.mz * (1.0 - self.ppm / 1_000_000.0)

    @property
    def mz_high(self) -> float:
        return self.mz * (1.0 + self.ppm / 1_000_000.0)

    @property
    def window_length(self) -> float:
        return self.window_end - self.window_start


@dataclass(frozen=True)
class SampleSpec:
    sample_key: str
    mzml_path: str          # 清单中的原样路径（相对或绝对）
    base_dir: Path

    def abs_path(self) -> Path:
        p = Path(self.mzml_path)
        return p if p.is_absolute() else (self.base_dir / p).resolve()


def _finite_number(value, field: str, target_id: str) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError) as e:
        raise TargetError(f"目标 {target_id!r} 的 {field} 不是数值: {value!r}") from e
    if not math_isfinite(x):
        raise TargetError(f"目标 {target_id!r} 的 {field} 必须有限，实际 {value!r}")
    return x


def math_isfinite(x: float) -> bool:
    import math
    return math.isfinite(x)


def _validate_target(raw: dict, index: int) -> Target:
    if not isinstance(raw, dict):
        raise TargetError(f"目标表第 {index} 项不是对象")
    tid = raw.get("target_id")
    if not isinstance(tid, str) or not tid.strip():
        raise TargetError(f"目标表第 {index} 项缺少合法 target_id")
    tid = tid.strip()

    mz = _finite_number(raw.get("mz"), "mz", tid)
    if mz <= 0.0:
        raise TargetError(f"目标 {tid!r} 的 mz 必须为正，实际 {mz}")
    ppm = _finite_number(raw.get("ppm"), "ppm", tid)
    if ppm <= 0.0:
        raise TargetError(f"目标 {tid!r} 的 ppm 必须为正，实际 {ppm}")

    pol_raw = raw.get("polarity")
    if not isinstance(pol_raw, str):
        raise TargetError(f"目标 {tid!r} 的 polarity 必须是字符串，实际 {pol_raw!r}")
    polarity = _POLARITY_ALIASES.get(pol_raw.strip().lower())
    if polarity is None:
        raise TargetError(
            f"目标 {tid!r} 的 polarity 非法: {pol_raw!r}（支持 positive/negative）")

    t0 = _finite_number(raw.get("window_start_seconds"),
                        "window_start_seconds", tid)
    t1 = _finite_number(raw.get("window_end_seconds"),
                        "window_end_seconds", tid)
    if not t1 > t0:
        raise TargetError(
            f"目标 {tid!r} 时间窗长度必须为正: start={t0}, end={t1}")
    if t0 < 0.0:
        raise TargetError(f"目标 {tid!r} 时间窗起点不可为负: {t0}")

    bg = raw.get("background", 0.0)
    background = _finite_number(bg, "background", tid)
    if background < 0.0:
        raise TargetError(f"目标 {tid!r} 的 background 必须非负，实际 {background}")

    return Target(tid, mz, ppm, polarity, t0, t1, background)


def _load_table_rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise TargetError(f"JSON 不合法: {path}: {e}") from e
        if isinstance(spec, dict):
            rows = spec.get("targets")
        else:
            rows = spec
        if not isinstance(rows, list) or not rows:
            raise TargetError(f"{path}: JSON 需要非空 targets 数组")
        return rows
    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh, delimiter=delimiter)
            if not reader.fieldnames or not set(_TARGET_FIELDS).issubset(reader.fieldnames):
                raise TargetError(
                    f"{path}: 表头必须包含 {list(_TARGET_FIELDS)}，实际 {reader.fieldnames}")
            rows = [dict(r) for r in reader]
        if not rows:
            raise TargetError(f"{path}: 表中没有任何数据行")
        return rows
    raise TargetError(f"不支持的目标表扩展名: {path}（.json/.csv/.tsv）")


def parse_targets(path) -> list[Target]:
    path = Path(path)
    if not path.is_file():
        raise TargetError(f"目标表不存在: {path}")
    rows = _load_table_rows(path)
    targets: list[Target] = []
    seen: set[str] = set()
    for i, raw in enumerate(rows):
        t = _validate_target(raw, i + 1)
        if t.target_id in seen:
            raise TargetError(f"target_id 重复: {t.target_id!r}")
        seen.add(t.target_id)
        targets.append(t)
    return targets


# ---------------------------------------------------------------- 清单
def _load_manifest_rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise TargetError(f"清单 JSON 不合法: {path}: {e}") from e
        if isinstance(spec, dict):
            rows = spec.get("samples")
            batch_id = spec.get("batch_id")
        else:
            rows, batch_id = spec, None
        if not isinstance(rows, list) or not rows:
            raise TargetError(f"{path}: JSON 需要非空 samples 数组")
        return rows
    if suffix in (".csv", ".tsv"):
        delimiter = "\t" if suffix == ".tsv" else ","
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh, delimiter=delimiter)
            required = {"sample_key", "mzml_path"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise TargetError(
                    f"{path}: 清单表头必须包含 {sorted(required)}，实际 {reader.fieldnames}")
            return [dict(r) for r in reader]
    raise TargetError(f"不支持的清单扩展名: {path}（.json/.csv/.tsv）")


def parse_manifest(path) -> tuple[list[SampleSpec], str | None]:
    path = Path(path)
    if not path.is_file():
        raise TargetError(f"清单不存在: {path}")
    rows = _load_manifest_rows(path)
    base_dir = path.parent.resolve()
    batch_id = None
    if path.suffix.lower() == ".json":
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(spec, dict):
                bid = spec.get("batch_id")
                batch_id = bid if isinstance(bid, str) and bid.strip() else None
        except json.JSONDecodeError:
            batch_id = None

    specs: list[SampleSpec] = []
    seen: set[str] = set()
    for i, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise TargetError(f"清单第 {i + 1} 项不是对象")
        key = raw.get("sample_key")
        mzml = raw.get("mzml_path") or raw.get("path")
        if not isinstance(key, str) or not key.strip():
            raise TargetError(f"清单第 {i + 1} 项缺少合法 sample_key")
        key = key.strip()
        if not isinstance(mzml, str) or not mzml.strip():
            raise TargetError(f"清单样本 {key!r} 缺少 mzml_path")
        if key in seen:
            raise TargetError(f"sample_key 在清单中重复: {key!r}")
        seen.add(key)
        specs.append(SampleSpec(key, mzml.strip(), base_dir))
    return specs, batch_id
