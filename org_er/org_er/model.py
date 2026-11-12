"""条目记录模型。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Record:
    record_key: str            # 规范条目键：source::source_key（规范化后）
    source: str
    source_key: str
    scenario: str
    # 原始值（保留，不改动）
    legal_name_raw: str
    country_raw: str
    website_host_raw: str
    registration_no_raw: str
    # 规范化值
    legal_name: str
    country_code: str
    website_host: str
    registration_no: str
    # 判定结果
    regno_valid: bool
    host_evidence: bool        # 域名是否可作证据（非空且不在排除名单）
    host_exclusion: str        # '', 'free_email', 'shared_hosting'
    tokens: frozenset
