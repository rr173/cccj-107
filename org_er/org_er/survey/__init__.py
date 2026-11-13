"""复杂抽样问卷估计（survey）子包。

链路：抽样设计校验 -> 应答调整（按 adjustment_cell 求基础权重和的比值）->
迭代比例拟合（IPF）把边际校准到已知总体人数 -> 按 domain 汇总点估计 ->
分层删一 PSU jackknife（每次复制都重新做应答调整与校准）。

输入只读；设计 JSON 与 CSV 的位置由命令行给出，产物写独立输出目录。
"""
from __future__ import annotations

from .errors import (CalibrationError, EstimationError, ResponseAdjustmentError,
                     SurveyDataError, SurveyDesignError, SurveyError)
from .job import read_diagnostics, read_estimates, read_person_weights, run_survey

__all__ = [
    "CalibrationError",
    "EstimationError",
    "ResponseAdjustmentError",
    "SurveyDataError",
    "SurveyDesignError",
    "SurveyError",
    "read_diagnostics",
    "read_estimates",
    "read_person_weights",
    "run_survey",
]

__version__ = "0.1.0"
