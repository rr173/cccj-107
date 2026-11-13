"""mzML 流式解码（标准命名空间 + 受控词汇，不依赖厂商软件）。

只接受带明确 **centroid** 标记的 MS1 谱图：MS2 跳过，profile MS1 明确拒绝，
无 centroiding 标记的 MS1 也拒绝（不靠默认值猜）。每条参与分析的扫描必须能
确定唯一极性。binaryDataArray 按类型访问号辨认 m/z 与 intensity，base64 后
按无压缩或 zlib 解压，再按声明的 32/64 位 IEEE 小端浮点解释；两个数组可以有
不同精度和压缩。Numpress 等未支持编码报出具体原因，绝不把未知字节当浮点数。

用 ``xml.etree.ElementTree.iterparse`` 增量解析，每个 spectrum 处理完即
``elem.clear()`` 并清空前序节点，内存不随扫描总数增长。
"""
from __future__ import annotations

import base64
import math
import zlib
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import numpy as np

from . import cv
from .cv import (BINARY_TYPE, CENTROID, FLOAT_32, FLOAT_64, INTENSITY_ARRAY,
                 MZ_ARRAY, MZML_NS, NEGATIVE_SCAN, NO_COMPRESSION, POSITIVE_SCAN,
                 PROFILE, SCAN_START_TIME, UNSUPPORTED_ENCODINGS, ZLIB_COMPRESSION,
                 collect_facts, parse_referenceable_groups, q, require_exclusive,
                 require_flag, require_unique)
from .errors import MzMLError


@dataclass
class DecodedScan:
    """一张参与分析的 centroid MS1 扫描（数组用完即可释放）。"""
    index: int                 # 文件中 spectrum 序号（0 起，含被跳过的谱图计数）
    time_seconds: float
    polarity: str              # "positive" / "negative"
    mz: np.ndarray             # float64，非递减
    intensity: np.ndarray      # float64，有限非负
    n_peaks: int
    default_array_length: int


def _scan_node(spectrum_elem, context: str):
    scan_list = spectrum_elem.find(q("scanList"))
    if scan_list is None:
        raise MzMLError(f"{context}: 缺少 scanList，无法确定扫描时间")
    scans = scan_list.findall(q("scan"))
    if len(scans) != 1:
        raise MzMLError(
            f"{context}: 每张谱图须恰好一个 scan，实际 {len(scans)} 个")
    return scans[0]


def _scan_time_seconds(spectrum_elem, groups, context: str) -> float:
    """从 scanList/scan 的 scan start time 取时间，秒与分钟统一为秒。"""
    scan = _scan_node(spectrum_elem, context)
    facts = collect_facts(scan, groups, context + " scan")
    time_fact = require_unique(facts, SCAN_START_TIME, context)
    if time_fact is None or time_fact.value is None:
        raise MzMLError(f"{context}: 缺少 scan start time (MS:1000016)")
    try:
        value = float(time_fact.value)
    except (TypeError, ValueError) as e:
        raise MzMLError(f"{context}: scan start time 不是数值: {time_fact.value!r}") from e
    if not math.isfinite(value) or value < 0.0:
        raise MzMLError(f"{context}: scan start time 须为非负有限值，实际 {value}")

    unit_acc = time_fact.unit_accession
    unit_name = (time_fact.unit_name or "").strip().lower()
    if unit_acc == cv.UNIT_SECOND or (not unit_acc and unit_name in cv._SECOND_NAMES):
        return value
    if unit_acc == cv.UNIT_MINUTE or (not unit_acc and unit_name in cv._MINUTE_NAMES):
        return value * 60.0
    raise MzMLError(
        f"{context}: scan start time 单位无法识别 "
        f"(unitAccession={unit_acc!r}, unitName={time_fact.unit_name!r})，只支持秒/分钟")


def _decode_binary_array(bda_elem, groups, context: str) -> np.ndarray:
    """解码单个 binaryDataArray：类型/精度/压缩全部按受控词汇辨认。"""
    facts = collect_facts(bda_elem, groups, context)

    # 未支持的编码：报出具体原因，绝不把未知字节当浮点数
    for acc, label in UNSUPPORTED_ENCODINGS.items():
        if acc in facts:
            raise MzMLError(f"{context}: 不支持的编码 {label}，本作业仅实现无压缩/zlib")

    precision = require_exclusive(facts, FLOAT_32, FLOAT_64, context + " 精度")
    if precision is None:
        raise MzMLError(f"{context}: 缺少 32 位 (MS:1000521) 或 64 位 (MS:1000523) "
                        "浮点精度声明")
    compression = require_exclusive(facts, NO_COMPRESSION, ZLIB_COMPRESSION,
                                    context + " 压缩")
    if compression is None:
        raise MzMLError(f"{context}: 缺少无压缩 (MS:1000576) 或 zlib (MS:1000574) 声明")

    binary_elems = bda_elem.findall(q("binary"))
    if len(binary_elems) != 1:
        raise MzMLError(f"{context}: binaryDataArray 须恰好一个 <binary>，"
                        f"实际 {len(binary_elems)} 个")
    raw_text = binary_elems[0].text or ""
    try:
        blob = base64.b64decode(raw_text, validate=True)
    except (ValueError, base64.binascii.Error) as e:
        raise MzMLError(f"{context}: base64 数据不合法: {e}") from e

    if compression == ZLIB_COMPRESSION:
        try:
            blob = zlib.decompress(blob)
        except zlib.error as e:
            raise MzMLError(f"{context}: zlib 解压失败: {e}") from e

    dtype = np.dtype("<f4") if precision == FLOAT_32 else np.dtype("<f8")
    n_declared = len(blob) // dtype.itemsize
    if len(blob) % dtype.itemsize != 0:
        raise MzMLError(
            f"{context}: 解码后 {len(blob)} 字节不是 {dtype.itemsize} 字节浮点的整数倍")
    try:
        arr = np.frombuffer(blob, dtype=dtype, count=n_declared).astype(np.float64,
                                                                         copy=True)
    except (ValueError, OverflowError) as e:
        raise MzMLError(f"{context}: IEEE 浮点解释失败: {e}") from e
    return arr


