"""通量分析的错误类型。

输入类错误（FluxInputError）与数值类错误（FluxSolverError）分开，
命令行用不同退出码区分；条件级别的不可行/无界不是异常，而是写进结果状态。
"""


class FluxError(Exception):
    """通量分析作业的基础异常。"""


class FluxInputError(FluxError):
    """网络或条件文件非法（结构、引用、重复、参数越界等）。"""


class FluxSolverError(FluxError):
    """求解器数值失败，或声称有效的见证超出声明容差。"""
