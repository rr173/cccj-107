#!/usr/bin/env python3
"""独立标量参考：从已知峰表与分段直线重算期望，与生产输出逐项核对。

**不 import 生产的 mzML 解码或积分代码**：mzML 用标准库 xml/base64/zlib/struct
独立解码；目标信号用纯 Python 闭区间求和；面积按分段直线逐段标量计算
（含背景穿点交点的三角形）。参考值与夹具峰表在本文件内独立声明，是手算的
程序化版本。

用法:
  python3 scripts/run_lcms_reference_check.py [output/lcms]
"""
from __future__ import annotations

import base64
import csv
import json
import struct
import sys
import zlib
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "lcms"
NS = "{http://psi.hupo.org/ms/mzml}"
MAX_GAP = 15.0
TOL_AREA = 1e-6
TOL_TIME = 1e-9

# ------------------------------------------------ 独立的期望峰表（与夹具同源数据，
# 但这里是独立字面量，参考程序不读取生成器模块）
POS_EXPECTED = {
    10.0: [(100.0, 100.0), (100.0, 50.0), (200.0, 10.0)],
    20.0: [(99.9996, 9.0), (100.0005, 400.0)],
    30.0: [(100.0005001, 300.0)],
    60.0: [],
    90.0: [(100.0, 500.0)],
    100.0: [(100.0, 700.0), (100.0, 200.0)],
    110.0: [(100.0, 100.0)],
}
NEG_EXPECTED_SEC = {
    12.0: [(200.0, 300.0)],
    24.0: [(200.0002, 600.0)],
    36.0: [(200.0, 400.0)],
    48.0: [(200.0, 200.0), (500.0, 999.0)],
    60.0: [(200.0, 700.0)],
}
TARGETS = [
    ("T1", 100.0, 5.0, "positive", 15.0, 105.0, 200.0),
    ("T2", 200.0, 5.0, "negative", 10.0, 70.0, 400.0),
    ("T3", 300.0, 10.0, "positive", 10.0, 30.0, 0.0),
    ("T4", 100.0, 5.0, "positive", 35.0, 55.0, 0.0),
]


# ------------------------------------------------ 独立 mzML 解码（标准库）
def decode_independent(path: Path):
    """返回 [(spectrum_index, time_sec, polarity, [(mz,int),...])]，仅 centroid MS1。"""
    tree = ET.parse(path)
    root = tree.getroot()
    if root.tag != NS + "mzML":
        raise AssertionError(f"根元素异常: {root.tag}")
    groups = {}
    gl = root.find(NS + "referenceableParamGroupList")
    if gl is not None:
        for g in gl.findall(NS + "referenceableParamGroup"):
            groups[g.get("id")] = [
                (c.get("accession"), c.get("value"), c.get("unitAccession"))
                for c in g.findall(NS + "cvParam")]

    def facts_of(node):
        out = []
        for c in node.findall(NS + "cvParam"):
            out.append((c.get("accession"), c.get("value"), c.get("unitAccession")))
        for r in node.findall(NS + "referenceableParamGroupRef"):
            out.extend(groups[r.get("ref")])
        return out

    scans = []
    for idx, sp in enumerate(root.iter(NS + "spectrum")):
        sf = dict((a, v) for a, v, _ in facts_of(sp))
        if "MS:1000511" not in sf or int(float(sf["MS:1000511"])) != 1:
            continue
        assert "MS:1000127" in sf, f"spectrum[{idx}] 应有 centroid 标记"
        scan = sp.find(NS + "scanList/" + NS + "scan")
        scan_facts = facts_of(scan)
        pol = ("positive" if any(a == "MS:1000130" for a, _, _ in scan_facts)
               else "negative")
        t_acc = next(f for f in scan_facts if f[0] == "MS:1000016")
        tval = float(t_acc[1])
        tsec = tval * 60.0 if t_acc[2] == "UO:0000031" else tval

        arrays = {}
        for bda in sp.findall(NS + "binaryDataArrayList/" + NS + "binaryDataArray"):
            bf = [a for a, _, _ in facts_of(bda)]
            kind = "mz" if "MS:1000514" in bf else "int"
            compressed = "MS:1000574" in bf
            fmt = "f" if "MS:1000521" in bf else "d"
            blob = base64.b64decode(bda.find(NS + "binary").text or "")
            if compressed:
                blob = zlib.decompress(blob)
            sz = 4 if fmt == "f" else 8
            n = len(blob) // sz
            arrays[kind] = list(struct.unpack("<" + fmt * n, blob)) if n else []
        peaks = list(zip(arrays["mz"], arrays["int"]))
        scans.append((idx, tsec, pol, peaks))
    return scans