def _identify_arrays(bda_elems, groups, context: str, default_len: int):
    mz_arr = None
    int_arr = None
    for ordinal, bda in enumerate(bda_elems):
        facts = collect_facts(bda, groups, f"{context} binaryDataArray[{ordinal}]")
        kind = require_exclusive(facts, MZ_ARRAY, INTENSITY_ARRAY,
                                 f"{context} binaryDataArray[{ordinal}] 数组类型")
        if kind is None:
            raise MzMLError(
                f"{context} binaryDataArray[{ordinal}]: 缺少 m/z array (MS:1000514) "
                "或 intensity array (MS:1000515) 类型声明")
        if BINARY_TYPE not in facts:
            raise MzMLError(
                f"{context} binaryDataArray[{ordinal}]: 缺少 binary data (MS:1000524) 标记")
        arr = _decode_binary_array(bda, groups, f"{context} binaryDataArray[{ordinal}]")
        if arr.shape[0] != default_len:
            raise MzMLError(
                f"{context} binaryDataArray[{ordinal}]: 数组长度 {arr.shape[0]} "
                f"与 defaultArrayLength={default_len} 不符")
        if kind == MZ_ARRAY:
            if mz_arr is not None:
                raise MzMLError(f"{context}: 出现两个 m/z array，互斥声明冲突")
            mz_arr = arr
        else:
            if int_arr is not None:
                raise MzMLError(f"{context}: 出现两个 intensity array，互斥声明冲突")
            int_arr = arr
    return mz_arr, int_arr


def _validate_peaks(mz: np.ndarray, intensity: np.ndarray, context: str) -> None:
    if mz.shape != intensity.shape:
        raise MzMLError(f"{context}: m/z 与 intensity 数组长度不一致 "
                        f"({mz.shape[0]} vs {intensity.shape[0]})")
    if mz.size == 0:
        return  # 长度为 0 的合法扫描也有时间点
    if not np.isfinite(mz).all():
        raise MzMLError(f"{context}: m/z 含非有限值")
    if np.any(mz <= 0.0):
        raise MzMLError(f"{context}: m/z 须为正数")
    if not np.isfinite(intensity).all():
        raise MzMLError(f"{context}: intensity 含非有限值")
    if np.any(intensity < 0.0):
        raise MzMLError(f"{context}: intensity 须非负")
    if np.any(np.diff(mz) < 0.0):
        raise MzMLError(f"{context}: m/z 必须非递减")  # 重复 m/z 允许，峰都计入


def iter_scans(path):
    """增量解析 mzML，惰性产出 :class:`DecodedScan`。

    只产出参与分析的 centroid MS1（极性唯一）；MS2 跳过。任何结构/解码/校验
    问题抛 :class:`MzMLError`（含 spectrum 序号，便于定位失败扫描）。
    """
    path = str(path)
    try:
        context = ET.iterparse(path, events=("start", "end"))
        root = None
        spectrum_list = None
        groups: dict = {}
        spectrum_index = -1
        for event, elem in context:
            tag = elem.tag
            if not isinstance(tag, str) or not tag.startswith("{" + MZML_NS + "}"):
                # 非 mzML 命名空间内容（如 indexList 片段）不参与解析，结束即清理
                if event == "end":
                    elem.clear()
                continue
            if event == "start":
                if root is None:
                    # 普通 mzML 根为 mzML；带索引尾部的 indexed mzML 根为
                    # indexedmzML，其内部仍含一个标准 mzML 元素
                    if tag not in (q("mzML"), q("indexedmzML")):
                        raise MzMLError(
                            f"根元素不是 mzML/indexedmzML，实际 {elem.tag!r}")
                    root = elem
                elif tag == q("spectrumList"):
                    spectrum_list = elem
                continue

            # event == "end"
            if tag == q("referenceableParamGroupList"):
                groups = parse_referenceable_groups(root)
                elem.clear()
                continue

            if tag == q("spectrum"):
                spectrum_index += 1
                ctx = f"spectrum[{spectrum_index}]"
                try:
                    yield from _process_spectrum(elem, groups, spectrum_index, ctx)
                finally:
                    # 先摘出 DOM 再 clear：已处理 spectrum 节点及时释放，
                    # 内存不随扫描总数增长
                    if spectrum_list is not None:
                        spectrum_list.remove(elem)
                    elem.clear()
                continue

            # 顶层容器节点结束即可清理（定义类内容已在需要时缓存）
            if tag in (q("cvList"), q("fileDescription"), q("softwareList"),
                       q("instrumentConfigurationList"), q("dataProcessingList"),
                       q("chromatogramList")):
                elem.clear()
    except ET.ParseError as e:
        raise MzMLError(f"mzML XML 解析失败（残缺或不合法 XML）: {e}") from e
    except FileNotFoundError as e:
        raise MzMLError(f"mzML 文件不存在: {path}") from e
    except OSError as e:
        raise MzMLError(f"mzML 读取失败: {e}") from e


