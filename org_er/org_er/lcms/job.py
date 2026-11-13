"""LC-MS 提取批作业：清单 + 目标表 -> 逐样本面积汇总、扫描明细与诊断。

每个样本独立处理：mzML 流式解码 -> 扫描级目标信号 -> 分段线性积分，明细顺序
写磁盘，解析后及时释放 spectrum 节点与扫描数组。除参数定义和目标配置外只保留
每极性上一个扫描时间及各目标累计积分状态，不积存全部谱图或完整色谱。

样本在文件后段失败时不会留下成功状态：该样本不进汇总、半成品明细被删除，只
进 diagnostics；批内其他样本照常给出结果。产物先写暂存目录，整批结束才换入
目标目录，上一次成功结果保持原样。
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .errors import LcmsError
from .extract import PolaritiesState, extract_signal, targets_by_polarity
from .integral import StreamingIntegrator
from .mzml import iter_scans
from .targets import SampleSpec, Target, parse_manifest, parse_targets

DETAIL_HEADER = [
    "sample_key", "polarity", "spectrum_index", "time_seconds", "n_peaks",
    "target_id", "mz_window_low", "mz_window_high", "signal",
]
SUMMARY_HEADER = [
    "sample_key", "target_id", "polarity", "mz", "ppm",
    "window_start_seconds", "window_end_seconds", "background",
    "area", "covered_seconds", "window_seconds", "coverage_ratio",
    "status", "n_segments", "n_scans", "note",
]
SIGNAL_NOTE = ("信号积分（强度×秒，已减背景的非负部分），不是已校准的浓度；"
               "absent 表示窗口内完全没有可积分线段，area 留空，与真实零面积不同")


def _safe_key(key: str) -> str:
    out = "".join(c if (c.isalnum() or c in "._-") else "_" for c in key)
    return out or "sample"


@dataclass
class SampleResult:
    sample_key: str
    mzml_path: str
    status: str                       # ok / failed
    reason: str | None = None
    n_ms1_scans: int = 0
    n_ms1_by_polarity: dict = None
    rows: list[dict] = None


def process_sample(spec: SampleSpec, targets: list[Target], max_gap_seconds: float,
                   detail_path: Path) -> SampleResult:
    """处理单个样本；任何 LcmsError 向上抛，由批作业隔离诊断。"""
    by_pol = targets_by_polarity(targets)
    time_state = PolaritiesState()
    integrators = {t.target_id: StreamingIntegrator(t, max_gap_seconds)
                   for t in targets}
    n_ms1 = 0
    n_ms1_pol: dict[str, int] = {}

    detail_path.parent.mkdir(parents=True, exist_ok=True)
    with open(detail_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(DETAIL_HEADER)
        for scan in iter_scans(spec.abs_path()):
            # 同极性 MS1 时间必须严格递增（交错极性各自独立校验）
            time_state.check_advance(scan)
            n_ms1 += 1
            n_ms1_pol[scan.polarity] = n_ms1_pol.get(scan.polarity, 0) + 1
            same_pol_targets = by_pol.get(scan.polarity, ())
            for t in same_pol_targets:
                signal = extract_signal(scan.mz, scan.intensity, t)
                integrators[t.target_id].add_scan(scan.time_seconds, signal)
                writer.writerow([
                    spec.sample_key, scan.polarity, scan.index,
                    f"{scan.time_seconds:.12g}", scan.n_peaks, t.target_id,
                    f"{t.mz_low:.12g}", f"{t.mz_high:.12g}",
                    f"{signal:.12g}",
                ])
            # 扫描数组在本循环迭代结束即释放（局部变量下次迭代被替换）
        fh.flush()
        os.fsync(fh.fileno())

    rows = []
    for t in targets:
        st = integrators[t.target_id].finalize()
        status = st.status()
        rows.append({
            "sample_key": spec.sample_key,
            "target_id": t.target_id,
            "polarity": t.polarity,
            "mz": t.mz,
            "ppm": t.ppm,
            "window_start_seconds": t.window_start,
            "window_end_seconds": t.window_end,
            "background": t.background,
            "area": None if status == "absent" else st.area,
            "covered_seconds": st.covered_seconds,
            "window_seconds": st.window_seconds,
            "coverage_ratio": st.coverage_ratio(),
            "status": status,
            "n_segments": st.n_segments,
            "n_scans": st.n_scans,
            "note": SIGNAL_NOTE,
        })
    return SampleResult(spec.sample_key, spec.mzml_path, "ok",
                        n_ms1_scans=n_ms1, n_ms1_by_polarity=n_ms1_pol, rows=rows)


def _promote(staging: Path, out_dir: Path) -> None:
    prev = None
    if out_dir.exists():
        prev = out_dir.with_name(out_dir.name + ".prev-" + uuid.uuid4().hex[:8])
        os.replace(out_dir, prev)
    os.replace(staging, out_dir)
    if prev is not None:
        shutil.rmtree(prev, ignore_errors=True)


def run_batch(manifest_path, targets_path, out_dir, *, max_gap_seconds: float = 5.0) -> dict:
    """执行一整批提取；返回批次索引 dict（已落盘）。目标表/清单级错误直接抛。"""
    targets = parse_targets(targets_path)
    specs, batch_id = parse_manifest(manifest_path)
    if not math_isfinite(max_gap_seconds) or max_gap_seconds < 0:
        raise LcmsError(f"max_gap_seconds 须为非负有限值，实际 {max_gap_seconds}")

    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.with_name(out_dir.name + ".staging-" + uuid.uuid4().hex[:8])
    (staging / "details").mkdir(parents=True, exist_ok=False)

    completed: list[SampleResult] = []
    diagnostics: list[dict] = []
    try:
        for spec in specs:
            detail_path = staging / "details" / f"{_safe_key(spec.sample_key)}.csv"
            if not spec.abs_path().is_file():
                diagnostics.append({
                    "sample_key": spec.sample_key, "mzml_path": spec.mzml_path,
                    "status": "rejected", "stage": "read",
                    "reason": f"mzML 文件不存在: {spec.abs_path()}",
                })
                continue
            try:
                result = process_sample(spec, targets, max_gap_seconds, detail_path)
            except LcmsError as e:
                # 失败样本：删除半成品明细，绝不留下成功状态
                if detail_path.exists():
                    detail_path.unlink()
                diagnostics.append({
                    "sample_key": spec.sample_key, "mzml_path": spec.mzml_path,
                    "status": "rejected", "stage": "mzml",
                    "reason": str(e),
                })
                continue
            completed.append(result)

        _write_summary(staging, completed)
        index = {
            "batch_id": batch_id or Path(manifest_path).stem,
            "manifest": str(manifest_path),
            "targets_path": str(targets_path),
            "max_gap_seconds": float(max_gap_seconds),
            "n_samples_listed": len(specs),
            "n_completed": len(completed),
            "n_diagnostics": len(diagnostics),
            "targets": [_target_meta(t) for t in targets],
            "samples": [_sample_meta(r) for r in completed],
            "signal_note": SIGNAL_NOTE,
        }
        (staging / "index.json").write_text(
            json.dumps(index, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8")
        (staging / "diagnostics.json").write_text(
            json.dumps({"diagnostics": diagnostics}, ensure_ascii=False,
                       indent=2, allow_nan=False), encoding="utf-8")
        (staging / "manifest.copy.json").write_text(
            Path(manifest_path).read_text(encoding="utf-8"), encoding="utf-8")
        (staging / "targets.copy.json").write_text(
            Path(targets_path).read_text(encoding="utf-8"), encoding="utf-8")
        _promote(staging, out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return index


def _target_meta(t: Target) -> dict:
    return {
        "target_id": t.target_id, "mz": t.mz, "ppm": t.ppm,
        "polarity": t.polarity, "window_start_seconds": t.window_start,
        "window_end_seconds": t.window_end, "background": t.background,
        "mz_window_low": t.mz_low, "mz_window_high": t.mz_high,
    }


def _sample_meta(r: SampleResult) -> dict:
    return {
        "sample_key": r.sample_key, "mzml_path": r.mzml_path, "status": r.status,
        "n_ms1_scans": r.n_ms1_scans, "n_ms1_by_polarity": r.n_ms1_by_polarity,
        "detail_file": f"details/{_safe_key(r.sample_key)}.csv",
        "summary_rows": len(r.rows or []),
    }


def _write_summary(staging: Path, completed: list[SampleResult]) -> None:
    with open(staging / "summary.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(SUMMARY_HEADER)
        for r in completed:
            for d in r.rows:
                writer.writerow([
                    d["sample_key"], d["target_id"], d["polarity"],
                    _num(d["mz"]), _num(d["ppm"]),
                    _num(d["window_start_seconds"]), _num(d["window_end_seconds"]),
                    _num(d["background"]),
                    "" if d["area"] is None else _num(d["area"]),
                    _num(d["covered_seconds"]), _num(d["window_seconds"]),
                    _num(d["coverage_ratio"]), d["status"], d["n_segments"],
                    d["n_scans"], d["note"],
                ])


def _num(x) -> str:
    return f"{float(x):.12g}"


def math_isfinite(x) -> bool:
    import math
    return math.isfinite(x)


# ---------------------------------------------------------------- 读回
def read_index(out_dir) -> dict:
    path = Path(out_dir) / "index.json"
    return json.loads(path.read_text(encoding="utf-8"))


def read_summary(out_dir) -> list[dict]:
    path = Path(out_dir) / "summary.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def read_diagnostics(out_dir) -> list[dict]:
    path = Path(out_dir) / "diagnostics.json"
    return json.loads(path.read_text(encoding="utf-8"))["diagnostics"]


def read_detail(out_dir, sample_key: str) -> list[dict]:
    path = Path(out_dir) / "details" / f"{_safe_key(sample_key)}.csv"
    if not path.is_file():
        raise LcmsError(f"样本 {sample_key!r} 无明细文件（可能未成功完成）: {path}")
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def locate_scan(out_dir, sample_key: str, spectrum_index: int) -> list[dict]:
    """定位失败/指定扫描：从明细读回某 spectrum 序号对应的全部目标信号行。"""
    rows = read_detail(out_dir, sample_key)
    wanted = str(int(spectrum_index))
    return [r for r in rows if r["spectrum_index"] == wanted]
