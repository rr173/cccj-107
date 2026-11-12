#!/usr/bin/env python3
"""修订入口：应用一轮修订事件（upsert/retract），确定性重建并输出实体迁移映射。"""
import argparse

import _bootstrap  # noqa: F401
from org_er import store
from org_er.config import load_config
from org_er.pipeline import rebuild
from org_er.revisions import apply_events, load_events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(_bootstrap.ROOT / "config" / "er_config.json"))
    ap.add_argument("--db", default=str(_bootstrap.ROOT / "output" / "er.duckdb"))
    ap.add_argument("--events", required=True)
    ap.add_argument("--round", type=int, required=True)
    ap.add_argument("--out", default=str(_bootstrap.ROOT / "output"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    con = store.open_db(args.db)
    store.init_db(con)

    events = load_events(args.events)
    applied = apply_events(con, cfg, events, args.round)
    for op, key in applied:
        print(f"  已应用 {op}: {key}")

    result = rebuild(con, cfg)
    store.export_outputs(con, args.out, result.round_no)

    print(f"round {args.round} 重建完成：{result.stats['n_records']} 条目 -> "
          f"{result.stats['n_entities']} 实体，迁移 {len(result.migrations)} 条")
    for round_no, key, old, new, ct in result.migrations:
        print(f"  [{ct:11s}] {key}: {old} -> {new}")
    con.close()


if __name__ == "__main__":
    main()
