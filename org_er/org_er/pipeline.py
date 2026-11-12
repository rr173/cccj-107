"""确定性实体解析管线：规范化 -> 三路阻塞 -> 规则评估 -> 约束连通分量 -> 迁移映射。

管线是“全部当前原始条目”的纯函数：同样的 raw_records 必得同样的结果，
与输入顺序、历史状态无关。增量修订 = 修改 raw_records 后重新执行本函数。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import store
from .entity import build_entities
from .model import Record
from .normalize import (make_record_key, name_tokens, norm_country, norm_host,
                        norm_regno, norm_text)
from .rules import evaluate_pair, host_exclusion, regno_is_valid

# 三路阻塞：国家+名称token 倒排、合法注册号索引、域名索引（均为等值连接，禁止全配对）
BLOCKING_SQL = """
WITH big_tokens AS (
  SELECT country_code, token FROM name_tokens
  GROUP BY country_code, token HAVING COUNT(*) > ?
),
regno_pairs AS (
  SELECT a.record_key AS key_a, b.record_key AS key_b
  FROM records a
  JOIN records b
    ON a.country_code = b.country_code
   AND a.registration_no = b.registration_no
   AND a.regno_valid AND b.regno_valid
   AND a.record_key < b.record_key
),
domain_pairs AS (
  SELECT a.record_key AS key_a, b.record_key AS key_b
  FROM records a
  JOIN records b
    ON a.website_host = b.website_host
   AND a.host_evidence AND b.host_evidence
   AND a.record_key < b.record_key
),
token_pairs AS (
  SELECT DISTINCT a.record_key AS key_a, b.record_key AS key_b
  FROM name_tokens a
  JOIN name_tokens b
    ON a.country_code = b.country_code
   AND a.token = b.token
   AND a.record_key < b.record_key
  WHERE NOT EXISTS (SELECT 1 FROM big_tokens t
                    WHERE t.country_code = a.country_code AND t.token = a.token)
)
SELECT key_a, key_b,
       MAX(CASE WHEN src = 'token'  THEN 1 ELSE 0 END) AS via_token,
       MAX(CASE WHEN src = 'regno'  THEN 1 ELSE 0 END) AS via_regno,
       MAX(CASE WHEN src = 'domain' THEN 1 ELSE 0 END) AS via_domain
