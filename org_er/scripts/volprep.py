#!/usr/bin/env python3
"""离线体数据整备：把一批 .nii/.nii.gz 重采样到指定分析网格。

  python3 scripts/volprep.py --manifest data/volprep/batch.json --target data/volprep/target.json --out output/volprep

只做**已有坐标间**的重采样（目标体素中心 -> 世界毫米 -> 源连续下标），不估计配准。
不写原文件、不改动机构库与贡献作业的任何产物。
"""
import argparse
import json
import sys

import _bootstrap  # noqa: F401
from org_er.volprep.job import run_batch
from org_er.volprep.manifest import load_product


def main():
    ap = argparse.ArgumentParser(description="NIfTI 批次体整备到目标分析网格")
    ap.add_argument("--manifest", help="批次清单 JSON（items: path/sample_key/kind）")
    ap.add_argument("--target", help="目标网格 JSON（shape + 4×4 affine，毫米）")
    ap.add_argument("--out", default=str(_bootstrap.ROOT / "output" / "volprep"),
                    help="产物目录（默认 output/volprep）")
    ap.add_argument("--chunk-voxels", type=int, default=1 << 16,
                    help="每块目标体素上限（默认 65536）")
    ap.add_argument("--show", help="读回指定产物元数据（products/ 下的 <stem>.json），不需要原文件")
    args = ap.parse_args()

    if args.show:
        try:
            arr, mask, meta = load_product(args.show)
        except Exception as e:
            raise SystemExit(f"读回失败: {e}")
        print(f"{meta['sample_key']} [{meta['kind']}] frame {meta['frame_index']}/"
              f"{meta['frame_count']}  array={arr.shape} {arr.dtype}  有效={int(mask.sum())}"
              f"/{mask.size}")
        print(json.dumps(meta, ensure_ascii=False, indent=2))
        return 0
    if not args.manifest or not args.target:
        ap.error("批处理需要 --manifest 与 --target（只读回产物请用 --show）")

    try:
        index = run_batch(args.manifest, args.target, args.out,
                          chunk_voxels=args.chunk_voxels)
    except Exception as e:
        raise SystemExit(f"批次失败（上一次成功结果保持不动）: {e}")

    print(f"批次 {index['batch_id']}: 清单 {index['n_items_listed']} 项，"
          f"处理 {index['n_items_attempted']} 项，完成 {index['n_completed']} 项，"
          f"诊断 {index['n_diagnostics']} 项")
    print(f"目标网格 shape={index['target']['shape']}  块形状={index['samples'][0]['chunk_shape'] if index['samples'] else None}")
    for rec in index["samples"]:
        for f in rec["frames"]:
            print(f"  OK  {rec['sample_key']} [{rec['kind']}] frame {f['frame_index']}"
                  f" -> {f['stem']}  (有效体素 {f['valid_voxels']})")
    for d in index["diagnostics"]:
        where = f"item {d.get('item')}"
        print(f"  BAD {where} {d.get('path', '')}: {d.get('reason', '')}")
    print(f"产物目录: {args.out}（index.json / diagnostics.json / products/）")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
