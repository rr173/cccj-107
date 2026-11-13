#!/usr/bin/env python3
"""生成微型 mzML 测试夹具（仅标准库；与生产解码代码完全独立）。

覆盖：referenceableParamGroupRef 参数组引用、32/64 位两种 IEEE 精度、
无压缩/zlib 两种压缩（两个数组可不同）、分钟时间单位、交错极性、ppm 闭区间
边界、同质量多峰、非等距时间、穿背景交点、长间隙、合法空扫描；
另生成 profile / 长度错配 / 重复时间 / 坏 zlib 等失败夹具，确认失败不冒充零。

产物：data/lcms/
  good_a.mzML / good_b.mzML          两个正常样本（二进制编码组合不同）
  bad_*.mzML                         一组必须被拒绝的输入
  manifest.json / targets.json
"""
from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "lcms"

MS_NS = "http://psi.hupo.org/ms/mzml"


# ------------------------------------------------------------ 极简 mzML 写入器
def encode_array(values, precision: str, compression: str) -> str:
    """按 32/64 位 IEEE 小端浮点编码，再可选 zlib，最后 base64。"""
    fmt = "<f" if precision == "32" else "<d"
    raw = b"".join(struct.pack(fmt, float(v)) for v in values)
    if compression == "zlib":
        raw = zlib.compress(raw, level=6)
    return base64.b64encode(raw).decode("ascii")


def _cv(accession: str, name: str, value=None, unit_accession=None,
        unit_name=None) -> str:
    attrs = f'accession="{accession}" name="{name}" cvRef="MS" '
    if value is not None:
        attrs += f'value="{value}" '
    if unit_accession:
        attrs += (f'unitAccession="{unit_accession}" unitName="{unit_name}" '
                  f'unitCvRef="UO" ')
    return f"<cvParam {attrs}/>"


def binary_data_array(encoded: str, kind: str, precision: str,
                      compression: str, length: int) -> str:
    kind_acc = "MS:1000514" if kind == "mz" else "MS:1000515"
    kind_name = "m/z array" if kind == "mz" else "intensity array"
    prec_acc = "MS:1000521" if precision == "32" else "MS:1000523"
    prec_name = "32-bit float" if precision == "32" else "64-bit float"
    comp_acc = "MS:1000576" if compression == "none" else "MS:1000574"
    comp_name = "no compression" if compression == "none" else "zlib compression"
    return (
        f'<binaryDataArray encodedLength="{len(encoded)}" '
        f'binaryDataType="{prec_acc}">'
        f'{_cv(kind_acc, kind_name)}'
        f'{_cv("MS:1000524", "binary data")}'
        f'{_cv(prec_acc, prec_name)}'
        f'{_cv(comp_acc, comp_name)}'
        f'<binary>{encoded}</binary>'
        f'</binaryDataArray>')


def spectrum_xml(index: int, time_value: float, time_unit: str, polarity: str,
                 mz_values, int_values, *, centroid=True, profile=False,
                 default_length=None, mz_precision="64", int_precision="64",
                 mz_compression="none", int_compression="none",
                 use_group: bool = False, group_ref: str | None = None,
                 corrupt_zlib: bool = False, bad_length: bool = False,
                 no_centroid_flag: bool = False) -> str:
    """拼一张 spectrum；各种故障开关只供失败夹具使用。"""
    pol_cv = (_cv("MS:1000130", "positive scan") if polarity == "positive"
              else _cv("MS:1000129", "negative scan"))
    flags = []
    if centroid and not profile:
        flags.append(_cv("MS:1000127", "centroid spectrum"))
    if profile:
        flags.append(_cv("MS:1000128", "profile spectrum"))
    if no_centroid_flag:
        flags = []
    flags.append(_cv("MS:1000511", "ms level", value=1))
    if use_group and group_ref:
        flags = [f'<referenceableParamGroupRef ref="{group_ref}"/>'] + flags

    unit_acc = "UO:0000010" if time_unit == "s" else "UO:0000031"
    unit_name = "second" if time_unit == "s" else "minute"
    default_len = len(mz_values) if default_length is None else default_length
    if bad_length:
        default_len = len(mz_values) + 3

    mz_blob = encode_array(mz_values, mz_precision, mz_compression)
    int_blob = encode_array(int_values, int_precision, int_compression)
    if corrupt_zlib:
        # 故意破坏 zlib 字节（保留合法 base64）
        raw = bytearray(base64.b64decode(mz_blob))
        for i in range(min(6, len(raw))):
            raw[i] = (raw[i] ^ 0xFF)
        mz_blob = base64.b64encode(bytes(raw)).decode("ascii")

    return (
        f'<spectrum index="{index}" id="scan={index}" '
        f'defaultArrayLength="{default_len}">'
        + "".join(flags)
        + f'<scanList count="1">'
          f'<scan>'
          f'{_cv("MS:1000016", "scan start time", value=repr_(time_value), unit_accession=unit_acc, unit_name=unit_name)}'
          f'{pol_cv}'
          f'</scan></scanList>'
        f'<binaryDataArrayList count="2">'
        f'{binary_data_array(mz_blob, "mz", mz_precision, mz_compression, default_len)}'
        f'{binary_data_array(int_blob, "int", int_precision, int_compression, default_len)}'
        f'</binaryDataArrayList>'
        f'</spectrum>')


