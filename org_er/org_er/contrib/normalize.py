"""目录行规范化：DOI、年份、供应方、论文号、署名引用。

原则与 org_er.normalize 一致：规范化是纯函数，原始值始终保留。
DOI 归并只清除链接前缀、空白与大小写差异，不改变 DOI 本体。
"""
from __future__ import annotations

import re

from ..normalize import make_record_key, norm_text

# 年份合法范围（发表年份的常识边界；超出即视为非法年份）
MIN_YEAR = 1000
MAX_YEAR = 2100

_WS_ALL = re.compile(r"\s+")
_YEAR_STR = re.compile(r"^\d{4}$")

# DOI 链接前缀（大小写不敏感，去空白后匹配；长的在前，循环剥离）
_DOI_PREFIXES = (
    "https://doi.org/", "http://doi.org/",
    "https://dx.doi.org/", "http://dx.doi.org/",
    "doi.org/", "dx.doi.org/",
    "doi:",
)


def norm_doi(raw) -> str:
    """DOI 规范化：去全部空白 -> 循环去链接前缀（大小写不敏感）-> casefold。

    空值/None/剥离后为空 -> ''（按无 DOI 处理）。
    """
    if raw is None:
        return ""
    s = _WS_ALL.sub("", str(raw))
    lowered = s.casefold()
    changed = True
    while changed and lowered:
        changed = False
        for p in _DOI_PREFIXES:
            if lowered.startswith(p):
                s = s[len(p):]
                lowered = lowered[len(p):]
                changed = True
                break
    return s.casefold()


def parse_year(raw):
    """年份校验：接受整数或 4 位数字字符串，范围 [MIN_YEAR, MAX_YEAR]。

    返回 (year_int, error)。error 非空即非法年份（说明原因）。
    """
    if isinstance(raw, bool):  # JSON true/false 不是年份
        return None, f"年份类型非法: {raw!r}"
    if isinstance(raw, int):
        y = raw
    elif isinstance(raw, str):
        s = raw.strip()
        if not _YEAR_STR.fullmatch(s):
            return None, f"年份不是 4 位数字: {raw!r}"
        y = int(s)
    else:
        return None, f"年份类型非法: {raw!r}"
    if not (MIN_YEAR <= y <= MAX_YEAR):
        return None, f"年份超出范围 [{MIN_YEAR},{MAX_YEAR}]: {y}"
    return y, ""


def norm_supplier(raw) -> str:
    """供应方规范键：空白折叠 + casefold（与 ER 的 source 大小写不敏感一致）。"""
    return norm_text(raw).casefold()


def norm_paper_id(raw) -> str:
    """供应方论文号规范键：空白折叠（大小写敏感，与 ER 的 source_key 一致）。"""
    return norm_text(raw)


def norm_author_name(raw) -> str:
    """作者名规范键（仅用于版本内容比对）：空白折叠 + casefold。"""
    return norm_text(raw).casefold()


def affil_record_key(source, entry) -> str:
    """署名引用 -> ER 规范条目键（与机构解析管线同一 make_record_key）。"""
    return make_record_key(norm_text(source), norm_text(entry))
