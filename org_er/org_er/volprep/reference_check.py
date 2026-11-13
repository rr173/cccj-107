"""体数据整备的独立小数组参考核对：不调用生产插值函数。

运行：python3 -m org_er.volprep.reference_check

对若干随机构造的（含旋转/斜切/轴交换）仿射，逐点标量参考与生产的多种块大小
结果逐点比较（强度掩码、NaN 位置、数值；标签类别与无效 0）。
"""
from __future__ import annotations

import numpy as np

from .reference import compare_chunked


def _cases(seed=20240501):
    rng = np.random.RandomState(seed)
    # 1) 轴交换 + 旋转 + 斜切
    cases = []
    shear = np.array([[2.0, 0.6, 0.2, -8], [0.3, 2.1, -0.4, -6],
                      [0.1, 0.5, 2.2, -5], [0.0, 0.0, 0.0, 1.0]])
    cases.append(((7, 8, 6), shear, np.array([[1.1, 0.3, 0.0, -3.0],
                                               [0.1, 1.4, 0.2, 0.0],
                                               [0.0, 0.2, 1.6, 1.0], [0, 0, 0, 1]])))
    # 2) 轴交换（permutation）+ 翻转
    swap = np.array([[0.0, 2.0, 0.0, 4.0], [-1.5, 0.0, 0.0, 2.0],
                     [0.0, 0.0, -2.0, 3.0], [0.0, 0.0, 0.0, 1.0]])
    cases.append(((5, 6, 4), swap, np.array([[1.2, 0, 0, 1.0], [0, 1.2, 0, -2.0],
                                              [0, 0, 1.3, 0.5], [0, 0, 0, 1]])))
    # 3) 随机近轴对齐（4 个种子）
    for _ in range(4):
        sa = np.eye(4)
        s = rng.uniform(1.0, 3.0, size=3)
        sa[:3, :3] = np.diag(s) + rng.normal(scale=0.25, size=(3, 3)) * np.array(
            [[0, 1, 1], [1, 0, 1], [1, 1, 0]])
        sa[:3, 3] = rng.uniform(-6, 6, size=3)
        ta = np.eye(4)
        ta[:3, :3] = np.diag(rng.uniform(0.8, 2.2, size=3)) + rng.normal(
            scale=0.2, size=(3, 3)) * np.array([[0, 1, 1], [1, 0, 1], [1, 1, 0]])
        ta[:3, 3] = rng.uniform(-4, 4, size=3)
        shape = tuple(int(x) for x in rng.randint(4, 8, size=3))
        cases.append((shape, sa, ta))
    return cases


def run_reference_checks() -> list:
    """返回失败描述列表（空 = 全部通过）；同时返回统计供打印。"""
    problems = []
    n_checks = 0
    for idx, (shape, sa, ta, ) in enumerate(_cases()):
        rng = np.random.RandomState(1000 + idx)
        src = rng.normal(size=shape).astype(np.float64)
        # 注入 NaN：至少一个点参与邻点时使某些目标点无效
        src.flat[0] = np.nan
        tshape = tuple(max(1, int(s) - 1) for s in shape)
        for chunk in (1, 2, 4, 7, 4096):
            reps = compare_chunked(src, sa, ta, tshape, chunk_sizes=(chunk,))
            rep = reps[0]
            n_checks += 1
            if not rep["ok"]:
                problems.append(
                    f"case {idx} chunk {chunk}: mask_eq={rep['mask_equal']} "
                    f"values_eq={rep['values_equal']} max|diff|={rep['max_abs_diff']}")
        lab = rng.randint(0, 6, size=shape).astype(np.int64)
        reps = compare_chunked(lab, sa, ta, tshape, label=True, chunk_sizes=(1, 3, 64))
        for rep in reps:
            n_checks += 1
            if not rep["ok"]:
                problems.append(
                    f"label case {idx} chunk {rep['chunk_voxels']}: mask_eq={rep['mask_equal']} "
                    f"values_eq={rep['values_equal']}")
    return problems, n_checks


if __name__ == "__main__":  # pragma: no cover
    probs, n = run_reference_checks()
    for p in probs:
        print("FAIL:", p)
    print(f"reference check: {n} 组比较，" + ("PASS" if not probs else "FAIL"))
    raise SystemExit(0 if not probs else 1)
