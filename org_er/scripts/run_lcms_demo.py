#!/usr/bin/env python3
"""LC-MS 目标离子提取端到端演示与回归（独立可运行，不依赖厂商软件）。

链路：生成微型 mzML 夹具 -> 流式提取（面积/覆盖率/状态 + 扫描明细）->
独立标量参考核对 -> 内存缩放实测 -> 读回与失败扫描定位。每一步有确定断言，
任一失败以非零状态结束。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(cmd, expect_zero=True):
    print(f"\n$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    if expect_zero and proc.returncode != 0:
        raise SystemExit(f"命令失败（退出码 {proc.returncode}）: {' '.join(cmd)}")
    return proc


def main():
    n_assert = 0

    # 1) 生成夹具（参数组引用、混合精度/压缩、分钟时间、交错极性、ppm 边界、同质量多峰）
    run([PY, "scripts/make_lcms_sample.py"])
    n_assert += 1

    # 2) 批处理提取：含 9 个坏夹具，预期退出码 4（有失败但正常样本完成）
    proc = run([PY, "scripts/lcms.py",
                "--manifest", "data/lcms/manifest.json",
                "--targets", "data/lcms/targets.json",
                "--out", "output/lcms", "--max-gap-seconds", "15"],
               expect_zero=False)
    assert proc.returncode == 4, f"有失败样本时应退出 4，实际 {proc.returncode}"
    n_assert += 1

    # 3) 全部样本都正常时退出码应为 0
    good_manifest = ROOT / "data/lcms/manifest_good_only.json"
    manifest = json.loads((ROOT / "data/lcms/manifest.json").read_text(encoding="utf-8"))
    manifest["samples"] = [s for s in manifest["samples"] if s["sample_key"] in ("S1", "S2")]
    good_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    proc = run([PY, "scripts/lcms.py",
                "--manifest", str(good_manifest),
                "--targets", "data/lcms/targets.json",
                "--out", "output/lcms_good", "--max-gap-seconds", "15"])
    assert proc.returncode == 0
    n_assert += 1

    # 4) 独立标量参考核对（不 import 生产解码/积分代码）
    run([PY, "scripts/run_lcms_reference_check.py", "output/lcms"])
    n_assert += 1

    # 5) 内存缩放实测（扫描数 20x，峰值 RSS 应平稳）
    run([PY, "scripts/run_lcms_memory_check.py"])
    report = json.loads((ROOT / "output/lcms_memory/memory_report.json").read_text())
    assert report["passed"] and report["ratio_peak_rss"] <= 1.30
    n_assert += 1

    # 6) 读回：summary 状态四态齐全且 absent 面积留空
    proc = run([PY, "scripts/lcms.py", "--out", "output/lcms", "--show"])
    assert "complete" in proc.stdout and "absent" in proc.stdout and "partial" in proc.stdout
    n_assert += 1

    # 7) 失败扫描定位：成功样本可定位 spectrum；坏样本明确无明细
    proc = run([PY, "scripts/lcms.py", "--out", "output/lcms",
                "--locate", "S1", "--spectrum", "2"])
    assert '"signal": "409"' in proc.stdout   # ppm 闭区间上界 + 99.9996 同窗
    proc = run([PY, "scripts/lcms.py", "--out", "output/lcms",
                "--locate", "BAD_bad_zlib", "--spectrum", "0"], expect_zero=False)
    assert proc.returncode != 0 and "无明细文件" in (proc.stderr or proc.stdout)
    n_assert += 2

    # 8) 诊断文件：9 类失败各有具体原因，且没有任何坏样本进入 summary
    diag = json.loads((ROOT / "output/lcms/diagnostics.json").read_text(encoding="utf-8"))
    assert len(diag["diagnostics"]) == 9
    assert all(len(d["reason"]) > 15 for d in diag["diagnostics"])
    summary_keys = set()
    import csv
    with open(ROOT / "output/lcms/summary.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            summary_keys.add(r["sample_key"])
    assert summary_keys == {"S1", "S2"}, summary_keys
    n_assert += 2

    # 9) 后段失败：最后一张谱损坏 -> 整样本隔离，不冒充零信号，同批正常样本保留
    src = (ROOT / "data/lcms/good_a.mzML").read_text(encoding="utf-8")
    last = src.rfind("<spectrum ")
    head, tail = src[:last], src[last:]
    bpos = tail.find("<binary>") + len("<binary>")
    (ROOT / "data/lcms/bad_late.mzML").write_text(
        head + tail[:bpos] + "!!!notbase64!!!" + tail[bpos:], encoding="utf-8")
    late_manifest = ROOT / "data/lcms/man_late.json"
    late_manifest.write_text(json.dumps({"samples": [
        {"sample_key": "GOOD", "mzml_path": "good_a.mzML"},
        {"sample_key": "LATEFAIL", "mzml_path": "bad_late.mzML"},
    ]}), encoding="utf-8")
    proc = run([PY, "scripts/lcms.py", "--manifest", str(late_manifest),
                "--targets", "data/lcms/targets.json",
                "--out", "output/lcms_late", "--max-gap-seconds", "15"],
               expect_zero=False)
    assert proc.returncode == 4
    late_diag = json.loads((ROOT / "output/lcms_late/diagnostics.json").read_text())
    assert any(d["sample_key"] == "LATEFAIL" and "spectrum[11]" in d["reason"]
               for d in late_diag["diagnostics"])
    with open(ROOT / "output/lcms_late/summary.csv", encoding="utf-8") as fh:
        late_keys = {r["sample_key"] for r in csv.DictReader(fh)}
    late_details = {p.name for p in (ROOT / "output/lcms_late/details").iterdir()}
    assert late_keys == {"GOOD"} and late_details == {"GOOD.csv"}
    n_assert += 1

    print(f"\nLC-MS 端到端演示全部通过（{n_assert} 组断言）。")
    print("产物: output/lcms/{summary.csv,details/*.csv,diagnostics.json,index.json}")
    print("       output/lcms_memory/memory_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
