#!/usr/bin/env python3
"""内存缩放实测：扫描数增长、单谱规模不变时，峰值内存必须保持平稳。

生成两个 mzML（峰表/谱图字节规模每张相同，仅扫描总数不同：N 与 20N），
在**隔离子进程**中跑完整流式提取，用 /proc 采样真实峰值常驻内存（RSS），
再读回汇总核对面积与状态。期望峰值 RSS 基本不随扫描总数增长（允许解析器
缓冲与测量噪声带来的有限差异，设阈值 30%），而明细/输出文件应随扫描数线性
增长（证明输入规模确实变大）。
"""
from __future__ import annotations

import base64
import csv
import json
import os
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from org_er.lcms.job import run_batch  # noqa: E402

NS = "http://psi.hupo.org/ms/mzml"
WORK = ROOT / "output" / "lcms_memory"
N_SMALL = 200
N_LARGE = N_SMALL * 20          # 4000 张扫描
PEAKS_PER_SCAN = 200            # 单谱规模固定
SAMPLE_INTERVAL = 0.02          # 20ms 采样一次 RSS


def _cv(acc, name, value=None):
    v = f' value="{value}"' if value is not None else ""
    return f'<cvParam accession="{acc}" name="{name}" cvRef="MS"{v}/>'


def _blob(values, compress=True):
    raw = b"".join(struct.pack("<d", float(v)) for v in values)
    if compress:
        raw = zlib.compress(raw, 6)
    return base64.b64encode(raw).decode("ascii")


def write_scaling_mzml(path: Path, n_scans: int) -> None:
    """n_scans 张 centroid MS1，每张固定 PEAKS_PER_SCAN 个峰，t 每秒一张。"""
    mz_vals = [100.0 + 0.001 * k for k in range(PEAKS_PER_SCAN)]
    int_vals = [float(10 + (k % 7)) for k in range(PEAKS_PER_SCAN)]
    mz_blob = _blob(mz_vals)          # 每张谱峰表相同 -> 单谱规模固定
    int_blob = _blob(int_vals)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<mzML xmlns="{NS}" version="1.1.0"><cvList count="1">',
             _cv("MS:1000000", "PSI-MS"), '</cvList>',
             '<run id="r"><spectrumList count="' + str(n_scans) + '">']
    for i in range(n_scans):
        t = float(i)                  # 严格递增，间隔 1s
        parts.append(
            f'<spectrum index="{i}" id="scan={i}" '
            f'defaultArrayLength="{PEAKS_PER_SCAN}">'
            f'{_cv("MS:1000511", "ms level", 1)}'
            f'{_cv("MS:1000127", "centroid spectrum")}'
            f'{_cv("MS:1000130", "positive scan")}'
            f'<scanList count="1"><scan>'
            f'<cvParam accession="MS:1000016" name="scan start time" cvRef="MS" '
            f'value="{t}" unitAccession="UO:0000010" unitName="second" unitCvRef="UO"/>'
            f'</scan></scanList>'
            f'<binaryDataArrayList count="2">'
            f'<binaryDataArray>{_cv("MS:1000514", "m/z array")}'
            f'{_cv("MS:1000524", "binary data")}{_cv("MS:1000523", "64-bit float")}'
            f'{_cv("MS:1000574", "zlib compression")}<binary>{mz_blob}</binary>'
            f'</binaryDataArray>'
            f'<binaryDataArray>{_cv("MS:1000515", "intensity array")}'
            f'{_cv("MS:1000524", "binary data")}{_cv("MS:1000523", "64-bit float")}'
            f'{_cv("MS:1000574", "zlib compression")}<binary>{int_blob}</binary>'
            f'</binaryDataArray></binaryDataArrayList></spectrum>')
    parts.append('</spectrumList></run></mzML>')
    path.write_text("".join(parts), encoding="utf-8")


