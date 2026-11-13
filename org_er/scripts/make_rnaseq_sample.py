#!/usr/bin/env python3
"""生成 RNA-seq 计数演示用的**小型人工** GTF 与 SAM/BAM 夹具。

每个片段都针对一个容易算错的情形手工构造（不是从真实数据抽样），包括：
同基因重复/重叠外显子、双端共盖同一外显子、内含子里嵌套别的基因、删除跨过外显子、
同区域正反链基因、两基因共同命中、primary 旁夹 secondary/supplementary、缺端、
重复 first-mate、以及同时命中多个过滤原因的片段。

产物（data/rnaseq/）：
  demo.gtf               小坐标注释（含故意重复的 exon 行与嵌套基因）
  s1_unstranded.sam      全状态场景，unstranded
  s2_forward.sam         forward 文库的链筛选场景
  s3_reverse.sam         reverse 文库的链筛选场景
  s1_unstranded.bam      s1 的 BAM 副本（验证 BAM 读取路径）
  unsorted.sam           故意打乱 QNAME 顺序（用于失败用例）
  truncated.bam          截断的 BAM（用于读取失败用例）
  samples.json / samples.tsv
"""
from __future__ import annotations

import json
from pathlib import Path

import _bootstrap  # noqa: F401

ROOT = _bootstrap.ROOT
OUT = ROOT / "data" / "rnaseq"

# ----------------------------------------------------------------- GTF 注释
# 坐标均为 GTF 的 1-based 包含；内部转换后是 0-based 半开。
# gA + 链：两个转录本重复/重叠列出外显子，合并后为 [100,150) 与 [200,260)
# gB - 链：整个落在 gA 的内含子 [150,200) 里（剪接 N 不能把它算成覆盖）
# gC + 链：exon [300,340)+[380,420)，中间 [340,380) 是删除验证区
# gD - 链：exon [130,160)，只与 gA exon1 的尾部交叠（[100,130) 是 gA 独有区；
#          落在 [130,150) 的片段在 unstranded 下歧义，链模式下按方向区分）
# gE + 链：exon [420,480)，与 gF 共享 [460,480)（两基因共同命中）
# gF - 链：exon [460,520)
# gG + 链：exon [600,640)，没有任何片段命中（验证零计数基因保留）
GTF_LINES = [
    ("chr1", "gA", "+", 100, 150, "tA1"),
    ("chr1", "gA", "+", 100, 150, "tA1"),   # 完全重复的 exon 行
    ("chr1", "gA", "+", 120, 150, "tA2"),   # 与上一行重叠，合并后不变
    ("chr1", "gA", "+", 200, 260, "tA1"),
    ("chr1", "gA", "+", 210, 240, "tA2"),   # 完全包含在 [200,260) 内
    ("chr1", "gB", "-", 160, 190, "tB1"),   # 内含子 [150,200) 内的嵌套基因
    ("chr1", "gC", "+", 300, 340, "tC1"),
    ("chr1", "gC", "+", 380, 420, "tC1"),
    ("chr1", "gD", "-", 130, 160, "tD1"),   # 只与 gA exon1 尾部交叠、反链
    ("chr1", "gE", "+", 420, 480, "tE1"),
    ("chr1", "gF", "-", 460, 520, "tF1"),   # [460,480) 与 gE 共享
    ("chr1", "gG", "+", 600, 640, "tG1"),   # 零计数基因
    ("chr2", "gH", "+", 50, 90, "tH1"),     # 供 chimeric 使用
]

# --------------------------------------------------------------- SAM 小工具
PAIRED = 0x1
UNMAPPED = 0x4
MATE_UNMAPPED = 0x8
REVERSE = 0x10
FIRST = 0x40
LAST = 0x80
SECONDARY = 0x100
DUP = 0x400
SUPPLEMENTARY = 0x800


def sam(qname, flag, rname="*", pos1=0, mapq=0, cigar="*", rnext="*", pnext=0,
        seq_len=0, nh=None):
    """生成一行 SAM；SEQ 用定长 A 填充（长度须与 query-consuming 操作一致），QUAL 用 *。"""
    seq = "*" if seq_len == 0 else "A" * seq_len
    extra = [f"NH:i:{nh}"] if nh is not None else []
    return "\t".join([qname, str(flag), rname, str(pos1), str(mapq), cigar,
                      rnext, str(pnext), "0", seq, "*", *extra])