FROM (
  SELECT key_a, key_b, 'token'  AS src FROM token_pairs
  UNION ALL
  SELECT key_a, key_b, 'regno'  AS src FROM regno_pairs
  UNION ALL
  SELECT key_a, key_b, 'domain' AS src FROM domain_pairs
) u
GROUP BY key_a, key_b
ORDER BY key_a, key_b
"""


@dataclass
class BuildResult:
    round_no: int
    records: dict = field(default_factory=dict)   # key -> Record
    candidates: list = field(default_factory=list)  # (key_a, key_b, via_token, via_regno, via_domain, rule_fired, score, notes)
    edges: list = field(default_factory=list)       # 规则命中的边（含 status/reason/evidence）
    members: dict = field(default_factory=dict)     # key -> entity_id
    migrations: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def normalize_record(row, cfg) -> Record:
    """row: (record_key, source, source_key, scenario, legal_name, country_code, website_host, registration_no)"""
    key, source, source_key, scenario, name_raw, country_raw, host_raw, regno_raw = row
    legal_name = norm_text(name_raw, cfg.unicode_form)
    country = norm_country(country_raw, cfg.unicode_form)
    host = norm_host(host_raw, cfg.unicode_form, cfg.strip_www)
    regno = norm_regno(regno_raw, cfg.unicode_form, cfg.regno_strip_pattern)
    valid = regno_is_valid(regno, country, cfg)
    excl = host_exclusion(host, cfg)
    tokens = name_tokens(legal_name, cfg.legal_form_tokens, cfg.cjk_legal_suffixes)
    return Record(
        record_key=key,
        source=norm_text(source, cfg.unicode_form),
        source_key=norm_text(source_key, cfg.unicode_form),
        scenario=scenario or "",
        legal_name_raw=name_raw or "",
        country_raw=country_raw or "",
        website_host_raw=host_raw or "",
        registration_no_raw=regno_raw or "",
        legal_name=legal_name,
        country_code=country,
        website_host=host,
        registration_no=regno,
        regno_valid=valid,
        host_evidence=bool(host) and not excl,
        host_exclusion=excl,
        tokens=tokens,
    )


def load_records(con, cfg) -> dict:
    records = {}
    for row in store.read_raw_rows(con):
        r = normalize_record(row, cfg)
        records[r.record_key] = r
    return records


def refresh_norm_tables(con, records: dict):
    con.execute("DELETE FROM records")
    con.execute("DELETE FROM name_tokens")
    store.executemany(con, 
        "INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [[r.record_key, r.source, r.source_key, r.scenario,
          r.legal_name_raw, r.country_raw, r.website_host_raw, r.registration_no_raw,
          r.legal_name, r.country_code, r.website_host, r.registration_no,
          r.regno_valid, r.host_evidence, r.host_exclusion,
          json.dumps(sorted(r.tokens), ensure_ascii=False)] for r in records.values()],
    )
    store.executemany(con, 
        "INSERT INTO name_tokens VALUES (?,?,?)",
        [[r.record_key, r.country_code, t] for r in records.values() for t in sorted(r.tokens)],
    )


def diff_members(prev: dict, new: dict, round_no: int) -> list:
    """计算实体成员迁移映射：旧标识 -> 新标识，标注 split/merge/new/removed。"""
    old_by_ent, new_by_ent = {}, {}
    for k, e in prev.items():
        old_by_ent.setdefault(e, set()).add(k)
    for k, e in new.items():
        new_by_ent.setdefault(e, set()).add(k)
    rows = []
    for k in sorted(set(prev) | set(new)):
        o, n = prev.get(k), new.get(k)
        if o == n:
            continue  # 稳定，不记录
        if o is None:
            ct = "new"
        elif n is None:
            ct = "removed"
        else:
            split = len({new.get(m) for m in old_by_ent.get(o, ()) if m in new}) > 1
            merge = len({prev.get(m) for m in new_by_ent.get(n, ()) if m in prev}) > 1
            if split and merge:
                ct = "split+merge"
            elif split:
                ct = "split"
            elif merge:
                ct = "merge"
            else:
                ct = "renamed"  # 理论上不发生（标识 = 成员函数）
        rows.append((round_no, k, o, n, ct))
    return rows


def rebuild(con, cfg) -> BuildResult:
    """在全部当前条目上执行确定算法；返回完整结果并落库。"""
    round_no = int(store.get_meta(con, "current_round", "0"))

    records = load_records(con, cfg)
    refresh_norm_tables(con, records)

    # 1) 三路阻塞生成候选（SQL 等值连接，禁止全配对）
    pair_rows = con.execute(BLOCKING_SQL, [cfg.max_token_block_size]).fetchall()

    # 2) 规则评估（与穷举参考程序共用 evaluate_pair）
    candidates, edge_dicts = [], []
    for key_a, key_b, vt, vr, vd in pair_rows:
        res = evaluate_pair(records[key_a], records[key_b], cfg)
        candidates.append((key_a, key_b, bool(vt), bool(vr), bool(vd),
                           res["edge_type"] or "", res["score"], "; ".join(res["notes"])))
        if res["fired"]:
            edge_dicts.append({"key_a": key_a, "key_b": key_b,
                               "edge_type": res["edge_type"], "score": res["score"],
                               "evidence": res["evidence"]})

    # 3) 约束连通分量（先强边，弱边按分数降序、端点键升序）
    edges, members = build_entities(records, edge_dicts)

    # 4) 与上一轮成员比对，生成迁移映射（round 0 为初始构建，无旧标识，不产生迁移）
    prev = {}
    if round_no > 0:
        prev = dict(con.execute(
            "SELECT record_key, entity_id FROM entity_members WHERE round_no = ?",
            [round_no - 1]).fetchall())
    migrations = diff_members(prev, members, round_no) if round_no > 0 else []

    # 5) 落库（本轮幂等：先清后写）
    for table in ("candidates", "edges", "entity_members"):
        con.execute(f"DELETE FROM {table} WHERE round_no = ?", [round_no])
    store.executemany(con, 
        "INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?,?)",
        [[round_no, *c] for c in candidates])
    store.executemany(con, 
        "INSERT INTO edges VALUES (?,?,?,?,?,?,?,?,?)",
        [[round_no, e["key_a"], e["key_b"], e["edge_type"], e["score"], e["status"],
          e["redundant"], e["reason"], json.dumps(e["evidence"], ensure_ascii=False, sort_keys=True)]
         for e in edges])
    store.executemany(con, 
        "INSERT INTO entity_members VALUES (?,?,?)",
        [[round_no, eid, k] for k, eid in sorted(members.items())])
    con.execute("DELETE FROM entity_migrations WHERE round_no = ?", [round_no])
    store.executemany(con, 
        "INSERT INTO entity_migrations VALUES (?,?,?,?,?)",
        [list(m) for m in migrations])

    # 6) 统计
    n = len(records)
    all_pairs = n * (n - 1) // 2
    stats = {
        "n_records": n,
        "all_pairs": all_pairs,
        "cand_via_token": sum(1 for c in candidates if c[2]),
        "cand_via_regno": sum(1 for c in candidates if c[3]),
        "cand_via_domain": sum(1 for c in candidates if c[4]),
        "candidates_total": len(candidates),
        "rule_matched": len(edge_dicts),
        "edges_accepted": sum(1 for e in edges if e["status"] == "accepted"),
        "edges_rejected": sum(1 for e in edges if e["status"] == "rejected"),
        "n_entities": len(set(members.values())),
        "candidate_savings_pct": round(100 * (1 - len(candidates) / all_pairs), 2) if all_pairs else 0.0,
    }
    con.execute("DELETE FROM run_stats WHERE round_no = ?", [round_no])
    store.executemany(con, "INSERT INTO run_stats VALUES (?,?,?)",
                    [[round_no, k, str(v)] for k, v in sorted(stats.items())])

    return BuildResult(round_no=round_no, records=records, candidates=candidates,
                       edges=edges, members=members, migrations=migrations, stats=stats)


def fresh_build(raw_rows, cfg) -> BuildResult:
    """在全新内存库上从原始条目重建（用于乱序/等价性验证）。"""
    con = store.open_db(":memory:")
    store.init_db(con)
    for row in raw_rows:
        key, source, source_key, scenario, name, country, host, regno = row
        store.upsert_raw(con, key, {"source": source, "source_key": source_key,
                                    "scenario": scenario, "legal_name": name,
                                    "country_code": country, "website_host": host,
                                    "registration_no": regno})
    store.set_meta(con, "current_round", 0)
    result = rebuild(con, cfg)
    con.close()
    return result


def state_signature(result: BuildResult):
    """用于等价性比较的规范签名：成员映射 + 边集合（含状态/分数/原因）。"""
    members = dict(sorted(result.members.items()))
    edges = sorted((e["key_a"], e["key_b"], e["edge_type"], e["score"], e["status"], e["reason"])
                   for e in result.edges)
    return members, edges
