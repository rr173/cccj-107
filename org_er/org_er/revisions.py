"""条目修订：upsert / retract 事件作用于 raw_records，随后由管线确定性重建。

事件格式（JSONL）：
  {"op": "upsert",  "record": {"source": ..., "source_key": ..., "legal_name": ...,
                               "country_code": ..., "website_host": ..., "registration_no": ...}}
  {"op": "retract", "source": ..., "source_key": ...}     # 证据撤回
"""
from __future__ import annotations

import json

from . import store
from .normalize import make_record_key


def apply_events(con, cfg, events: list, round_no: int) -> list:
    """应用一批修订事件，返回 [(op, record_key), ...]。"""
    applied = []
    for ev in events:
        op = ev["op"]
        if op == "upsert":
            rec = ev["record"]
            key = make_record_key(rec["source"], rec["source_key"], cfg.unicode_form)
            store.upsert_raw(con, key, rec)
        elif op == "retract":
            key = make_record_key(ev["source"], ev["source_key"], cfg.unicode_form)
            store.retract_raw(con, key)
        else:
            raise ValueError(f"未知修订操作: {op}")
        con.execute("INSERT INTO revision_log VALUES (?,?,?,?)",
                    [round_no, op, key, json.dumps(ev, ensure_ascii=False, sort_keys=True)])
        applied.append((op, key))
    store.set_meta(con, "current_round", round_no)
    return applied


def load_events(path) -> list:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
