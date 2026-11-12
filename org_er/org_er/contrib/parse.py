"""论文目录 JSONL 解析与行级校验。

每行：{"supplier", "paper_id", "doi", "year", "authors": [...]}，可带 "scenario" 等额外字段。
作者：{"name": ..., "affils": [{"source": ..., "entry": ...}, ...]}（affils 可缺省 = 无署名）。

坏行（不计入任何论文，必须说明原因）：
  json_parse_error        JSON 无法解析
  not_an_object           行不是 JSON 对象
  missing_supplier        供应方缺失或空白
  missing_paper_id        供应方论文号缺失或空白
  illegal_year            年份非法（非整数/非 4 位数字串/超出范围）
  broken_author_structure 作者结构损坏（authors 缺失或不是列表、作者不是对象、
                          affils 不是列表、署名条目缺 source/entry 等）
  empty_authors           作者名单为空
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .normalize import (norm_author_name, norm_doi, norm_paper_id,
                        norm_supplier, parse_year)
from ..normalize import norm_text


@dataclass
class AffilRef:
    source: str   # 原目录来源（原始写法，展示用）
    entry: str    # 原目录条目号（原始写法，展示用）

    def key_parts(self):
        """规范化后的 (source, entry)，用于计算 ER 条目键与内容指纹。"""
        return (norm_text(self.source).casefold(), norm_text(self.entry))


@dataclass
class Author:
    name: str                     # 原始写法（缺失时为 ''）
    affils: list = field(default_factory=list)  # AffilRef 列表（原始顺序）

    @property
    def name_norm(self):
        return norm_author_name(self.name)


@dataclass
class CatalogLine:
    """一条目录行的解析结果。status='ok' 时 version 字段可用。"""
    line_no: int
    raw: str
    status: str = "ok"            # 'ok' | 'rejected'
    reject_reason: str = ""
    supplier: str = ""            # 原始写法
    paper_id: str = ""
    doi_raw: str = ""
    doi_norm: str = ""
    year_raw: str = ""
    year: int = 0                 # 仅 ok 时有效
    authors: list = field(default_factory=list)  # Author 列表（仅 ok）

    @property
    def supplier_norm(self):
        return norm_supplier(self.supplier)

    @property
    def paper_id_norm(self):
        return norm_paper_id(self.paper_id)


def _parse_authors(node):
    """解析作者列表，返回 (authors, error)。error 非空即结构损坏/为空。"""
    if node is None:
        return None, "broken_author_structure: 缺少 authors 字段"
    if not isinstance(node, list):
        return None, f"broken_author_structure: authors 不是列表（{type(node).__name__}）"
    if not node:
        return None, "empty_authors: 作者名单为空"
    authors = []
    for i, a in enumerate(node):
        if not isinstance(a, dict):
            return None, f"broken_author_structure: 第 {i + 1} 位作者不是对象"
        affils_node = a.get("affils", [])
        if not isinstance(affils_node, list):
            return None, f"broken_author_structure: 第 {i + 1} 位作者 affils 不是列表"
        affils = []
        for j, r in enumerate(affils_node):
            if not isinstance(r, dict):
                return None, f"broken_author_structure: 第 {i + 1} 位作者第 {j + 1} 条署名不是对象"
            source, entry = r.get("source"), r.get("entry")
            if source is None or entry is None:
                return None, (f"broken_author_structure: 第 {i + 1} 位作者第 {j + 1} 条署名"
                              f"缺少 source/entry")
            if not norm_text(source) or not norm_text(entry):
                return None, (f"broken_author_structure: 第 {i + 1} 位作者第 {j + 1} 条署名"
                              f"source/entry 为空白")
            affils.append(AffilRef(source=str(source), entry=str(entry)))
        name = a.get("name", "")
        authors.append(Author(name=norm_text(name) if name is not None else "",
                              affils=affils))
    return authors, ""


def parse_line(line_no: int, raw: str) -> CatalogLine:
    """解析一行目录数据；任何坏行都给出原因，绝不抛异常中断作业。"""
    line = CatalogLine(line_no=line_no, raw=raw.rstrip("\n"))
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        line.status, line.reject_reason = "rejected", f"json_parse_error: {e}"
        return line
    if not isinstance(obj, dict):
        line.status, line.reject_reason = "rejected", "not_an_object: 行不是 JSON 对象"
        return line

    line.supplier = norm_text(obj.get("supplier", ""))
    line.paper_id = norm_text(obj.get("paper_id", ""))
    if not line.supplier:
        line.status, line.reject_reason = "rejected", "missing_supplier: 供应方缺失或空白"
        return line
    if not line.paper_id:
        line.status, line.reject_reason = "rejected", "missing_paper_id: 供应方论文号缺失或空白"
        return line

    doi_raw = obj.get("doi", "")
    line.doi_raw = "" if doi_raw is None else str(doi_raw)
    line.doi_norm = norm_doi(doi_raw)

    year_raw = obj.get("year")
    line.year_raw = "" if year_raw is None else str(year_raw)
    year, err = parse_year(year_raw)
    if err:
        line.status, line.reject_reason = "rejected", f"illegal_year: {err}"
        return line
    line.year = year

    authors, err = _parse_authors(obj.get("authors"))
    if err:
        line.status, line.reject_reason = "rejected", err
        return line
    line.authors = authors
    line.status = "ok"
    return line


def parse_catalog(path) -> list:
    """逐行解析目录文件，返回 CatalogLine 列表（含坏行，保留输入行号）。"""
    lines = []
    with open(path, encoding="utf-8") as f:
        for i, raw in enumerate(f, start=1):
            if not raw.strip():
                continue  # 空行不算数据行，也不计行号偏移
            lines.append(parse_line(i, raw))
    return lines