def run_extraction_subprocess(manifest, targets, out) -> int:
    code = (
        "import sys; sys.path.insert(0, %r);"
        "from org_er.lcms.job import run_batch;"
        "run_batch(%r, %r, %r, max_gap_seconds=1.0)"
        % (str(ROOT), str(manifest), str(targets), str(out)))
    proc = subprocess.Popen([sys.executable, "-c", code],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    peak_kb = 0
    status = Path("/proc") / str(proc.pid) / "status"
    while proc.poll() is None:
        try:
            for line in status.read_text().splitlines():
                if line.startswith("VmRSS:"):
                    peak_kb = max(peak_kb, int(line.split()[1]))
                    break
        except (FileNotFoundError, ProcessLookupError):
            break
        time.sleep(SAMPLE_INTERVAL)
    _, err = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"提取子进程失败: {err.decode()[:500]}")
    return peak_kb


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    files = {}
    for tag, n in (("small", N_SMALL), ("large", N_LARGE)):
        p = WORK / f"scale_{tag}.mzML"
        write_scaling_mzml(p, n)
        files[tag] = (p, n)

    # 目标模板：mz=100 窗只含每个扫描的第一个峰（强度 10）；每个 run 的窗收在
    # 自己的扫描时间范围内，相邻 1s 扫描（max_gap=1）整链相连 -> complete
    targets = WORK / "targets.json"
    targets.write_text(json.dumps({"targets": [
        {"target_id": "SCALE", "mz": 100.0, "ppm": 5.0, "polarity": "positive",
         "window_start_seconds": 0.0, "window_end_seconds": float(N_LARGE),
         "background": 0.0},
    ]}), encoding="utf-8")

    results = {}
    for tag, (p, n) in files.items():
        # 每 run 单独目标表，窗口终点 == 末扫描时间，保证完整覆盖
        tgt = WORK / f"targets_{tag}.json"
        tgt.write_text(json.dumps({"targets": [
            {"target_id": "SCALE", "mz": 100.0, "ppm": 5.0, "polarity": "positive",
             "window_start_seconds": 0.0, "window_end_seconds": float(n - 1),
             "background": 0.0},
        ]}), encoding="utf-8")
        manifest = WORK / f"manifest_{tag}.json"
        manifest.write_text(json.dumps({
            "batch_id": f"scale_{tag}",
            "samples": [{"sample_key": f"K_{tag}", "mzml_path": p.name}]}),
            encoding="utf-8")
        out = WORK / f"out_{tag}"
        size_mb = p.stat().st_size / 1e6
        peak_kb = run_extraction_subprocess(manifest, tgt, out)
        # 读回核对：每张扫描信号都应为 10（仅 mz=100.0 首峰命中）
        detail = out / "details" / f"K_{tag}.csv"
        with open(detail, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == n, f"{tag} 明细行数 {len(rows)} != 扫描数 {n}"
        assert all(float(r["signal"]) == 10.0 for r in rows), "信号核对失败"
        summary = list(csv.DictReader(open(out / "summary.csv", encoding="utf-8")))[0]
        assert summary["status"] == "complete", f"{tag} 应 complete，实际 {summary['status']}"
        results[tag] = {
            "n_scans": n, "file_mb": round(size_mb, 2),
            "peak_rss_mb": round(peak_kb / 1024, 1),
            "detail_rows": len(rows), "status": summary["status"],
            "area": float(summary["area"]),
        }

    small, large = results["small"], results["large"]
    ratio_scans = large["n_scans"] / small["n_scans"]
    ratio_size = large["file_mb"] / small["file_mb"]
    ratio_rss = large["peak_rss_mb"] / small["peak_rss_mb"]
    # 每谱面积贡献：信号 10 × 1s 梯形 ≈ 10/扫描（首末各半个区间）
    per_scan_area = large["area"] / large["n_scans"]

    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"\n扫描数倍数 = {ratio_scans:.0f}x，文件大小倍数 = {ratio_size:.1f}x，"
          f"峰值 RSS 倍数 = {ratio_rss:.2f}x")
    print(f"大文件每扫描面积贡献 = {per_scan_area:.4f}（期望 ≈10 强度·秒/扫描）")

    ok = True
    if ratio_size < 10.0:
        print(f"[BAD] 文件未随扫描数线性增长（{ratio_size:.1f}x），夹具未真正放大")
        ok = False
    if ratio_rss > 1.30:
        print(f"[BAD] 峰值 RSS 随扫描数增长 {ratio_rss:.2f}x（阈值 1.30x），"
              "内存没有保持流式平稳")
        ok = False
    else:
        print(f"[OK] 扫描数 {ratio_scans:.0f}x 而峰值 RSS 仅 {ratio_rss:.2f}x，"
              "内存不随扫描总数增长（流式释放生效）")
    if not (9.0 < per_scan_area < 11.0):
        print("[BAD] 面积/扫描核对失败")
        ok = False
    (WORK / "memory_report.json").write_text(
        json.dumps({"results": results, "ratio_scans": ratio_scans,
                    "ratio_file_size": ratio_size, "ratio_peak_rss": ratio_rss,
                    "passed": ok}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已写入 {WORK / 'memory_report.json'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