def header() -> str:
    return "\n".join([
        "@HD\tVN:1.6\tSO:queryname",
        "@SQ\tSN:chr1\tLN:1000",
        "@SQ\tSN:chr2\tLN:1000",
    ]) + "\n"


# ------------------------------------------------- s1：unstranded 全场景
def s1_records() -> list[str]:
    R = []
    # fragA：单端 10M 落在 gA exon1 [100,150)（0-based pos 100 -> POS 101）
    R.append(sam("fragA", 0, "chr1", 101, 30, "10M", seq_len=10))
    # fragB：双端完全重叠地覆盖 gA 独有区同一外显子，合并去重后只给 gA +1
    R.append(sam("fragB", PAIRED | FIRST, "chr1", 101, 30, "15M", "=", 101, seq_len=15))
    R.append(sam("fragB", PAIRED | LAST | REVERSE, "chr1", 101, 30, "15M", "=", 101, seq_len=15))
    # fragC：剪接比对 20M75N30M，N 跨过内含子 [125,200)；gB [160,190) 落在 N 中不得计数
    R.append(sam("fragC", 0, "chr1", 106, 30, "20M75N30M", seq_len=50))
    # fragD：删除 20M40D20M：覆盖 gC 两个外显子，D 区 [340,380) 不贡献
    R.append(sam("fragD", 0, "chr1", 321, 30, "20M40D20M", seq_len=40))
    # fragE：完全落在 gB（内含子中的嵌套基因），gA 不应被算上
    R.append(sam("fragE", 0, "chr1", 161, 30, "20M", seq_len=20))
    # fragF：覆盖 gA 与 gD 同区域 -> unstranded 下歧义（不投票、不分摊）
    R.append(sam("fragF", 0, "chr1", 131, 30, "20M", seq_len=20))
    # fragG：覆盖 gE/gF 共享区 -> 歧义
    R.append(sam("fragG", 0, "chr1", 461, 30, "20M", seq_len=20))
    # fragH：primary + secondary（256），secondary 忽略，primary 命中 gE
    R.append(sam("fragH", SECONDARY, "chr1", 461, 0, "20M", seq_len=20))
    R.append(sam("fragH", 0, "chr1", 431, 30, "20M", seq_len=20))
    # fragI：primary + supplementary（2048），supp 忽略，primary 命中 gF
    R.append(sam("fragI", 0, "chr1", 481, 30, "20M", seq_len=20))
    R.append(sam("fragI", SUPPLEMENTARY, "chr2", 51, 0, "10M", seq_len=10))
    # fragJ：只有 secondary（256），ignored_only
    R.append(sam("fragJ", SECONDARY, "chr1", 101, 0, "10M", seq_len=10))
    # fragK：缺端——只有 first-mate
    R.append(sam("fragK", PAIRED | FIRST, "chr1", 101, 30, "10M", "=", 200, seq_len=10))
    # fragL：重复 first-mate（两条 0x40，无 0x80）-> malformed
    R.append(sam("fragL", PAIRED | FIRST, "chr1", 101, 30, "10M", "=", 200, seq_len=10))
    R.append(sam("fragL", PAIRED | FIRST | REVERSE, "chr1", 221, 30, "10M", "=", 101, seq_len=10))
    # fragM：CIGAR 非法（未知操作 Q）-> malformed
    R.append(sam("fragM", 0, "chr1", 101, 30, "10Q", seq_len=10))
    # fragN：CIGAR 读段消耗与 SEQ 长度不一致 -> malformed
    R.append(sam("fragN", 0, "chr1", 101, 30, "8M", seq_len=10))
    # fragO：未比对 -> unmapped
    R.append(sam("fragO", UNMAPPED, "*", 0, 0, "*", seq_len=20))
    # fragP：duplicate 且同时 NH=3 且 MAPQ=0 -> 优先级 duplicate
    R.append(sam("fragP", DUP, "chr1", 101, 0, "10M", seq_len=10, nh=3))
    # fragQ：NH=2 -> multimapping
    R.append(sam("fragQ", 0, "chr1", 101, 30, "10M", seq_len=10, nh=2))
    # fragR：MAPQ=3 < 默认阈值 10 -> low_mapq
    R.append(sam("fragR", 0, "chr1", 101, 3, "10M", seq_len=10))
    # fragS：双端跨染色体 -> chimeric
    R.append(sam("fragS", PAIRED | FIRST, "chr1", 101, 30, "10M", "chr2", 60, seq_len=10))
    R.append(sam("fragS", PAIRED | LAST | REVERSE, "chr2", 61, 30, "10M", "chr1", 101, seq_len=10))
    # fragT：比对到基因间区 -> no_feature
    R.append(sam("fragT", 0, "chr1", 701, 30, "20M", seq_len=20))
    # fragU：软/硬裁剪 + 插入，真正覆盖仍是 gA 的 [100,110)
    # query-consuming: 3S+10M+2I+5S = 20，与 SEQ 长度一致；5H 不消耗任何位置
    R.append(sam("fragU", 0, "chr1", 101, 30, "5H3S10M2I5S", seq_len=20))
    # fragV：未声明配对却有两条 primary -> malformed
    R.append(sam("fragV", 0, "chr1", 101, 30, "10M", seq_len=10))
    R.append(sam("fragV", REVERSE, "chr1", 221, 30, "10M", seq_len=10))
    # fragW：双端且一条未比对 -> unmapped（结构本身合法）
    R.append(sam("fragW", PAIRED | FIRST | MATE_UNMAPPED, "chr1", 101, 30, "10M", "*", 0, seq_len=10))
    R.append(sam("fragW", PAIRED | LAST | UNMAPPED | REVERSE, "*", 0, 0, "*", "=", 101, seq_len=10))
    return R


