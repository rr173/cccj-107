"""实体构建：被接受匹配边的弱边/强边的连通分量，带注册号唯一性约束。

约束：同一分量中同一国家不能出现两个不同有效注册号。
考察顺序（确定）：先强边（规范端点键升序），再弱边（分数降序、规范端点键升序）。
违反约束的边进入拒绝表并保留原因；已连通的边记为冗余接受。
实体标识 = 分量最小规范条目键的 SHA1摘要。
"""
from __future__ import annotations

import hashlib
import json


class DSU:
    """并查集，根上维护 country -> registration_no 映射（每国至多一个有效注册号）。"""

    def __init__(self, keys):
        self.parent = {k: k for k in keys}
        self.size = {k: 1 for k in keys}
        self.regnos = {}

    def find(self, x):
        p = self.parent
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:  # 路径压缩
            p[x], x = root, p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        self.regnos[ra].update(self.regnos[rb])
        del self.regnos[rb]
        return ra


def entity_id_of(member_keys) -> str:
    """实体标识：分量最小规范条目键的摘要（与输入顺序无关）。"""
    mk = min(member_keys)
    return "ent_" + hashlib.sha1(mk.encode("utf-8")).hexdigest()[:12]


def build_entities(records: dict, edges: list):
    """records: key -> Record；edges: 规则命中的边（含 edge_type/score/evidence）。

    返回 (edges_with_status, members: key -> entity_id)。
    """
    dsu = DSU(records.keys())
    for k, r in records.items():
        dsu.regnos[k] = {r.country_code: r.registration_no} if r.regno_valid else {}

    # 确定顺序：强边按端点键升序；弱边按分数降序、端点键升序
    strong = sorted((e for e in edges if e["edge_type"] == "strong"),
                    key=lambda e: (e["key_a"], e["key_b"]))
    weak = sorted((e for e in edges if e["edge_type"] == "weak"),
                  key=lambda e: (-e["score"], e["key_a"], e["key_b"]))
    ordered = strong + weak

    for e in ordered:
        ra, rb = dsu.find(e["key_a"]), dsu.find(e["key_b"])
        if ra == rb:
            e["status"], e["redundant"], e["reason"] = "accepted", True, ""
            continue
        conflicts = {
            c: (dsu.regnos[ra][c], dsu.regnos[rb][c])
            for c in dsu.regnos[ra].keys() & dsu.regnos[rb].keys()
            if dsu.regnos[ra][c] != dsu.regnos[rb][c]
        }
        if conflicts:
            e["status"], e["redundant"] = "rejected", False
            e["reason"] = json.dumps({
                "violation": "regno_conflict",
                "detail": {c: {"component_a": a, "component_b": b}
                           for c, (a, b) in sorted(conflicts.items())},
            }, ensure_ascii=False, sort_keys=True)
            continue
        dsu.union(ra, rb)
        e["status"], e["redundant"], e["reason"] = "accepted", False, ""

    comp = {}
    for k in records:
        comp.setdefault(dsu.find(k), []).append(k)
    members = {}
    for ks in comp.values():
        eid = entity_id_of(ks)
        for k in ks:
            members[k] = eid
    return ordered, members
