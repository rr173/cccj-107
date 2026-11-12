#!/usr/bin/env python3
"""验证入口：对当前数据库状态执行全部核验。

1) 配置样例自检（注册号规则 / 占位值）
2) 小规模穷举参考程序：候选不漏掉规则可接受的边，且管线判定与穷举一致
3) 实体约束不变量：同实体同国家至多一个有效注册号
4) 输入顺序打乱 -> 结果签名一致
5) 增量结果 == 在全部当前条目上全量重算
"""
import argparse

import _bootstrap  # noqa: F401
from org_er import store
from org_er.config import load_config
from org_er.pipeline import rebuild
from org_er.reference import check_against_pipeline
from org_er.verifylib import (check_config_examples, check_constraint,
                              check_incremental_equivalence, check_shuffle_stability)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(_bootstrap.ROOT / "config" / "er_config.json"))
    ap.add_argument("--db", default=str(_bootstrap.ROOT / "output" / "er.duckdb"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    con = store.open_db(args.db)
    store.init_db(con)
    round_no = int(store.get_meta(con, "current_round", "0"))
    raw_rows = store.read_raw_rows(con)

    ok_all = True

    # 1) 配置自检
    problems = check_config_examples(cfg)
    print(f"[1] 配置样例自检: {'PASS' if not problems else 'FAIL'}")
    for p in problems:
        print("    -", p)
    ok_all &= not problems

    # 2) 穷举参考程序 vs 管线（在全部当前条目上）
    current = rebuild(con, cfg)  # 当前轮次幂等重建，拿到内存结果
    rep = check_against_pipeline(current.records, cfg, current.candidates, current.edges)
    print(f"[2] 穷举参考核验: {'PASS' if rep['ok'] else 'FAIL'} "
          f"(N={rep['n_records']}, 全配对={rep['all_pairs']}, 候选={rep['n_candidates']}, "
          f"规则可接受边={rep['n_reference_pairs']})")
    for k in ("missing_in_candidates", "not_evaluated", "extra_in_pipeline", "mismatched"):
        if rep[k]:
            print(f"    - {k}: {rep[k][:5]}")
    ok_all &= rep["ok"]

    # 3) 实体约束不变量
    violations = check_constraint(current.members, current.records)
    print(f"[3] 注册号唯一性约束: {'PASS' if not violations else 'FAIL'}")
    for v in violations:
        print("    -", v)
    ok_all &= not violations

    # 4) 乱序稳定性
    shuf = check_shuffle_stability(raw_rows, cfg)
    print(f"[4] 输入顺序打乱稳定性 (seeds={shuf['seeds']}): {'PASS' if shuf['ok'] else 'FAIL'}")
    ok_all &= shuf["ok"]

    # 5) 增量 == 全量重算（对全部当前条目重新执行确定算法）
    eq = check_incremental_equivalence(raw_rows, current, cfg)
    print(f"[5] 增量结果 == 全量重算: {'PASS' if eq['ok'] else 'FAIL'}")
    ok_all &= eq["ok"]

    con.close()
    print("总体:", "PASS" if ok_all else "FAIL")
    raise SystemExit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