# ------------------------------------------------- s2：forward 链筛选
def s2_records() -> list[str]:
    R = []
    # forward：只接受与片段方向同链的基因。
    # fragF1：正向读段在 gA(+)/gD(-) 交叠区 -> 同链只留 gA
    R.append(sam("fragF1", 0, "chr1", 131, 30, "20M", seq_len=20))
    # fragF2：反向读段在交叠区 -> 同链只留 gD（-），gA 被链筛掉
    R.append(sam("fragF2", REVERSE, "chr1", 131, 30, "20M", seq_len=20))
    # fragF3：反向读段落在 gA 独有区 [100,130)，同链基因只有 gA(+) 被筛掉 -> no_feature
    R.append(sam("fragF3", REVERSE, "chr1", 101, 30, "15M", seq_len=15))
    # fragF4：反向双端（first 反向），first 落在 gA 独有区 -> + 链基因被筛 -> no_feature
    R.append(sam("fragF4", PAIRED | FIRST | REVERSE, "chr1", 101, 30, "15M", "=", 221, seq_len=15))
    R.append(sam("fragF4", PAIRED | LAST, "chr1", 221, 30, "15M", "=", 101, seq_len=15))
    # fragF5：双端 first 正向，落在 gA/gD 交叠区 -> gA
    R.append(sam("fragF5", PAIRED | FIRST, "chr1", 131, 30, "20M", "=", 221, seq_len=20))
    R.append(sam("fragF5", PAIRED | LAST | REVERSE, "chr1", 221, 30, "15M", "=", 131, seq_len=15))
    # fragF6：NH=2 多比对 -> multimapping
    R.append(sam("fragF6", 0, "chr1", 101, 30, "10M", seq_len=10, nh=2))
    return R


# ------------------------------------------------- s3：reverse 链筛选
def s3_records() -> list[str]:
    R = []
    # reverse：只接受与片段方向相反链的基因。
    # fragR1：正向读段在 gA(+)/gD(-) 交叠区 -> 反链只留 gD
    R.append(sam("fragR1", 0, "chr1", 131, 30, "20M", seq_len=20))
    # fragR2：反向读段在交叠区 -> 反链只剩 gA(+)
    R.append(sam("fragR2", REVERSE, "chr1", 131, 30, "20M", seq_len=20))
    # fragR3：正向读段在 gF(-) 独有区 [480,520) -> 反链 gF
    R.append(sam("fragR3", 0, "chr1", 501, 30, "15M", seq_len=15))
    # fragR4：双端 first 正向，落在 gB(-) 区 -> 反链 gB
    R.append(sam("fragR4", PAIRED | FIRST, "chr1", 161, 30, "20M", "=", 221, seq_len=20))
    R.append(sam("fragR4", PAIRED | LAST | REVERSE, "chr1", 221, 30, "15M", "=", 161, seq_len=15))
    # fragR5：正向读段覆盖 gE(+)/gF(-) 共享区 [460,480) -> 反链只留 gF
    R.append(sam("fragR5", 0, "chr1", 461, 30, "20M", seq_len=20))
    # fragR6：反向读段落在 gE 独有区 [420,460) -> 反链只剩 gE(+)
    R.append(sam("fragR6", REVERSE, "chr1", 421, 30, "20M", seq_len=20))
    return R


