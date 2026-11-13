"""数据 CSV 的解析与逐行校验：非法输入指出原行，绝不悄悄丢弃。

必填列：``unit_id, stratum, psu, base_weight, response, adjustment_cell, domain,
outcome``，外加设计 JSON 里 ``calibration.columns`` 列出的每个校准列。

校验规则：

- ``unit_id`` 非空且唯一（重复时指出两行行号）；设计列（``stratum/psu/
  adjustment_cell/domain``）不得为空；校准列不得为空；
- ``base_weight`` 为正的有限数；
- ``response`` 只接受 0/1；``response == 0`` 时 ``outcome`` 不读入（可空）；
  ``response == 1`` 时 ``outcome`` 必须是有限数；
- 空行跳过（CSV 末尾常见），其它坏行**整批失败**并在异常里列出行号与原文。

行号是 CSV 的物理行号（表头为第 1 行），方便研究人员直接定位文件。
"""
from __future__ import annotations

import csv
import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SurveyDataError

REQUIRED_COLUMNS = (
    "unit_id", "stratum", "psu", "base_weight", "response",
    "adjustment_cell", "domain", "outcome",
)
DESIGN_TEXT_COLUMNS = ("stratum", "psu", "adjustment_cell", "domain")


@dataclass(frozen=True)
class Person:
    line_no: int
    unit_id: str
    stratum: str
    psu: str
    base_weight: float
    response: bool
    adjustment_cell: str
    domain: str
    outcome: float | None
    calibration: tuple[str, ...]       # 与设计列顺序一致

    @property
    def row_index(self) -> int:
        """数据行序号（0 起，表头之后），用于稳定的输出排序。"""
        return self.line_no - 2


@dataclass
class SurveyData:
    path: str
    sha256: str
    columns: tuple[str, ...]
    calibration_columns: tuple[str, ...]
    people: tuple[Person, ...]
    n_rows_read: int = 0
    excluded_blank_lines: tuple[int, ...] = ()

    @property
    def n_people(self) -> int:
        return len(self.people)

    def people_in_order(self) -> list[Person]:
        return list(self.people)


@dataclass
class _Problems:
    items: list[str] = field(default_factory=list)

    def add(self, line_no: int, reason: str, raw: str = "") -> None:
        location = f"第 {line_no} 行"
        text = f"{location}: {reason}"
        if raw:
            text += f"（原文: {raw[:160]!r}）"
        self.items.append(text)

    def raise_if_any(self, path: Path) -> None:
        if self.items:
            head = f"数据文件 {path} 非法，共 {len(self.items)} 处问题（未丢弃任何行，整批失败）："
            raise SurveyDataError(head + "\n  - " + "\n  - ".join(self.items))


def file_sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_weight(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0.0:
        return None
    return value


def _parse_response(text: str) -> bool | None:
    t = text.strip().lower()
    if t in ("1", "true", "yes"):
        return True
    if t in ("0", "false", "no"):
        return False
    return None


def _parse_outcome(text: str) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def load_data(path, *, calibration_columns: tuple[str, ...]) -> SurveyData:
    """读入 CSV 并逐行校验；任一坏行 -> SurveyDataError（列出行号，不产出部分数据）。"""
    p = Path(path)
    if not p.is_file():
        raise SurveyDataError(f"数据文件不存在: {p}")
    digest = file_sha256(p)

    problems = _Problems()
    people: list[Person] = []
    blank_lines: list[int] = []
    seen_units: dict[str, int] = {}

    try:
        fh = open(p, newline="", encoding="utf-8-sig")
    except OSError as e:
        raise SurveyDataError(f"无法读取数据文件 {p}: {e}") from e

    try:
        with fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise SurveyDataError(f"数据文件 {p} 为空（连表头都没有）")
            header = [h.strip() for h in header]
            missing = [c for c in REQUIRED_COLUMNS if c not in header]
            if missing:
                raise SurveyDataError(
                    f"数据文件 {p} 缺少必填列 {missing}；实际表头 {header}")
            dup_header = sorted({c for c in header if header.count(c) > 1})
            if dup_header:
                raise SurveyDataError(f"数据文件 {p} 表头列名重复: {dup_header}")
            missing_cal = [c for c in calibration_columns if c not in header]
            if missing_cal:
                raise SurveyDataError(
                    f"数据文件 {p} 缺少设计 JSON 要求的校准列 {missing_cal}；"
                    f"实际表头 {header}")
            index = {name: i for i, name in enumerate(header)}

            for line_no, row in enumerate(reader, start=2):
                if not row or all(not cell.strip() for cell in row):
                    blank_lines.append(line_no)
                    continue
                problems_before = len(problems.items)
                raw = ",".join(row)

                def cell(name: str, _row=row) -> str:
                    i = index[name]
                    return _row[i].strip() if i < len(_row) else ""

                unit_id = cell("unit_id")
                if not unit_id:
                    problems.add(line_no, "unit_id 为空", raw)
                elif unit_id in seen_units:
                    problems.add(
                        line_no,
                        f"unit_id {unit_id!r} 身份重复（首次出现在第 "
                        f"{seen_units[unit_id]} 行）",
                        raw)

                design_values: dict[str, str] = {}
                for name in DESIGN_TEXT_COLUMNS:
                    v = cell(name)
                    if not v:
                        problems.add(line_no, f"设计列 {name} 为空", raw)
                    design_values[name] = v

                weight = _parse_weight(cell("base_weight"))
                if weight is None:
                    problems.add(
                        line_no,
                        f"base_weight 必须是正的有限数，实际 {cell('base_weight')!r}",
                        raw)

                response = _parse_response(cell("response"))
                if response is None:
                    problems.add(
                        line_no,
                        f"response 必须是 0 或 1，实际 {cell('response')!r}", raw)

                outcome_text = cell("outcome")
                outcome: float | None = None
                if response is True:
                    outcome = _parse_outcome(outcome_text)
                    if outcome is None:
                        problems.add(
                            line_no,
                            f"response=1 时 outcome 必须是有限数，实际 {outcome_text!r}",
                            raw)
                elif response is False:
                    outcome = None  # 未应答者不提供 outcome；即使填了也不参与分析

                cal_values: list[str] = []
                for name in calibration_columns:
                    v = cell(name)
                    if not v:
                        problems.add(line_no, f"校准列 {name} 为空", raw)
                    cal_values.append(v)

                if len(problems.items) == problems_before:
                    assert weight is not None and response is not None
                    seen_units[unit_id] = line_no
                    people.append(Person(
                        line_no=line_no,
                        unit_id=unit_id,
                        stratum=design_values["stratum"],
                        psu=design_values["psu"],
                        base_weight=weight,
                        response=response,
                        adjustment_cell=design_values["adjustment_cell"],
                        domain=design_values["domain"],
                        outcome=outcome,
                        calibration=tuple(cal_values),
                    ))
    except csv.Error as e:
        raise SurveyDataError(f"数据文件 {p} 的 CSV 结构不合法: {e}") from e

    problems.raise_if_any(p)
    if not people:
        raise SurveyDataError(f"数据文件 {p} 没有任何数据行")
    return SurveyData(
        path=str(p),
        sha256=digest,
        columns=tuple(header),
        calibration_columns=tuple(calibration_columns),
        people=tuple(people),
        n_rows_read=len(people) + len(blank_lines),
        excluded_blank_lines=tuple(blank_lines),
    )
