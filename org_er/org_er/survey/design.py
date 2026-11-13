"""设计 JSON 的解析与结构校验。

设计文件描述抽样结构与校准目标，格式::

    {
      "must_include_strata": ["CERT"],
      "calibration": {
        "columns": [
          {"name": "sex", "categories": {"M": 950, "F": 650}},
          {"name": "age_band", "categories": {"young": 850, "mid": 450, "old": 300}}
        ]
      },
      "max_iterations": 100,
      "tolerance": 1e-8
    }

约定：

- ``must_include_strata`` 列出必入层（含单 PSU 必入层）。必入层不产生删除复制；
  未列出的单 PSU 层是设计非法（不能把单 PSU 层的方差默认为 0）。
- 其余层至少 2 个 PSU（与数据交叉校验，见 :mod:`data`）。
- 每个校准列按配置顺序参与 IPF；类别及已知总体人数显式给出，
  同一列各类别人数之和以及不同列的总人数必须一致。
- ``max_iterations`` 为正整数；``tolerance`` 是边际相对误差的收敛阈值。
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .errors import SurveyDesignError


@dataclass(frozen=True)
class CalibrationColumn:
    name: str
    categories: tuple[str, ...]
    targets: tuple[float, ...]

    @property
    def target_map(self) -> dict[str, float]:
        return dict(zip(self.categories, self.targets))

    @property
    def total(self) -> float:
        return math.fsum(self.targets)


@dataclass(frozen=True)
class Design:
    path: str
    must_include_strata: tuple[str, ...]
    calibration_columns: tuple[CalibrationColumn, ...]
    max_iterations: int
    tolerance: float
    population_total: float
    sha256: str
    raw: dict

    @property
    def calibration_column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.calibration_columns)


def _require_object(raw, path: Path) -> dict:
    if not isinstance(raw, dict):
        raise SurveyDesignError(f"设计文件 {path} 必须是 JSON 对象，实际是 {type(raw).__name__}")
    return raw


def _parse_count(value, context: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SurveyDesignError(f"{context} 的已知人数必须是数值，实际 {value!r}")
    x = float(value)
    if not math.isfinite(x):
        raise SurveyDesignError(f"{context} 的已知人数必须有限，实际 {value!r}")
    if x < 0.0:
        raise SurveyDesignError(f"{context} 的已知人数必须非负，实际 {value!r}")
    return x


def _parse_categories(entry: dict, column_index: int) -> CalibrationColumn:
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SurveyDesignError(f"calibration.columns 第 {column_index} 项缺少合法 name")
    name = name.strip()
    categories = entry.get("categories")
    if isinstance(categories, list):
        parsed: dict[str, float] = {}
        for cat_index, item in enumerate(categories):
            if not isinstance(item, dict):
                raise SurveyDesignError(
                    f"校准列 {name!r} 第 {cat_index} 个类别必须是对象 "
                    f"{{'value':..., 'count':...}}")
            value = item.get("value")
            count = item.get("count")
            if not isinstance(value, str) or not value.strip():
                raise SurveyDesignError(
                    f"校准列 {name!r} 第 {cat_index} 个类别缺少合法 value")
            value = value.strip()
            if value in parsed:
                raise SurveyDesignError(f"校准列 {name!r} 类别重复: {value!r}")
            parsed[value] = _parse_count(count, f"校准列 {name!r} 类别 {value!r}")
    elif isinstance(categories, dict):
        parsed = {}
        for value, count in categories.items():
            if not isinstance(value, str) or not value.strip():
                raise SurveyDesignError(f"校准列 {name!r} 存在空类别名")
            value = value.strip()
            if value in parsed:
                raise SurveyDesignError(f"校准列 {name!r} 类别重复: {value!r}")
            parsed[value] = _parse_count(count, f"校准列 {name!r} 类别 {value!r}")
    else:
        raise SurveyDesignError(
            f"校准列 {name!r} 的 categories 必须是对象或类别数组，实际 {type(categories).__name__}")

    if not parsed:
        raise SurveyDesignError(f"校准列 {name!r} 没有任何类别")
    return CalibrationColumn(name, tuple(parsed.keys()), tuple(parsed.values()))


def load_design(path, *, sha256: str = "") -> Design:
    p = Path(path)
    if not p.is_file():
        raise SurveyDesignError(f"设计文件不存在: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SurveyDesignError(f"设计 JSON 不合法: {e}") from e
    raw = _require_object(raw, p)

    must_raw = raw.get("must_include_strata", [])
    if must_raw is None:
        must_raw = []
    if not isinstance(must_raw, list):
        raise SurveyDesignError("must_include_strata 必须是字符串数组")
    must: list[str] = []
    for item in must_raw:
        if not isinstance(item, str) or not item.strip():
            raise SurveyDesignError(f"must_include_strata 含非法条目: {item!r}")
        item = item.strip()
        if item in must:
            raise SurveyDesignError(f"must_include_strata 重复列层: {item!r}")
        must.append(item)

    calibration = raw.get("calibration")
    if not isinstance(calibration, dict):
        raise SurveyDesignError("设计文件缺少 calibration 对象")
    entries = calibration.get("columns")
    if not isinstance(entries, list) or not entries:
        raise SurveyDesignError("calibration.columns 必须是非空数组")
    columns: list[CalibrationColumn] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SurveyDesignError(f"calibration.columns 第 {i} 项不是对象")
        col = _parse_categories(entry, i)
        if col.name in seen:
            raise SurveyDesignError(f"校准列重复: {col.name!r}")
        seen.add(col.name)
        columns.append(col)

    totals = [c.total for c in columns]
    base_total = totals[0]
    if base_total <= 0.0:
        raise SurveyDesignError(f"校准边际总人数必须为正，实际 {base_total}")
    for col, total in zip(columns, totals):
        if not math.isclose(total, base_total, rel_tol=1e-9, abs_tol=0.0):
            raise SurveyDesignError(
                "所有校准边际的总人数必须一致："
                f"列 {columns[0].name!r} 合计 {base_total:g}，"
                f"列 {col.name!r} 合计 {total:g}")

    max_iterations = raw.get("max_iterations", 100)
    if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) \
            or max_iterations <= 0:
        raise SurveyDesignError(
            f"max_iterations 必须是正整数，实际 {max_iterations!r}")
    tolerance = raw.get("tolerance", 1e-8)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        raise SurveyDesignError(f"tolerance 必须是正有限数，实际 {tolerance!r}")
    tolerance = float(tolerance)
    if not math.isfinite(tolerance) or not tolerance > 0.0:
        raise SurveyDesignError(f"tolerance 必须是正有限数，实际 {tolerance!r}")

    return Design(
        path=str(p),
        must_include_strata=tuple(must),
        calibration_columns=tuple(columns),
        max_iterations=int(max_iterations),
        tolerance=tolerance,
        population_total=base_total,
        sha256=sha256,
        raw=raw,
    )
