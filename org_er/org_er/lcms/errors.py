"""LC-MS 提取作业的异常类型。

所有“输入失败”（mzML 解码错误、参数组无法解析、互斥声明、坏压缩、残缺 XML、
目标表/清单错误等）都派生于 :class:`LcmsError`，批处理捕获它即可把该样本
隔离到诊断而不拖垮整批。
"""
from __future__ import annotations


class LcmsError(Exception):
    """LC-MS 作业通用错误（输入失败，绝不冒充零信号）。"""


class MzMLError(LcmsError):
    """mzML 结构 / 受控词汇 / 二进制解码失败。"""


class TargetError(LcmsError):
    """目标表或清单内容非法。"""


class IntegrationError(LcmsError):
    """时间序列校验或积分状态失败（如重复扫描时间）。"""