# ------------------------------------------------ 独立目标信号（纯 Python 标量）
def signal_at(peaks, mz0, ppm):
    lo = mz0 * (1 - ppm / 1e6)
    hi = mz0 * (1 + ppm / 1e6)
    total = 0.0
    for mz, intensity in peaks:
        if lo <= mz <= hi:          # 闭区间；重复 mz 都计入
            total += intensity
    return total


# ------------------------------------------------ 独立分段线性积分（标量手算）
def integrate(points, win_lo, win_hi, background, max_gap):
    """points: [(t, y), ...] 同极性严格递增。返回 (area, covered, n_segments)。"""
    area = 0.0
    covered = 0.0
    n_seg = 0
    for (t0, y0), (t1, y1) in zip(points, points[1:]):
        if t1 - t0 > max_gap:
            continue                                  # 长间隙不连线、不外推
        a = max(t0, win_lo)
        b = min(t1, win_hi)
        if b <= a:
            continue
        va = y0 + (y1 - y0) * (a - t0) / (t1 - t0)   # 窗端点处插值
        vb = y0 + (y1 - y0) * (b - t0) / (t1 - t0)
        fa, fb = va - background, vb - background
        if fa <= 0 and fb <= 0:
            piece = 0.0
        elif fa > 0 and fb > 0:
            piece = 0.5 * (fa + fb) * (b - a)        # 梯形
        else:
            tc = a + (b - a) * fa / (fa - fb)        # 背景穿点交点
            piece = 0.5 * fa * (tc - a) if fa > 0 else 0.5 * fb * (b - tc)
        area += piece
        covered += (b - a)
        n_seg += 1
    return area, covered, n_seg


