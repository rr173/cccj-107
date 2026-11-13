"""离线 LC-MS 目标离子提取（mzML -> 目标离子色谱 -> 时间窗积分）。

只依赖标准库与 numpy，不借助厂商软件、不做化合物自动鉴定。

- :mod:`org_er.lcms.cv`        受控词汇与 referenceableParamGroup 解析
- :mod:`org_er.lcms.mzml`      mzML 流式解码（base64/zlib，32/64 位 IEEE 小端）
- :mod:`org_er.lcms.targets`   目标表与清单解析
- :mod:`org_er.lcms.extract`   扫描级闭 m/z 窗目标信号
- :mod:`org_er.lcms.integral`  分段线性积分（长间隙不连线、不外推、背景穿点）
- :mod:`org_er.lcms.job`       批作业、明细落盘、诊断隔离与读回
"""
from __future__ import annotations

from .errors import IntegrationError, LcmsError, MzMLError, TargetError
from .integral import StreamingIntegrator
from .job import run_batch

__all__ = [
    "IntegrationError",
    "LcmsError",
    "MzMLError",
    "TargetError",
    "StreamingIntegrator",
    "run_batch",
]
