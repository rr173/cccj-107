"""规范化：Unicode、空白、域名、注册号、名称 token。

原则：所有规范化都是纯函数；原始值始终保留在 raw_* 字段，规范化值另存。
"""
from __future__ import annotations

import re
import unicodedata

_WS = re.compile(r"\s+")
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_TOKEN_SPLIT = re.compile(r"[^\w]+", re.UNICODE)
_CJK = re.compile(r"[一-鿿㐀-䶿]")


def norm_text(s, form: str = "NFKC") -> str:
    """Unicode 规范化 + 空白折叠；None -> ''。"""
    if s is None:
        return ""
    return _WS.sub(" ", unicodedata.normalize(form, str(s))).strip()


def norm_country(s, form: str = "NFKC") -> str:
    return norm_text(s, form).upper()


def norm_host(s, form: str = "NFKC", strip_www: bool = True) -> str:
    """域名规范化：小写、去协议/路径/端口/末尾点、IDNA 转 punycode、可选去 www. 前缀。"""
    h = norm_text(s, form).lower()
    h = _SCHEME.sub("", h)
    for sep in ("/", "?", "#"):
        h = h.split(sep)[0]
    h = h.rstrip(".")
    if ":" in h:  # 去端口号
        head, _, port = h.rpartition(":")
        if port.isdigit():
            h = head
    if h:
        try:
            h = h.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            pass
    if strip_www and h.startswith("www."):
        h = h[4:]
    return h


def norm_regno(s, form: str = "NFKC", strip_pattern: str = "") -> str:
    """注册号规范化：Unicode 规范化、大写、按配置正则去分隔符（默认去空白/连字符/点）。"""
    r = norm_text(s, form).upper()
    if strip_pattern:
        r = re.sub(strip_pattern, "", r)
    return r


def name_tokens(norm_name: str, legal_form_tokens=frozenset(), cjk_legal_suffixes=()) -> frozenset:
    """名称 token 集：casefold、按非词字符切分、去法定形式词。

    拉丁语系：法定形式词作为独立 token 精确去除；
    CJK：名称通常无空格，整串为单 token，按配置的法定后缀剥离（如“有限公司”）。
    """
    toks = set()
    for t in _TOKEN_SPLIT.split(norm_name.casefold()):
        if not t:
            continue
        if t in legal_form_tokens:
            continue
        if _CJK.search(t):
            changed = True
            while changed:
                changed = False
                for sfx in cjk_legal_suffixes:
                    if len(t) > len(sfx) and t.endswith(sfx):
                        t = t[: -len(sfx)]
                        changed = True
            if not t:
                continue
        toks.add(t)
    return frozenset(toks)


def make_record_key(source, source_key, form: str = "NFKC") -> str:
    """规范条目键：source（大小写不敏感）与 source_key 各自规范化后拼接。"""
    return f"{norm_text(source, form).casefold()}::{norm_text(source_key, form)}"
