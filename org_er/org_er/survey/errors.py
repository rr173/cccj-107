"""survey 子包异常类型：按环节区分，便于命令行给出非零退出码与具体诊断。"""
from __future__ import annotations


class SurveyError(Exception):
    """survey 作业通用错误。"""


class SurveyDataError(SurveyError):
    """数据 CSV 非法（身份重复、设计列缺失、非法权重、非法 response/outcome）。"""


class SurveyDesignError(SurveyError):
    """设计 JSON 非法（层/PSU 结构、校准列、已知边际、必入层声明）。"""


class ResponseAdjustmentError(SurveyError):
    """应答调整失败（某 adjustment_cell 无应答者等）。"""


class CalibrationError(SurveyError):
    """边际校准失败（零分母、无样本支持的控制格、边际冲突、迭代超限）。"""


class EstimationError(SurveyError):
    """点估计或 jackknife 复制失败（复制后调整格消失、目标人群分母为 0 等）。"""