def write_gtf(path: Path) -> None:
    lines = ["# 人工小注释：坐标为 1-based 包含；重复/重叠 exon 行用于验证并集逻辑"]
    for chrom, gene, strand, start, end, transcript in GTF_LINES:
        attrs = f'gene_id "{gene}"; transcript_id "{transcript}";'
        lines.append("\t".join([chrom, "demo", "exon", str(start), str(end),
                                ".", strand, ".", attrs]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_sam(path: Path, records: list[str]) -> None:
    path.write_text(header() + "\n".join(records) + "\n", encoding="utf-8")


def write_bam(sam_path: Path, bam_path: Path, skip_qnames=()) -> None:
    """把 SAM 转成 BAM。非法 CIGAR 的夹具记录 htslib 在解析阶段即拒绝，
    因此先按 QNAME 过滤文本行，再交给 pysam 解析；被跳过的记录仍保留在 SAM 中，
    另由 malformed_cigar.sam 专门覆盖文本路径。"""
    import pysam
    import tempfile
    skipped = set(skip_qnames)
    kept = []
    with open(sam_path, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("@") or not line.strip():
                kept.append(line)
                continue
            if line.split("\t", 1)[0] in skipped:
                continue
            kept.append(line)
    with tempfile.NamedTemporaryFile("w", suffix=".sam", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write("".join(kept))
        tmp_sam = Path(tmp.name)
    try:
        with pysam.AlignmentFile(str(tmp_sam), "r") as samin:
            with pysam.AlignmentFile(str(bam_path), "wb", header=samin.header) as out:
                for aln in samin.fetch(until_eof=True):
                    out.write(aln)
    finally:
        tmp_sam.unlink(missing_ok=True)
    # 读回确认文件完整
    with pysam.AlignmentFile(str(bam_path), "rb") as check:
        n = sum(1 for _ in check.fetch(until_eof=True))
    assert n > 0


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    write_gtf(OUT / "demo.gtf")
    write_sam(OUT / "s1_unstranded.sam", s1_records())
    write_sam(OUT / "s2_forward.sam", s2_records())
    write_sam(OUT / "s3_reverse.sam", s3_records())
    # BAM 无法承载故意非法的 CIGAR：跳过 fragM/fragN，二者保留在 SAM 与专用夹具里
    write_bam(OUT / "s1_unstranded.sam", OUT / "s1_unstranded.bam",
              skip_qnames=("fragM", "fragN"))

    # 专用坏 CIGAR 夹具（仅文本 SAM；pysam/htslib 在解析阶段就会拒绝这种记录）
    write_sam(OUT / "malformed_cigar.sam", [
        sam("fragM", 0, "chr1", 101, 30, "10Q", seq_len=10),
        sam("fragN", 0, "chr1", 101, 30, "8M", seq_len=10),
        sam("fragN2", 0, "chr1", 101, 30, "5M3S10M", seq_len=18),  # S 夹在中间
    ])

    # 故障夹具 1：QNAME 乱序（fragA 出现在 fragB 之后，字节序倒退）
    unsorted = s1_records()
    unsorted = [r for r in unsorted if not r.startswith("fragA\t")]
    unsorted.append(sam("fragA", 0, "chr1", 101, 30, "10M", seq_len=10))
    write_sam(OUT / "unsorted.sam", unsorted)

    # 故障夹具 2：截断 BAM（BGZF 流不完整，读取必须报错而非静默）
    data = (OUT / "s1_unstranded.bam").read_bytes()
    (OUT / "truncated.bam").write_bytes(data[: len(data) // 2])

    samples = {"samples": [
        {"sample": "s1", "alignment": "s1_unstranded.bam", "library": "unstranded"},
        {"sample": "s2", "alignment": "s2_forward.sam", "library": "forward"},
        {"sample": "s3", "alignment": "s3_reverse.sam", "library": "reverse"},
    ]}
    (OUT / "samples.json").write_text(
        json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    tsv = ["sample\talignment\tlibrary",
           "s1\ts1_unstranded.bam\tunstranded",
           "s2\ts2_forward.sam\tforward",
           "s3\ts3_reverse.sam\treverse"]
    (OUT / "samples.tsv").write_text("\n".join(tsv) + "\n", encoding="utf-8")
    print(f"夹具已写入 {OUT}")


if __name__ == "__main__":
    main()
