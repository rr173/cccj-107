"""mzML 受控词汇（PSI-MS / UO）访问号与参数解析。

只按 **标准命名空间 + cvParam 的 accession 受控词汇标记**辨认事实，绝不靠标签在
文件里的固定次序猜字段。支持直接写在节点内的 cvParam，也支持
referenceableParamGroupRef 引用（可嵌套）的参数组：

- 参数组不存在 / 引用成环 -> 输入失败；
- 同一事实出现互斥声明（不同取值、或正负极性同时出现）-> 输入失败；
- 同一标记重复但取值一致 -> 允许（幂等重复声明）。
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

from .errors import MzMLError

MZML_NS = "http://psi.hupo.org/ms/mzml"
NS = {"mz": MZML_NS}


def q(tag: str) -> str:
    """带 mzML 标准命名空间的限定标签名。"""
    return f"{{{MZML_NS}}}{tag}"


# ---------------------------------------------------------------- 受控词汇访问号
# 谱图级别（MS: 前缀为 PSI-MS）
MS_LEVEL = "MS:1000511"          # ms level，值为整数
CENTROID = "MS:1000127"          # centroid spectrum（存在性标记）
PROFILE = "MS:1000128"           # profile spectrum（存在性标记）
POSITIVE_SCAN = "MS:1000130"     # positive scan
NEGATIVE_SCAN = "MS:1000129"     # negative scan
SCAN_START_TIME = "MS:1000016"   # scan start time（带单位）

# 单位（UO: 前缀为 Units Ontology）
UNIT_SECOND = "UO:0000010"
UNIT_MINUTE = "UO:0000031"
_SECOND_NAMES = {"second", "seconds", "sec", "secs", "s"}
_MINUTE_NAMES = {"minute", "minutes", "min", "mins"}

# binaryDataArray 类型
MZ_ARRAY = "MS:1000514"          # m/z array
INTENSITY_ARRAY = "MS:1000515"   # intensity array
BINARY_TYPE = "MS:1000524"       # binary data（存在性标记，值通常为空）
FLOAT_32 = "MS:1000521"          # 32-bit float
FLOAT_64 = "MS:1000523"          # 64-bit float
NO_COMPRESSION = "MS:1000576"    # no compression
ZLIB_COMPRESSION = "MS:1000574"  # zlib compression

# 明确不支持的编码（Numpress 等）：遇到即报具体原因，绝不把未知字节当浮点数
NUMPRESS_LINEAR = "MS:1002312"   # Numpress linear prediction
NUMPRESS_PIC = "MS:1002313"      # Numpress positive integer
NUMPRESS_SLOF = "MS:1002314"     # Numpress short logged float
UNSUPPORTED_ENCODINGS = {
    NUMPRESS_LINEAR: "Numpress linear prediction (MS:1002312)",
    NUMPRESS_PIC: "Numpress positive integer (MS:1002313)",
    NUMPRESS_SLOF: "Numpress short logged float (MS:1002314)",
}
# 其它已知但本作业不支持的压缩
OTHER_UNSUPPORTED = {
    "MS:1002747": "truncated binary data arrays (MS:1002747)",
    "MS:1003055": "no compression (pwiz, MS:1003055) combined with unsupported array type",
}


class Fact:
    """一条受控词汇事实（accession + 原值 + 单位）。"""

    __slots__ = ("accession", "name", "value", "unit_accession", "unit_name")

    def __init__(self, accession, name, value, unit_accession, unit_name):
        self.accession = accession
        self.name = name or ""
        self.value = value
        self.unit_accession = unit_accession
        self.unit_name = unit_name

    def __repr__(self):
        return f"Fact({self.accession!r}, value={self.value!r}, unit={self.unit_accession!r})"


def _direct_cvparams(node) -> list[Fact]:
    out = []
    for cv in node.findall(q("cvParam")):
        acc = cv.get("accession")
        if not acc:
            # 标准 mzML 的 cvParam 必带 accession；缺失说明文件不符合规范
            raise MzMLError(
                f"节点 <{node.tag.split('}')[-1]}> 含无 accession 的 cvParam，"
                "无法按受控词汇辨认")
        out.append(Fact(acc, cv.get("name"), cv.get("value"),
                        cv.get("unitAccession"), cv.get("unitName")))
    return out


def collect_facts(node, groups: dict, context: str, _seen: tuple = ()) -> dict[str, list[Fact]]:
    """收集节点上的全部受控词汇事实（直接 cvParam + 参数组引用，递归展开）。

    ``groups`` 是 referenceableParamGroup 的 id -> 节点映射。返回
    accession -> Fact 列表（保持文件出现顺序）。
    """
    facts: dict[str, list[Fact]] = {}

    def add(f: Fact) -> None:
        facts.setdefault(f.accession, []).append(f)

    # 1) 直接写在节点内的 cvParam
    for f in _direct_cvparams(node):
        add(f)

    # 2) referenceableParamGroupRef 引用（参数组可嵌套引用别的参数组）
    for ref in node.findall(q("referenceableParamGroupRef")):
        ref_id = ref.get("ref")
        if not ref_id:
            raise MzMLError(f"{context}: referenceableParamGroupRef 缺少 ref 属性")
        if ref_id in _seen:
            raise MzMLError(
                f"{context}: referenceableParamGroup 引用成环: {' -> '.join(_seen + (ref_id,))}")
        if ref_id not in groups:
            raise MzMLError(
                f"{context}: 引用了不存在的 referenceableParamGroup id={ref_id!r}")
        group_node = groups[ref_id]
        sub = collect_facts(group_node, groups,
                            f"{context} -> paramGroup[{ref_id}]", _seen + (ref_id,))
        for acc, lst in sub.items():
            facts.setdefault(acc, []).extend(lst)
    return facts


def require_unique(facts: dict, accession: str, context: str) -> Fact | None:
    """取某 accession 的唯一事实；重复但取值一致允许，互斥取值即失败。"""
    entries = facts.get(accession)
    if not entries:
        return None
    distinct = {(e.value, e.unit_accession) for e in entries}
    if len(distinct) > 1:
        detail = ", ".join(f"value={e.value!r} unit={e.unit_accession!r}" for e in entries)
        raise MzMLError(f"{context}: {accession} 出现互斥声明（{detail}）")
    return entries[0]


def require_flag(facts: dict, accession: str, context: str = "") -> bool:
    """存在性标记是否出现（重复出现幂等）。"""
    return accession in facts


def require_exclusive(facts: dict, acc_a: str, acc_b: str, context: str) -> str | None:
    """两个互斥存在性标记必须至多出现一个；返回出现的那个 accession。"""
    a = acc_a in facts
    b = acc_b in facts
    if a and b:
        raise MzMLError(f"{context}: 互斥标记 {acc_a} 与 {acc_b} 同时出现")
    if a:
        return acc_a
    if b:
        return acc_b
    return None


def parse_referenceable_groups(root) -> dict:
    """读取 referenceableParamGroupList 下的全部参数组（id -> 节点）。"""
    groups: dict[str, ET.Element] = {}
    container = root.find(q("referenceableParamGroupList"))
    if container is None:
        return groups
    for grp in container.findall(q("referenceableParamGroup")):
        gid = grp.get("id")
        if not gid:
            raise MzMLError("referenceableParamGroup 缺少 id 属性")
        if gid in groups:
            raise MzMLError(f"referenceableParamGroup id 重复: {gid!r}")
        groups[gid] = grp
    # 预解析一遍以暴露成环引用（递归收集会检测到环）
    for gid, node in groups.items():
        collect_facts(node, groups, f"paramGroup[{gid}]", (gid,))
    return groups