def _process_spectrum(elem, groups, spectrum_index: int, ctx: str):
    facts = collect_facts(elem, groups, ctx)

    level_fact = require_unique(facts, cv.MS_LEVEL, ctx)
    if level_fact is None or level_fact.value is None:
        raise MzMLError(f"{ctx}: 缺少 ms level (MS:1000511)")
    try:
        ms_level = int(float(level_fact.value))
    except (TypeError, ValueError) as e:
        raise MzMLError(f"{ctx}: ms level 不是整数: {level_fact.value!r}") from e

    if ms_level != 1:
        return  # MS2 及其它级别跳过（不要求其 centroid/profile 标记）

    is_centroid = require_flag(facts, CENTROID)
    is_profile = require_flag(facts, PROFILE)
    if is_centroid and is_profile:
        raise MzMLError(f"{ctx}: centroid 与 profile 标记互斥但同时出现")
    if is_profile:
        raise MzMLError(f"{ctx}: profile MS1 明确拒绝，本作业只分析 centroid MS1")
    if not is_centroid:
        raise MzMLError(
            f"{ctx}: MS1 缺少明确 centroid (MS:1000127) 标记，拒绝靠默认值猜测")

    # 极性标记在 mzML 规范中位于 scanList/scan，也允许直接写在 spectrum 上；
    # 两处合并后必须能确定唯一极性，互斥声明即输入失败
    scan_facts = collect_facts(_scan_node(elem, ctx), groups, ctx + " scan")
    merged_polarity: dict = {}
    for src in (facts, scan_facts):
        for acc in (POSITIVE_SCAN, NEGATIVE_SCAN):
            if acc in src:
                merged_polarity.setdefault(acc, []).extend(src[acc])
    if POSITIVE_SCAN in merged_polarity and NEGATIVE_SCAN in merged_polarity:
        raise MzMLError(f"{ctx}: 正/负极性互斥声明同时出现 "
                        "(MS:1000130 与 MS:1000129)")
    if POSITIVE_SCAN in merged_polarity:
        polarity_str = "positive"
    elif NEGATIVE_SCAN in merged_polarity:
        polarity_str = "negative"
    else:
        raise MzMLError(f"{ctx}: 参与分析的扫描必须确定唯一极性，"
                        "缺少 positive (MS:1000130) / negative (MS:1000129)")

    time_seconds = _scan_time_seconds(elem, groups, ctx)

    default_len_attr = elem.get("defaultArrayLength")
    if default_len_attr is None:
        raise MzMLError(f"{ctx}: spectrum 缺少 defaultArrayLength 属性")
    try:
        default_len = int(default_len_attr)
    except (TypeError, ValueError) as e:
        raise MzMLError(f"{ctx}: defaultArrayLength 不是整数: "
                        f"{default_len_attr!r}") from e
    if default_len < 0:
        raise MzMLError(f"{ctx}: defaultArrayLength 不可为负")

    bda_list = elem.find(q("binaryDataArrayList"))
    if bda_list is None:
        raise MzMLError(f"{ctx}: 缺少 binaryDataArrayList，无法取得 m/z 与 intensity 数组")
    bda_elems = bda_list.findall(q("binaryDataArray"))
    if len(bda_elems) < 2:
        raise MzMLError(f"{ctx}: 至少需要 m/z 与 intensity 两个 binaryDataArray，"
                        f"实际 {len(bda_elems)} 个")
    mz_arr, int_arr = _identify_arrays(bda_elems, groups, ctx, default_len)
    if mz_arr is None or int_arr is None:
        missing = "m/z array" if mz_arr is None else "intensity array"
        raise MzMLError(f"{ctx}: 缺少 {missing} (MS:1000514/MS:1000515)")
    _validate_peaks(mz_arr, int_arr, ctx)

    yield DecodedScan(
        index=spectrum_index,
        time_seconds=time_seconds,
        polarity=polarity_str,
        mz=mz_arr,
        intensity=int_arr,
        n_peaks=int(mz_arr.shape[0]),
        default_array_length=default_len,
    )
