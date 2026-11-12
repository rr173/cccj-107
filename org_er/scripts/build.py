#!/usr/bin/env python3
"""批量构建入口：从原始条目 JSONL 全量构建实体解析数据集（round 0）。"""
import argparse
import json

import _bootstrap  # noqa: F401
from org_er import store
from org_er.config import load_config
from org_er.normalize import make_record_key
from org_er.pipeline import rebuild
from org_er.verifylib import check_config_examples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(_bootstrap.ROOT / "config" / "er_config.json"))
    ap.add_argument("--records", default=str(_bootstrap.ROOT / "data" / "records.jsonl"))
    ap.add_argument("--db", default=str(_bootstrap.ROOT / "output" / "er.duckdb"))
    ap.add_argument("--out", default=str(_bootstrap.ROOT / "output"))
    args = ap.parse_args()

    cfg = load_config(args.config)
    problems = check_config_examples(cfg)
    if problems:
        for p in problems:
            print("配置自检失败:", p)
        raise SystemExit(1)
    print("配置自检通过（注册号规则与占位值样例全部符合）")

    con = store.open_db(args.db)
    store.init_db(con)
    with open(args.records, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            key = make_record_key(rec["source"], rec["source_key"], cfg.unicode_form)
            store.upsert_raw(con, key, rec)
    store.set_meta(con, "current_round", 0)

    result = rebuild(con, cfg)
    store.export_outputs(con, args.out, result.round_no)

    s = result.stats
    print(f"round 0 构建完成：{s['n_records']} 条目 -> {s['n_entities']} 实体，"
          f"接受边 {s['edges_accepted']}，拒绝边 {s['edges_rejected']}")
    print(f"候选 {s['candidates_total']}（token {s['cand_via_token']} / 注册号 {s['cand_via_regno']} / "
          f"域名 {s['cand_via_domain']}），全配对 {s['all_pairs']}，节省 {s['candidate_savings_pct']}%")
    print(f"交付表已导出至 {args.out}（entity_members / match_edges / rejected_edges / candidates / entity_migrations）")
    con.close()


if __name__ == "__main__":
    main()
