"""样本表解析与产物落盘/读回。

样本表支持两种格式（按扩展名识别）：

- JSON：``{"samples": [{"sample": 名, "alignment": 路径, "library": 模式}, ...]}``
- TSV：表头 ``sample<TAB>alignment<TAB>library``，每行一个样本

产物目录（一次发布）::

    <out>/
      counts.csv             gene×sample 整数计数矩阵（含全部零计数基因）
      summary.csv            每样本互斥状态汇总（9 个去向 + ignored_only + 合计校验）
      fragments/<sample>.jsonl   逐片段：状态、原因、候选基因、判定依据的比对块
      run.json               运行参数、样本表快照、矩阵总和 == assigned 的校验结果
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import IGNORED_ONLY, LIBRARY_MODES, STATUSES
from .annotation import Annotation
from .counting import CountingError, count_sample


@dataclass(frozen=True)
class SampleSpec:
    sample: str
    alignment: str
    library: str
    base_dir: Path

    def abs_alignment(self) -> Path:
        p = Path(self.alignment)
        return p if p.is_absolute() else (self.base_dir / p).resolve()


def parse_sample_sheet(path) -> list[SampleSpec]:
    path = Path(path)
    if not path.is_file():
        raise CountingError(f"样本表不存在: {path}")
    if path.suffix.lower() == ".json":
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise CountingError(f"样本表 JSON 不合法: {e}") from e
        items = spec.get("samples") if isinstance(spec, dict) else None
        if not isinstance(items, list) or not items:
            raise CountingError("样本表 JSON 需要非空的 samples 数组")
        raw = items
    else:
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            required = {"sample", "alignment", "library"}
            if not reader.fieldnames or not required.issubset(reader.fieldnames):
                raise CountingError(
                    f"TSV 样本表必须含表头 {sorted(required)}，实际 {reader.fieldnames}")
            items = [row for row in reader]
        if not items:
            raise CountingError("TSV 样本表没有任何样本行")
        raw = items

    specs: list[SampleSpec] = []
    seen_names: set[str] = set()
    seen_files: set[str] = set()
    for i, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise CountingError(f"样本表第 {i} 项不是对象")
        name = item.get("sample")
        alignment = item.get("alignment")
        library = item.get("library")
        if not name or not isinstance(name, str):
            raise CountingError(f"样本表第 {i} 项缺少合法 sample 名")
        if not alignment or not isinstance(alignment, str):
            raise CountingError(f"样本 {name!r} 缺少 alignment 路径")
        if library not in LIBRARY_MODES:
            raise CountingError(
                f"样本 {name!r} 的 library={library!r} 非法，允许 {LIBRARY_MODES}")
        if name in seen_names:
            raise CountingError(f"样本名重复: {name!r}")
        abs_path = str((Path(alignment) if Path(alignment).is_absolute()
                        else (path.parent / alignment)).resolve())
        if abs_path in seen_files:
            raise CountingError(f"比对文件被多个样本共用: {alignment}")
        seen_names.add(name)
        seen_files.add(abs_path)
        specs.append(SampleSpec(name, alignment, library, path.parent))
    return specs


def _write_counts(matrix_path: Path, gene_ids: list[str],
                  samples: list[str], per_sample_counts: dict[str, dict[str, int]]) -> None:
    with open(matrix_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["gene_id", *samples])
        for gene_id in gene_ids:
            w.writerow([gene_id, *[per_sample_counts[s][gene_id] for s in samples]])


def _write_summary(summary_path: Path, samples: list[str],
                   per_sample_summary: dict[str, dict[str, int]]) -> None:
    cols = (*STATUSES, IGNORED_ONLY, "total_groups", "check", "matrix_sum")
    with open(summary_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sample", *cols])
        for sample in samples:
            s = per_sample_summary[sample]
            total = sum(s.values())
            partition_ok = total == sum(s[k] for k in (*STATUSES, IGNORED_ONLY))
            matrix_total = s["assigned"]  # 与矩阵总和的真实交叉校验在 run() 中完成
            w.writerow([sample, *[s[k] for k in STATUSES], s[IGNORED_ONLY], total,
                        "ok" if partition_ok else "BAD", matrix_total])


def read_counts(path) -> tuple[list[str], list[str], dict[str, dict[str, int]]]:
    """读回计数矩阵 -> (样本列, 基因行, {sample: {gene_id: count}})。"""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        samples = header[1:]
        matrix = {s: {} for s in samples}
        gene_ids = []
        for row in reader:
            gene_id = row[0]
            gene_ids.append(gene_id)
            for sample, value in zip(samples, row[1:]):
                matrix[sample][gene_id] = int(value)
    return samples, gene_ids, matrix


def read_fragment(out_dir, sample: str, qname: str) -> dict | None:
    """从逐片段 JSONL 中查某个 QNAME 为什么被排除/归属到哪里。"""
    path = Path(out_dir) / "fragments" / f"{sample}.jsonl"
    if not path.is_file():
        raise CountingError(f"逐片段文件不存在: {path}（样本名 {sample!r}）")
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row["qname"] == qname:
                return row
    return None


def run(gtf_path, sample_sheet_path, out_dir, *, min_mapq: int = 10) -> dict:
    """整批计数：解析注释与样本表 -> 逐样本流式计数 -> 暂存后整体发布。"""
    if min_mapq < 0:
        raise CountingError("--min-mapq 必须是非负整数")
    from .annotation import load_gtf
    annotation = load_gtf(gtf_path)
    specs = parse_sample_sheet(sample_sheet_path)
    for spec in specs:
        if not spec.abs_alignment().is_file():
            raise CountingError(f"样本 {spec.sample!r} 的比对文件不存在: {spec.alignment}")

    out_dir = Path(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = out_dir.with_name(out_dir.name + ".staging-" + uuid.uuid4().hex[:8])
    (staging / "fragments").mkdir(parents=True, exist_ok=False)

    gene_ids = annotation.gene_ids_sorted()
    samples = [s.sample for s in specs]
    per_sample_counts: dict[str, dict[str, int]] = {}
    per_sample_summary: dict[str, dict[str, int]] = {}
    sample_meta = []
    try:
        for spec in specs:
            counts, summary, rows = count_sample(
                spec.abs_alignment(), annotation, spec.library, min_mapq)
            per_sample_counts[spec.sample] = counts
            per_sample_summary[spec.sample] = summary
            frag_path = staging / "fragments" / f"{spec.sample}.jsonl"
            with open(frag_path, "w", encoding="utf-8") as fh:
                for res in rows:
                    fh.write(json.dumps(res.as_dict(), ensure_ascii=False) + "\n")
            matrix_sum = sum(counts.values())
            if matrix_sum != summary["assigned"]:  # 构造期不变量兜底
                raise CountingError(
                    f"内部错误: 样本 {spec.sample!r} 矩阵总和 {matrix_sum} != "
                    f"assigned {summary['assigned']}")
            sample_meta.append({
                "sample": spec.sample,
                "alignment": spec.alignment,
                "library": spec.library,
                "min_mapq": int(min_mapq),
                "n_qname_groups": sum(summary.values()),
                "matrix_sum": matrix_sum,
                "matrix_sum_equals_assigned": matrix_sum == summary["assigned"],
            })

        _write_counts(staging / "counts.csv", gene_ids, samples, per_sample_counts)
        _write_summary(staging / "summary.csv", samples, per_sample_summary)
        run_info = {
            "gtf": str(gtf_path),
            "sample_sheet": str(sample_sheet_path),
            "min_mapq": int(min_mapq),
            "statuses": list(STATUSES),
            "ignored_only": IGNORED_ONLY,
            "coordinate_convention": "内部与产物中的坐标均为 0-based 半开；GTF 源文件为 1-based 包含",
            "n_genes": len(gene_ids),
            "samples": sample_meta,
            "global_check": {
                "matrix_sum": sum(m["matrix_sum"] for m in sample_meta),
                "assigned_total": sum(per_sample_summary[s]["assigned"] for s in samples),
            },
        }
        run_info["global_check"]["ok"] = (
            run_info["global_check"]["matrix_sum"]
            == run_info["global_check"]["assigned_total"])
        (staging / "run.json").write_text(
            json.dumps(run_info, ensure_ascii=False, indent=2), encoding="utf-8")

        if out_dir.exists():
            prev = out_dir.with_name(out_dir.name + ".prev-" + uuid.uuid4().hex[:8])
            os.replace(out_dir, prev)
            shutil.rmtree(prev, ignore_errors=True)
        os.replace(staging, out_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return run_info