def repr_(x: float) -> str:
    return repr(float(x))


def mzml_document(spectra: list[str], param_groups: list[str] | None = None) -> str:
    group_list = ""
    if param_groups:
        group_list = (f'<referenceableParamGroupList count="{len(param_groups)}">'
                      + "".join(param_groups) + "</referenceableParamGroupList>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<mzML xmlns="{MS_NS}" version="1.1.0">'
        '<cvList count="2">'
        f'{_cv("MS:1000000", "Proteomics Standards Initiative Mass Spectrometry Ontology")}'
        f'{_cv("UO:0000000", "Units Ontology")}'
        '</cvList>'
        f'{group_list}'
        '<run id="run1" defaultInjectionTime="0">'
        f'<spectrumList count="{len(spectra)}" defaultDataProcessingRef="dp1">'
        + "".join(spectra)
        + '</spectrumList></run>'
        '<dataProcessingList count="1"><dataProcessing id="dp1">'
        f'{_cv("MS:1000544", "Conversion to mzML")}'
        '</dataProcessing></dataProcessingList>'
        '</mzML>')


def param_group_xml(group_id: str, accessions: list[tuple]) -> str:
    body = "".join(_cv(acc, name, value=val) for acc, name, val in accessions)
    return f'<referenceableParamGroup id="{group_id}">{body}</referenceableParamGroup>'