def expected_for_sample():
    """返回 {sample: {target_id: (signal_series, area, covered, nseg, status)}}。"""
    out = {}
    for sample in ("S1", "S2"):
        by_pol = {"positive": sorted(POS_EXPECTED.items()),
                  "negative": sorted(NEG_EXPECTED_SEC.items())}
        res = {}
        for tid, mz0, ppm, pol, w0, w1, bg in TARGETS:
            series = [(t, signal_at(peaks, mz0, ppm))
                      for t, peaks in by_pol[pol]]
            area, covered, nseg = integrate(series, w0, w1, bg, MAX_GAP)
            win_len = w1 - w0
            if nseg == 0 or covered <= 0:
                status = "absent"
            elif covered + 1e-12 >= win_len:
                status = "complete"
            else:
                status = "partial"
            res[tid] = (series, area, covered, nseg, status, win_len)
        out[sample] = res
    return out


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "output" / "lcms"
    failures = []
    n_checks = 0

    # 1) 独立解码结果必须与夹具字面量峰表逐点一致（验证生产解码正确性的旁证）
    for sample, fname in (("S1", "good_a.mzML"), ("S2", "good_b.mzML")):
        scans = decode_independent(DATA / fname)
        assert len(scans) == 12, f"{sample} 应有 12 张 MS1，实际 {len(scans)}"
        pos = {t: p for _, t, pol, p in scans if pol == "positive"}
        neg = {t: p for _, t, pol, p in scans if pol == "negative"}
        for t, peaks in POS_EXPECTED.items():
            assert pos[t] == peaks, f"{sample} pos t={t} 解码峰表不符: {pos[t]} vs {peaks}"
        for t, peaks in NEG_EXPECTED_SEC.items():
            assert neg[t] == peaks, f"{sample} neg t={t} 解码峰表不符"
        # 两种极性各自的时间必须严格递增（交错极性各自独立）
        for pol_times in (sorted(pos), sorted(neg)):
            assert all(b > a for a, b in zip(pol_times, pol_times[1:]))
        n_checks += len(POS_EXPECTED) + len(NEG_EXPECTED_SEC)

    expected = expected_for_sample()

    # 1b) 手算锚点：直接核对几个关键扫描信号（闭区间边界、同质量多峰、窗外忽略）
    anchor = {
        ("T1", 10.0): 150.0,     # 同质量双峰 100+50
        ("T1", 20.0): 409.0,     # ppm 闭区间上界命中 + 99.9996 也在窗内
        ("T1", 30.0): 0.0,       # 100.0005001 略出上界
        ("T2", 24.0): 600.0,     # 分钟换算后 24s
        ("T2", 48.0): 200.0,     # 500 峰无目标，不计
        ("T3", 10.0): 0.0,
        ("T4", 90.0): 500.0,     # T4 与 T1 同 m/z/极性，信号相同
    }
    for sample in ("S1", "S2"):
        for (tid, tt), want in anchor.items():
            series = dict(expected[sample][tid][0])
            assert abs(series[tt] - want) < TOL_AREA, (
                f"{sample}/{tid} t={tt} 锚点信号 {series[tt]} != {want}")
            n_checks += 1

    # 2) 明细文件：每个扫描时刻的信号必须等于独立标量求和
    for sample in ("S1", "S2"):
        with open(out_dir / "details" / f"{sample}.csv", newline="", encoding="utf-8") as fh:
            detail = list(csv.DictReader(fh))
        for tid, mz0, ppm, pol, *_ in TARGETS:
            series = expected[sample][tid][0]
            got = {(float(r["time_seconds"])): float(r["signal"])
                   for r in detail if r["target_id"] == tid}
            for t, y in series:
                n_checks += 1
                if abs(got[t] - y) > TOL_AREA:
                    failures.append(f"{sample}/{tid} t={t}: 明细信号 {got[t]} != 参考 {y}")

    # 3) 汇总：面积/覆盖/段数/状态逐项核对
    # 3a) 完全手写的期望（独立于本文件的 integrate()，用纸笔分段直线算好）
    hand = {
        # T1：可积分线段 10-20-30 与 90-100-110（30s 长间隙断开），
        #     与窗 [15,105] 相交 5+10+10+5=30s
        "T1": (8755.247555012225, 30.0, "partial"),
        # T2：12..60 整链 48s 全在窗 [10,70] 内，背景 400 上下穿点
        "T2": (3080.0, 48.0, "partial"),
        # T3：窗 [10,30] 被 10-20、20-30 完整覆盖，信号恒 0 -> complete + area 0
        "T3": (0.0, 20.0, "complete"),
        # T4：窗 [35,55] 落在 30→60 长间隙中，没有任何线段 -> absent
        "T4": (None, 0.0, "absent"),
    }
    hand_segments = {"T1": 4, "T2": 4, "T3": 2, "T4": 0}
    with open(out_dir / "summary.csv", newline="", encoding="utf-8") as fh:
        summary = {(r["sample_key"], r["target_id"]): r for r in csv.DictReader(fh)}
    for sample in ("S1", "S2"):
        for tid, *_ in TARGETS:
            _, area, covered, nseg, status, win_len = expected[sample][tid]
            r = summary[(sample, tid)]
            n_checks += 4
            if r["status"] != status:
                failures.append(f"{sample}/{tid} 状态 {r['status']} != {status}")
            if status == "absent":
                if r["area"] != "":
                    failures.append(f"{sample}/{tid} absent 但 area 非空: {r['area']}")
            else:
                if abs(float(r["area"]) - area) > TOL_AREA:
                    failures.append(f"{sample}/{tid} 面积 {r['area']} != 参考 {area}")
            if abs(float(r["covered_seconds"]) - covered) > TOL_TIME:
                failures.append(
                    f"{sample}/{tid} 覆盖 {r['covered_seconds']} != 参考 {covered}")
            if int(r["n_segments"]) != nseg:
                failures.append(f"{sample}/{tid} 段数 {r['n_segments']} != 参考 {nseg}")

            # 与完全手写锚点核对（生产输出 + 参考 integrate 双重对照手算值）
            h_area, h_cov, h_status = hand[tid]
            n_checks += 3
            if r["status"] != h_status:
                failures.append(f"{sample}/{tid} 状态与手算不符: {r['status']} != {h_status}")
            if h_area is None:
                if r["area"] != "":
                    failures.append(f"{sample}/{tid} 手算 absent 但 area 非空")
            elif abs(float(r["area"]) - h_area) > TOL_AREA:
                failures.append(f"{sample}/{tid} 面积与手算不符: {r['area']} != {h_area}")
            if abs(float(r["covered_seconds"]) - h_cov) > TOL_TIME:
                failures.append(f"{sample}/{tid} 覆盖与手算不符")
            if int(r["n_segments"]) != hand_segments[tid]:
                failures.append(
                    f"{sample}/{tid} 段数与手算不符: {r['n_segments']} != {hand_segments[tid]}")
            # 参考程序自身的 integrate 也必须与手算一致（防参考实现一起错）
            if h_area is not None and abs(area - h_area) > TOL_AREA:
                failures.append(f"{sample}/{tid} 参考积分 {area} 与手算 {h_area} 不符")

    # 3b) 关键语义：真实零面积(complete) 与 absent 必须可区分
    t3 = summary[("S1", "T3")]
    t4 = summary[("S1", "T4")]
    n_checks += 3
    if not (t3["status"] == "complete" and float(t3["area"]) == 0.0
            and float(t3["coverage_ratio"]) == 1.0):
        failures.append(f"T3 应为 complete + area=0 + 覆盖率1，实际 {t3['status']}/"
                        f"{t3['area']}/{t3['coverage_ratio']}")
    if not (t4["status"] == "absent" and t4["area"] == ""
            and float(t4["coverage_ratio"]) == 0.0):
        failures.append(f"T4 应为 absent + area 留空 + 覆盖率0，实际 {t4['status']}/"
                        f"{t4['area']!r}/{t4['coverage_ratio']}")

    # 4) 诊断：9 个坏夹具必须全部失败，且原因具体、不冒充零信号
    diag = json.loads((out_dir / "diagnostics.json").read_text(encoding="utf-8"))
    diag_keys = {d["sample_key"] for d in diag["diagnostics"]}
    expected_bad = {
        "BAD_profile", "BAD_no_centroid", "BAD_length_mismatch", "BAD_duplicate_time",
        "BAD_bad_zlib", "BAD_missing_group", "BAD_truncated", "BAD_polarity_conflict",
        "BAD_numpress"}
    n_checks += len(expected_bad)
    if diag_keys != expected_bad:
        failures.append(f"诊断集合不符: 缺 {expected_bad - diag_keys}, 多 {diag_keys - expected_bad}")
    for d in diag["diagnostics"]:
        if not d["reason"] or len(d["reason"]) < 8:
            failures.append(f"{d['sample_key']} 诊断原因不具体")

    print(f"独立标量参考核对完成：{n_checks} 项检查")
    if failures:
        print("失败:")
        for f in failures:
            print("  -", f)
        return 1
    print("全部通过：解码峰表、逐扫描信号、分段面积/覆盖率/状态、9 类失败诊断均与独立参考一致")
    # 打印手算摘要，便于人工复核
    for sample in ("S1", "S2"):
        for tid, *_ in TARGETS:
            _, area, covered, nseg, status, win_len = expected[sample][tid]
            area_txt = "(空)" if status == "absent" else f"{area:.6f}"
            print(f"  {sample}/{tid}: area={area_txt} cover={covered:.3f}/{win_len}s "
                  f"segments={nseg} status={status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
