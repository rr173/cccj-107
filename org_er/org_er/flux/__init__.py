"""稳态通量分析（FBA + FVA）子包。

只依赖 SciPy HiGHS 做线性规划，不实现 LP 求解器，不引入热力学假设，
也不自动消除内部循环（type III pathways）。模块划分：

- model     网络/条件 JSON 的读取与严格校验（绝不静默改写原网络）
- solver    FBA、逐反应 FVA 端点求解与见证残差诊断
- job       多条件作业编排、原子发布与读回
"""