def write(path: Path, doc: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")


# ------------------------------------------------------------ 扫描峰表（能手算）
# 正离子扫描（秒）：t=10,20,30,60,90,100,110；30→60、60→90 是两段 30s 长间隙，
# t=60 为合法空扫描（0 仍参与连线）
POS_SCANS = [
    # (时间秒, [(mz, intensity), ...])；m/z 非递减，重复 m/z 允许
    (10.0, [(100.0000, 100.0), (100.0000, 50.0), (200.0, 10.0)]),  # T1=150 同质量双峰
    (20.0, [(99.9996, 9.0), (100.0005, 400.0)]),   # 100.0005 恰在 5ppm 闭区间上界
    (30.0, [(100.0005001, 300.0)]),                # 略出 5ppm 上界 -> T1 不命中=0
    (60.0, []),                                    # 合法空扫描
    (90.0, [(100.0, 500.0)]),
    (100.0, [(100.0, 700.0), (100.0000, 200.0)]),  # 同质量双峰都计入 -> T1=900
    (110.0, [(100.0, 100.0)]),
]
# 负离子扫描（分钟，验证单位换算）：0.2/0.4/0.6/0.8/1.0 min -> 12/24/36/48/60 s，
# 间隔均 12s（max_gap=15 时整链可连）
NEG_SCANS_MIN = [
    (0.2, [(200.0, 300.0)]),
    (0.4, [(200.0002, 600.0)]),                    # 微小 m/z 偏移仍在 5ppm 闭区间内
    (0.6, [(200.0, 400.0)]),                       # 恰等于背景 400
    (0.8, [(200.0, 200.0), (500.0, 999.0)]),       # 500 峰无目标匹配，须被忽略
    (1.0, [(200.0, 700.0)]),
]

TARGETS = [
    # T1：partial + 背景穿点三角形 + 长间隙断开；窗 [15,105] 长 90s
    {"target_id": "T1", "mz": 100.0, "ppm": 5.0, "polarity": "positive",
     "window_start_seconds": 15.0, "window_end_seconds": 105.0,
     "background": 200.0},
    # T2：负极性（分钟时间）partial，信号在背景上下往返穿点
    {"target_id": "T2", "mz": 200.0, "ppm": 5.0, "polarity": "negative",
     "window_start_seconds": 10.0, "window_end_seconds": 70.0,
     "background": 400.0},
    # T3：完整覆盖但信号恒为 0 -> complete 且 area=0（与 absent 严格区分）
    {"target_id": "T3", "mz": 300.0, "ppm": 10.0, "polarity": "positive",
     "window_start_seconds": 10.0, "window_end_seconds": 30.0,
     "background": 0.0},
    # T4：时间窗 [35,55] 完全落在 30→60 的长间隙里，没有任何线段 -> absent
    {"target_id": "T4", "mz": 100.0, "ppm": 5.0, "polarity": "positive",
     "window_start_seconds": 35.0, "window_end_seconds": 55.0,
     "background": 0.0},
]


def _interleave_spectra(group_mode: str, mz_prec, int_prec, mz_comp, int_comp,
                        **fault):
    """把正/负离子扫描按时间交错拼成 spectrum XML 列表。"""
    specs = []
    for t, peaks in POS_SCANS:
        specs.append((t, "s", "positive", peaks))
    for tmin, peaks in NEG_SCANS_MIN:
        specs.append((tmin * 60.0, "min", "negative", peaks))
    specs.sort(key=lambda x: x[0])  # 交错但同极性仍严格递增

    out = []
    for idx, (tsec, unit, pol, peaks) in enumerate(specs):
        mz_vals = [p[0] for p in peaks]
        int_vals = [p[1] for p in peaks]
        use_group = group_mode == "group" and pol == "positive"
        out.append(spectrum_xml(
            idx, tsec if unit == "s" else tsec / 60.0, unit, pol,
            mz_vals, int_vals,
            mz_precision=mz_prec, int_precision=int_prec,
            mz_compression=mz_comp, int_compression=int_comp,
            use_group=use_group, group_ref="pg_pos" if use_group else None,
            **fault))
    return out


def build_good(path: Path, *, group_mode: str, mz_prec, int_prec, mz_comp,
               int_comp) -> None:
    groups = None
    if group_mode == "group":
        # 参数组携带 centroid + ms level，正离子谱通过引用获得这两个事实
        groups = [param_group_xml("pg_pos", [
            ("MS:1000127", "centroid spectrum", None),
            ("MS:1000511", "ms level", 1),
        ])]
    spectra = _interleave_spectra(group_mode, mz_prec, int_prec, mz_comp, int_comp)
    write(path, mzml_document(spectra, groups))


def build_bad_files() -> dict:
    made = {}
    base = _interleave_spectra("inline", "64", "64", "none", "none")

    # 1) profile MS1：明确拒绝
    prof = [s.replace(
        _cv("MS:1000127", "centroid spectrum"),
        _cv("MS:1000128", "profile spectrum")) for s in base]
    p = DATA / "bad_profile.mzML"
    write(p, mzml_document(prof))
    made["profile"] = p

    # 2) 无 centroid/profile 标记：拒绝猜测
    noflag = _interleave_spectra("inline", "64", "64", "none", "none",
                                 no_centroid_flag=True)
    p = DATA / "bad_no_centroid.mzML"
    write(p, mzml_document(noflag))
    made["no_centroid"] = p

    # 3) 数组长度与 defaultArrayLength 不符
    badlen = _interleave_spectra("inline", "64", "64", "none", "none",
                                 bad_length=True)
    p = DATA / "bad_length_mismatch.mzML"
    write(p, mzml_document(badlen))
    made["length_mismatch"] = p

    # 4) 同极性重复时间（复制第一张正离子谱，时间相同）
    dup = list(base)
    dup.insert(1, base[0])
    p = DATA / "bad_duplicate_time.mzML"
    write(p, mzml_document(dup))
    made["duplicate_time"] = p

    # 5) 坏 zlib 数据
    badz = _interleave_spectra("inline", "64", "64", "zlib", "none",
                               corrupt_zlib=True)
    p = DATA / "bad_zlib.mzML"
    write(p, mzml_document(badz))
    made["bad_zlib"] = p

    # 6) 引用不存在的参数组
    missing = _interleave_spectra("inline", "64", "64", "none", "none")
    missing = [s.replace(
        '<cvParam accession="MS:1000127"',
        '<referenceableParamGroupRef ref="pg_ghost"/>'
        '<cvParam accession="MS:1000127"', 1) for s in missing[:1]] + missing[1:]
    p = DATA / "bad_missing_group.mzML"
    write(p, mzml_document(missing))
    made["missing_group"] = p

    # 7) 残缺 XML（截断文件尾）
    doc = mzml_document(base)
    p = DATA / "bad_truncated.mzML"
    write(p, doc[: len(doc) // 2])
    made["truncated"] = p

    # 8) 极性互斥声明（同一谱同时正/负）
    pol_bad = [s.replace(
        _cv("MS:1000130", "positive scan"),
        _cv("MS:1000130", "positive scan") + _cv("MS:1000129", "negative scan"))
        for s in base[:1]] + base[1:]
    p = DATA / "bad_polarity_conflict.mzML"
    write(p, mzml_document(pol_bad))
    made["polarity_conflict"] = p

    # 9) Numpress 不支持编码：报具体原因
    numpress = []
    for idx, s in enumerate(base):
        s2 = s.replace(
            _cv("MS:1000576", "no compression"),
            _cv("MS:1002312", "Numpress linear prediction compression"))
        numpress.append(s2)
    p = DATA / "bad_numpress.mzML"
    write(p, mzml_document(numpress))
    made["numpress"] = p
    return made


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    # good_a：参数组引用 + 混合精度 + 混合压缩
    build_good(DATA / "good_a.mzML", group_mode="group",
               mz_prec="64", int_prec="32", mz_comp="zlib", int_comp="none")
    # good_b：全部内联 cvParam + 另一套精度/压缩组合
    # m/z 用 64 位保证 ppm 边界可辨，强度用 32 位，仍覆盖两数组不同精度/压缩
    build_good(DATA / "good_b.mzML", group_mode="inline",
               mz_prec="64", int_prec="32", mz_comp="none", int_comp="zlib")
    bad = build_bad_files()

    manifest = {
        "batch_id": "lcms_demo",
        "samples": [
            {"sample_key": "S1", "mzml_path": "good_a.mzML"},
            {"sample_key": "S2", "mzml_path": "good_b.mzML"},
        ] + [{"sample_key": f"BAD_{k}", "mzml_path": v.name}
             for k, v in sorted(bad.items())],
    }
    (DATA / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA / "targets.json").write_text(
        json.dumps({"targets": TARGETS}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    # 同时给一份 TSV 目标表，验证多格式入口
    lines = ["target_id\tmz\tppm\tpolarity\twindow_start_seconds\t"
             "window_end_seconds\tbackground"]
    for t in TARGETS:
        lines.append("\t".join(str(t[k]) for k in (
            "target_id", "mz", "ppm", "polarity", "window_start_seconds",
            "window_end_seconds", "background")))
    (DATA / "targets.tsv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"夹具已写入 {DATA}")
    print(f"  正常样本: good_a.mzML (参数组+混合精度/压缩), good_b.mzML (内联)")
    print(f"  失败夹具 {len(bad)} 个: {', '.join(sorted(bad))}")
    print("  manifest.json / targets.json / targets.tsv")


if __name__ == "__main__":
    main()
