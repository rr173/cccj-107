# 机构实体解析数据集构建（org_er）

从多个机构目录（source）的原始条目出发，构建**可解释、能纠错**的机构实体解析数据集。
Python + DuckDB 实现，支持批量构建与增量修订，输出实体成员表、有效匹配边和解释证据。

## 数据模型

原始条目字段：`source, source_key, legal_name, country_code, website_host, registration_no`。

- **规范条目键** `record_key = norm(source)::norm(source_key)`（source 大小写不敏感）。
- 规范化是纯函数：Unicode NFKC、空白折叠、大小写/全半角处理；域名小写、去协议/路径/端口、
  IDNA 转 punycode、去 `www.` 前缀；注册号大写并按配置去分隔符。**原始值全部保留**在
  `records` 表的 `*_raw` 列。

## 配置（config/er_config.json）

所有判定规则均可配置且确定：

| 配置项 | 说明 |
|---|---|
| `registration_rules` | 按国家的合法注册号正则（作用于规范化值），含 `default` 兜底；每条规则带 `examples.valid/invalid` 样例，构建与验证时自动自检 |
| `placeholder_regnos` / `placeholder_patterns` | 占位值排除：精确列表（与条目走同一规范化）+ 正则模式（如 `^0+$`）；命中即非法 |
| `domain_evidence` | 免费邮箱域、共享托管域名单（精确或子域名命中即排除），命中域名的 `website_host` **不构成证据** |
| `similarity` | 弱边名称相似度：`token_jaccard`（默认，阈值 0.5）/ `token_dice` / `levenshtein_ratio` |
| `blocking.max_token_block_size` | 单国家 token 块大小上限（防巨块；不影响规则可接受边的完备性） |

## 匹配规则（确定）

- **强边**：同国家 且 双方注册号均合法 且 规范化注册号相同（score=1.0）。
- **弱边**：规范化域名相同、域名可作证据、名称相似度 ≥ 阈值（score=相似度）。
- 同一对同时命中时强边优先，证据取并集。

## 候选生成（禁止全配对）

三路阻塞均为 DuckDB 等值连接，取并集：

1. **国家 + 名称 token 倒排阻塞**（`name_tokens` 自连接）；
2. **合法注册号索引**（`country_code + registration_no`，仅合法值入索引）；
3. **域名索引**（`website_host`，仅可作证据的域名入索引）。

规则可接受边必然落在注册号或域名索引内，因此候选集是规则可接受边的超集，
数量远小于 N(N-1)/2（样本中 51 vs 2080，节省 97.55%）。穷举参考程序对此做了核验。

## 实体构建（确定）

实体 = 被接受匹配边的连通分量，约束：**同一分量中同一国家不能出现两个不同有效注册号**。

- 边考察顺序确定：先强边（规范端点键升序），再弱边（**分数降序、规范端点键升序**）；
- 违反约束的边进入**拒绝表**并保留原因（冲突国家与双方注册号）；已连通边记为冗余接受；
- **实体标识** = 分量最小规范条目键的 SHA1 摘要（`ent_xxxxxxxxxxxx`），与输入顺序无关，
  且成员不变时标识不变（跨轮次稳定）。

## 增量修订与等价性

修订事件（JSONL）：`{"op":"upsert","record":{...}}` / `{"op":"retract","source":...,"source_key":...}`。

- 事件作用于 `raw_records`（当前全部条目），随后**在全部当前条目上重新执行同一确定算法**，
  因此增量结果按构造即等价于全量重算，并由验证程序实际比对确认；
- 每轮重建后与上一轮成员比对，输出**实体迁移映射**（旧标识 → 新标识，标注
  `split / merge / split+merge / new / removed`），分裂与合并均可追踪。

## 交付表（output/*.csv）

| 表 | 内容 |
|---|---|
| `entity_members.csv` | 实体成员表：entity_id, record_key |
| `match_edges.csv` | 有效匹配边：类型、分数、是否冗余、结构化证据 JSON |
| `rejected_edges.csv` | 被约束拒绝的边及原因 |
| `candidates.csv` | 全部候选对：来源通道（token/regno/domain）、规则命中情况、未命中说明（解释证据） |
| `entity_migrations.csv` | 实体标识迁移映射（按轮次） |
| `run_stats.csv` | 每轮候选数、边数、实体数、节省率 |

## 入口

```bash
python3 scripts/make_sample_data.py            # 生成样本与三轮修订事件（data/）
python3 scripts/build.py                       # 批量构建 round 0
python3 scripts/revise.py --events data/revisions_round1.jsonl --round 1
python3 scripts/revise.py --events data/revisions_round2.jsonl --round 2
python3 scripts/revise.py --events data/revisions_round3.jsonl --round 3
python3 scripts/verify.py                      # 全部核验
python3 scripts/run_demo.py                    # 端到端演示（自建独立库，含全部场景与核验）
```

## 体数据整备（org_er.volprep）：NIfTI 批次 -> 指定分析网格

离线、只做**已有坐标间的重采样**（不估计配准变换）：把一批 `.nii/.nii.gz`
逐项映射到同一个目标三维网格，产出可直接读取的浮点/标签数组、有效掩码与几何元数据。

```bash
python3 -m pip install --user -r requirements-volprep.txt   # numpy + nibabel
python3 scripts/make_volprep_sample.py                       # 生成 data/volprep 小样本（可跳过）
python3 scripts/volprep.py --manifest data/volprep/batch.json \
                          --target   data/volprep/target.json \
                          --out      output/volprep \
                          [--chunk-voxels 65536] [--show <产物元数据路径>]
python3 scripts/run_volprep_demo.py      # 端到端演示（44 项断言全过）
python3 -m org_er.volprep.selfcheck      # 作业层自检（坏项/原子发布）
python3 -m org_er.volprep.reference_check  # 小数组逐点标量参考交叉核对
```

`data/volprep/` 带一个 3 项小样本（强度 `.nii.gz`、标签 `.nii`、4D 强度 `.nii`）与
目标网格 JSON，可直接跑上述命令；`output/volprep/` 是它的产物（可只读回）。

### 清单与目标网格

清单（`--manifest`）逐项给出相对清单目录的文件名、唯一 `sample_key` 与
`kind`（`intensity` / `label`）；同一样本可同时有强度与标签两项。四维文件按
第四维原序逐帧产出（帧号 0 起），第四维不当空间轴；只接受三维/四维，其余拒绝。

```json
{"batch_id": "demo",
 "items": [{"path": "raw/sub-01_T1.nii.gz", "sample_key": "sub-01", "kind": "intensity"},
           {"path": "raw/sub-01_seg.nii",   "sample_key": "sub-01", "kind": "label"}]}
```

目标网格（`--target`，JSON）给出三维 `shape` 与 4×4 `affine`；**体素整数下标指中心**，
世界坐标单位为毫米：

```json
{"shape": [16, 14, 12],
 "affine": [[1.5, 0, 0, -6], [0, 1.5, 0, -5], [0, 0, 1.5, -4], [0, 0, 0, 1]]}
```

### 几何选择与单位（拒绝即诊断，绝不默认单位矩阵）

- 优先**有效 sform**（`sform_code>0` 且矩阵有效），否则**有效 qform**；
  两者皆无、含非有限值、3×3 不可逆或末行非 `[0,0,0,1]` -> 拒绝该输入；
- 空间单位：`mm` 原样、`meter`×1000、`micron`×0.001，`unknown`/其它单位拒绝；
- 元数据记录**实际采用的几何**（sform/qform）、文件原值、单位系数、
  **归一化后的毫米矩阵**、轴方向描述与（若有）被拒 form 的原因。

### 重采样规则

目标体素中心 `i,j,k` -> 世界 `w = T @ [i,j,k,1]` -> 源连续下标 `s = inv(S) @ w`。

- **强度**：三线性；源值先应用头 `scl_slope/scl_inter`（`slope` 缺失/0/非有限 =
  无缩放）。中心落在闭区间 `[0,n-1]` 外 -> 无效（输出 NaN，掩码 0）；恰好边界上的
  退化维度不要求不存在的邻居；仅**非零权重**邻点参与，参与邻点含 NaN/Inf 时整点
  无效（不把缺失当 0、不重新归一化剩余权重）。
- **标签**：最近中心；恰好半格取较小下标；不线性插出新类别。未缩放标签须为非负
  整数且缩放恒等，否则拒绝。无效位置输出 0，与真实类别 0 用掩码区分。
- 允许负轴向、旋转、斜切、轴交换；**按 affine 映射**，不是按 voxel size 拉伸数组
  （演示用“只按 voxel size 拉伸”的对照，误差 9.04 vs 3.8e-6）。
- **按目标切块**（`--chunk-voxels` 控制每块目标体素上限，默认 65536）：只为一块目标
  体素生成坐标再插值，除源数组外不构造整卷/四维坐标云。

### 产物与读回（不需要原文件）

```
<out>/index.json               批次索引（目标几何、逐项/逐帧、诊断）
<out>/diagnostics.json         全部坏项及其原因（不冒充已完成样本）
<out>/manifest.copy.json       本批使用的清单快照
<out>/products/<stem>.npy      强度 float32 / 标签 int32
<out>/products/<stem>.mask.npy uint8 掩码（1 有效 / 0 无效）
<out>/products/<stem>.json     样本键、帧号、源几何（实际采用 + 归一化矩阵）、缩放、目标几何
```

```python
from org_er.volprep.job import read_batch, read_sample
idx = read_batch("output/volprep")                     # 不打开任何 NIfTI
for frame, arr, mask, meta in read_sample("output/volprep", "sub-01", "intensity"):
    ...                                                # 4D 逐帧、原序
```

元数据记录数组/掩码的相对文件名与 sha256，整目录可搬迁；读回会校验形状、掩码、
NaN 约定与内容摘要。批次产物先写暂存目录，整批成功后才换入目标目录；失败或中断时
上一次成功结果保持原样。

### 坏项与独立性

清单坏行（缺 `sample_key`、非法 `kind`、不安全键、重复的 `sample_key+kind`、非对象
条目）和读取/几何坏项逐条进入 `diagnostics`，不产生产物；同批正常项照常完成。
本管线只读输入文件，不改动机构库与贡献作业的任何产物，原有命令
（`scripts/build.py`、`verify.py`、`contrib.py` 等）继续可用。

## 论文署名贡献核算作业（org_er.contrib）

在既有归并结果之上核算论文目录的署名贡献，供研究管理人员核对年度产出。
**归并结果只作只读对照**：作业以 `READ_ONLY` 挂载机构库、只查指定轮次的
`entity_members`，绝不重新猜测机构是否相同，也不改动库中任何内容
（演示中以作业前后全表快照比对证实）。

```bash
python3 scripts/make_contrib_sample.py         # 生成论文目录小样本 data/papers.jsonl
python3 scripts/contrib.py \
    --er-db output/er.duckdb --round 3 \       # 归并结果所在库与轮次（运行时指定）
    --catalog data/papers.jsonl \              # 论文目录 JSONL
    --priority crossref,scopus,local_ir \      # 供应方优先级（高->低；未列入者并列最低）
    --out-db output/contrib.duckdb \           # 作业库（独立文件，不写机构库）
    --out output/contrib --verify              # 交付表目录 + 独立核验
python3 scripts/contrib.py --reuse-existing \
    --out-db output/contrib.duckdb --out output/contrib
                                               # 不重算目录：识别/核验已有账目并重新导出
                                               # （旧版 HUGEINT 小账目自动无损转换）
python3 scripts/run_contrib_demo.py            # 端到端演示（18 项断言全过）
python3 scripts/run_contrib_regression.py      # 故障链复现与回归（40 项断言全过）
```

一次核算的处理链路固定为：**精确计算 -> 暂存 -> 独立核验 -> 整套导出并回读核验 ->
单事务切换当前结果 -> 发布 CSV -> 清理**。重跑不会先摧毁上一次成功结果，细节见
“重跑、失败与恢复”。


### 目录行格式

```json
{"supplier": "crossref", "paper_id": "cr-0001", "doi": "https://doi.org/10.1000/XYZ.123",
 "year": 2023,
 "authors": [{"name": "Alice Zhang",
              "affils": [{"source": "companies_house", "entry": "GB-ACME-1"}]}]}
```

每行含供应方、供应方论文号、DOI、发表年份与**有序**作者列表；每位作者带若干
机构条目引用，引用 = 原目录的 `source`（来源）+ `entry`（条目号），按与机构管线
相同的规范条目键 `norm(source)::norm(entry)` 对照到归并结果。

### 去重与版本选取

- 同一 DOI（清除链接前缀、空白、大小写差异后）被多家重复收录时归并为一篇，
  按 `--priority` 选取**整份**作者名单，不跨名单拼作者；
- 不带 DOI 的论文按供应方 + 论文号独立保留，互不归并；
- 年份或作者顺序等冲突时仍按优先级选取，但逐版本留下差异
  （`year_differs / author_list_differs / author_order_differs / affil_differs`）与选取理由；
- 优先级打平：内容一致则按确定序取一（`tie_identical_content`）；
  内容不同则**整篇隔离**（`quarantine:tie_conflicting_content`），绝不按输入顺序挑一个；
- 坏行（空作者名单、非法年份、作者结构损坏、JSON 残缺）逐行说明原因，
  不算作正常论文。

### 贡献核算（精确有理数，任意精度整数）

每篇计入论文总贡献为 1：先均分给作者（`1/作者数`），作者份额再均分给他能定位到的
**不同**机构（同一机构多条署名先去重再分）。部分署名能定位时只在已定位机构间分配，
未定位引用保留待核查；署名全部未定位或无署名的作者，整份记入**待归属**。
隔离论文不计贡献。

核算在 Python 里用 `fractions.Fraction` 精确累计；落库的分子/分母列是 DuckDB
`BIGNUM`（任意精度十进制整数），**不会因年度累计超过 int64/int128 被截断、转成浮点
或拒绝**。展示用小数单独放在 `*_decimal_approx` 列，并带常量列
`total_decimal_note = approx_display_only` 明确标注“仅近似展示，精确值看分子/分母”；
精确值另有 `*_text`（如 `422113843906354093775418512493046577809/232862364358497360900063316880507363070`）
与整数分子/分母列，两者绝不混用。BIGNUM 经 Python 客户端返回十进制字符串，
核验与导出都按字符串->Python 大整数处理，绝不经过 float。

### 重跑、失败与恢复（不摧毁上一次成功结果）

- 每次运行先写**暂存 schema** `contrib_stage_<批次号>`，当前结果表在切换前完全不动；
- 暂存结果先做独立核验，再把整套 CSV 导出到 `<out>.staging-<批次号>`（与目标目录
  同父目录）并回读核验精确分子/分母；
- 以上全部通过后，才用**一个事务**把暂存表切换为当前表；随后发布 CSV 目录
  （目录级 rename，旧目录先退为 `<out>.prev-<批次号>`，新目录换入后才删除）；
- 状态表 `contrib_state` 记录 `running / committed / ready / failed`、批次号、
  上一成功批次、暂存 schema、临时 CSV 目录，不需要研究人员手工拼库或挑 CSV。

任何一步失败或进程终止后的行为：

| 中断位置 | 保住的完整结果 | 可辨认的未完成数据 | 重启后 |
|---|---|---|---|
| 写明细/核验失败 | **上一次成功批次**的库表与 CSV 都原样 | `contrib_state=failed` + `contrib_stage_<批次>` | 自动丢弃暂存 schema，保留旧批次，可再重跑 |
| 导出中途终止 | 同上（导出只写临时目录 `<out>.staging-<批次>`） | 状态 `running` + 临时目录 | 同上 |
| 已切换、CSV 未发布 | 库已是**新批次**，CSV 仍是**旧批次**（两份完整结果，不混合） | 状态 `committed` + 临时目录 | 自动完成目录切换并核验，新批次成为唯一当前结果 |

启动时 `recover_pending` 先执行上述收敛，再开始新批次。导出目录里的
`contrib_manifest.json` 记录批次号与“小数仅近似”的说明；CSV 与库同一批次才算
完整结果。

### 兼容旧版 HUGEINT 账目

旧版本把分子/分母存成 `HUGEINT`。`--reuse-existing` 会先识别列类型
（`legacy_hugeint / bignum_v2 / mixed / unknown`），对旧账目先执行同一套独立核验，
再用暂存/切换链路无损转换成 BIGNUM（逐分数以十进制字符串复制），补齐展示列并重新导出。
转换前对整个作业库文件做备份（`<out-db>.pre-migration-<时间>.bak`）；转换在事务中进行，
核验失败或转换出错时回滚、**原账目保持不动**，不会出现半新半旧。


### 交付表（output/contrib/*.csv）

| 表 | 内容 |
|---|---|
| `contrib_author_details.csv` | 逐位作者分摊明细：去重来源、作者位置、署名原样、去重后机构、未定位引用、每次除法依据；精确分数 `*_num/*_den` + `*_text`，展示小数单列 |
| `contrib_institution_year.csv` | 按机构、年份汇总的贡献量：精确分子/分母 + `total_text` + 近似展示列（`total_decimal_note` 标注） |
| `contrib_unassigned_year.csv` | 待归属按年汇总（同样区分精确值与近似展示） |
| `contrib_papers.csv` / `contrib_paper_versions.csv` | 论文去重结果、选取理由与全部版本差异 |
| `contrib_quarantine.csv` | 隔离论文及冲突版本 |
| `contrib_rejected_lines.csv` | 坏行及原因 |
| `contrib_manifest.json` | 本套 CSV 的批次号、格式版本与“小数仅近似”说明（整套导出完成的标志） |

### 独立核验（--verify / run_contrib_demo.py / run_contrib_regression.py）

不复用核算代码路径，从落库结果反推：每篇计入论文的机构份额 + 待归属份额恰好为 1；
作者份额 == 1/作者数且分摊结构一致；机构×年份汇总由明细逐行重算可逐项追到作者明细；
隔离论文零分摊；每年机构汇总 + 待归属 == 该年计入论文数；ok 行恰好进一组、坏行不进组。
另外核验：精确分数列是整数类型且已约分，展示列与精确列分离并标注；年度汇总在
Python 大整数中按明细重建（覆盖超过 int128 的分数）。

`run_contrib_regression.py` 实际执行并核对：素数 n=2..101 目录的 1/n 精确和跨 int128
完整保存与导出；另一份不同作者数、多机构分配目录的独立重算；旧 HUGEINT 账目的识别、
转换与损坏时原账目保留；一次成功后的失败重跑；写明细/导出/发布各中断窗口的重启恢复；
机构库与历史只读不变。演示另验证：机构库作业前后全表快照一致、目录乱序读入结果
签名一致、同一目录在不同轮次下的定位差异（轮次即只读对照的版本）。


## RNA-seq 片段计数（org_er.rnaseq）：GTF + 按 QNAME 排序的 SAM/BAM -> gene×sample 计数矩阵

在**不重新比对**的前提下，按外显子归属统计每个基因在各样本中的片段数。一条双端片段
合并两端覆盖后至多给一个基因 +1，绝不把一对 mate 算成两次表达证据。依赖仅 pysam
（`requirements-rnaseq.txt`）。

```bash
python3 -m pip install -r requirements-rnaseq.txt
python3 scripts/make_rnaseq_sample.py          # 生成小型人工 GTF/SAM/BAM 夹具（data/rnaseq/）
python3 scripts/rnaseq.py --gtf   data/rnaseq/demo.gtf \
                           --samples data/rnaseq/samples.json \
                           --out   output/rnaseq --min-mapq 10
python3 scripts/rnaseq.py --explain --out output/rnaseq --sample s1 --qname fragC   # 查某片段为何这样判
python3 scripts/run_rnaseq_demo.py             # 端到端演示与回归（118 项断言全过）
python3 scripts/run_rnaseq_reference_check.py data/rnaseq/demo.gtf \
                           data/rnaseq/samples.json output/rnaseq   # 独立逐碱基解释器交叉核对
python3 scripts/run_rnaseq_sparse_check.py     # 大坐标稀疏注释的存储实测
```

### 注释模型（0-based 半开，内部统一）

- GTF 坐标是 1-based 包含，读入即转成 0-based 半开（`start0=start1-1, end0=end1`）；
- 基因只由 `exon` 行的 `gene_id` 定义；同一基因的重复/重叠外显子（多转录本）先按
  染色体、链取**并集**；同一 `gene_id` 出现在不一致的染色体或链上 -> 注释整体报错；
- 基因之间允许外显子交叠：索引返回**全部**命中基因，绝不按注释行序选一个。

### CIGAR 决定覆盖（不把剪接间隙当覆盖）

`M/=/X` 贡献参考碱基；`D` 只推进参考不贡献；`N` 跨过内含子不贡献；`I/S` 只推进读段；
`H/P` 什么都不推进。操作串合法性与参考/读段消耗长度均被校验，非法记录归 `malformed`
并保留原因。因此坐落在内含子中的嵌套基因不会被一条剪接比对误计。

### 片段分组与互斥去向（固定优先级，每 QNAME 恰好一项）

同 QNAME 记录连续成组、组结束立即判定，记录流只保留当前组。secondary/supplementary
忽略并保留统计；全组无 primary 单列 `ignored_only`。剩余 primary 必须是单个未配对记录，
或恰好一条 first-mate + 一条 second-mate；缺端、重复端、配对标志矛盾归 `malformed`。
互斥去向按优先级判定：

```
malformed > unmapped > duplicate > multimapping(NH>1) > low_mapq > chimeric
          > no_feature > ambiguous > assigned
```

双端落在不同染色体为 `chimeric`；链模式 `unstranded/forward/reverse`：单端以自身方向、
双端以 first-mate 方向为准，forward 只收同链基因、reverse 只收反链基因。任一覆盖碱基
落入某基因外显子并集即纳入候选：空集 `no_feature`、多基因 `ambiguous`（不按长度投票、
不分摊小数）、唯一才 `assigned` +1。

### 产物（整目录一次发布，失败不冒充完成）

| 文件 | 内容 |
|---|---|
| `counts.csv` | gene×sample **整数**矩阵，注释中的零计数基因照样保留；矩阵总和 == assigned |
| `summary.csv` | 每样本 9 个互斥状态 + ignored_only + 合计分区校验 + matrix_sum |
| `fragments/<sample>.jsonl` | 逐片段：状态、原因、候选基因、归属基因、合并后的判定比对块、被忽略记录 |
| `run.json` | 运行参数、样本快照与“矩阵总和 == assigned”全局校验 |

### 故障即非零退出（不隐式重排、不出假矩阵）

- 输入未按 QNAME **字节序**非递减排列 -> 退出码 3，错误信息带两个 QNAME 与记录序号；
- BAM 无法打开/截断、记录结构损坏 -> 退出码 4，带文件与记录定位；
- 注释/样本表错误 -> 退出码 2。产物先写暂存目录，失败时上一次成功结果保持不动。

### 独立核验与存储特性

- `run_rnaseq_reference_check.py` **不 import 生产的区间树/CIGAR 代码**，自带正则 CIGAR
  解码并把小范围注释逐碱基展开成 Python set 重算，逐片段核对状态、候选、归属与矩阵；
- 注释索引是静态区间树，节点/负载只与外显子区间数同阶，与坐标绝对值和染色体长度无关：
  500 个 10^15 量级稀疏基因的完整索引实测约 680 KiB（约 1.4 KB/基因），逐碱基集合方案
  即使只展开外显子也要 24 MiB 以上，按整条染色体展开则完全不可行。

### 人工夹具覆盖的易错情形（全部在 118 项断言中）

同基因重复外显子、双端共盖同一外显子（重叠只算一次）、剪接间隙中嵌套别的基因、
删除跨过外显子、同区域正反链基因、两基因共同命中（歧义不分摊）、primary 旁夹
secondary/supplementary、只有 secondary 的组、缺端、重复 first-mate、非法 CIGAR
（未知操作/消耗不符/内部裁剪）、duplicate+multimapping+low_mapq 同时命中的优先级。


## 离线 LC-MS 目标离子提取（org_er.lcms）：mzML -> 目标离子色谱 -> 时间窗积分

不借助厂商软件、不做化合物自动鉴定：按标准命名空间与受控词汇（PSI-MS/UO）流式
解码 mzML，对每个目标取闭 m/z 窗内全部峰强度形成扫描信号，再在时间窗内做分段
线性积分，输出每个样本/目标的**信号**面积（强度×秒，非已校准浓度）、覆盖率与
complete/partial/absent 状态。依赖仅 numpy（`requirements-lcms.txt`）。

```bash
python3 -m pip install -r requirements-lcms.txt
python3 scripts/make_lcms_sample.py          # 生成微型 mzML 夹具（data/lcms/，可跳过）
python3 scripts/lcms.py --manifest data/lcms/manifest.json \
                        --targets  data/lcms/targets.json \
                        --out      output/lcms --max-gap-seconds 15
python3 scripts/lcms.py --out output/lcms --show                 # 读回批次汇总
python3 scripts/lcms.py --out output/lcms --locate S1 --spectrum 2   # 定位某扫描的目标信号
python3 scripts/run_lcms_demo.py             # 端到端演示（10 组断言全过）
python3 scripts/run_lcms_reference_check.py  # 独立标量参考交叉核对（不 import 生产解码/积分）
python3 scripts/run_lcms_memory_check.py     # 扫描数 20x 的峰值内存实测
```

### 输入（路径相对清单所在目录解析；JSON/CSV/TSV 均可）

- 清单：`sample_key` + `mzml_path`，样本键唯一；
- 目标表每项：唯一 `target_id`、正的 `mz` 与 `ppm`、`polarity`（positive/negative）、
  窗口起止秒数（窗长为正）、非负常数 `background`；所有目标值必须有限。

### mzML 解码（受控词汇驱动，不靠标签次序）

- 只按标准命名空间下 cvParam 的 **accession** 辨认事实；支持直接写在节点内的
  cvParam，也支持 `referenceableParamGroupRef`（可嵌套；引用不存在/成环即输入失败）；
- 同一事实互斥声明（如正负极性同时出现、两个 m/z array）直接拒绝；
- 只分析带**明确 centroid 标记的 MS1**：MS2 跳过，profile MS1 明确拒绝，无
  centroiding 标记的 MS1 拒绝靠默认值猜测；每条参与分析的扫描必须确定唯一极性；
- scan start time 的秒（UO:0000010）与分钟（UO:0000031）统一换算为秒；
- m/z 与 intensity 数组按类型访问号（MS:1000514/1000515）辨认，base64 后按无压缩
  （MS:1000576）或 zlib（MS:1000574）处理，再按声明的 32/64 位 IEEE 小端浮点
  （MS:1000521/1000523）解释；**两个数组可有不同精度与压缩**；数组长度必须与
  `defaultArrayLength` 相符，长度为 0 的合法扫描也保留时间点；
- Numpress 等未支持编码报出**具体原因**，绝不把未知字节当浮点数；坏 base64/zlib、
  残缺 XML、m/z 递减、强度为负/非有限都属于输入失败。

### 扫描信号与时间序列

- 对每个扫描和同极性目标，取闭区间 `mz×(1±ppm/1e6)` 内**所有**峰的强度之和；
  无命中是实际观测到的 0（不是缺测）；重复 m/z 的峰都计入；
- 两种极性扫描可交错，但各自参与分析的 MS1 时间必须**严格递增**：重复/回退时间
  直接失败，不静默排序或合并；
- 时间—强度明细逐扫描顺序写盘（`details/<sample>.csv`），可核对面积来自哪些扫描。

### 积分规则（不是扫描点直接相加）

- 同极性相邻扫描时间差 `<= max_gap_seconds` 才在二者间线性插值；更大间隙没有可
  积分信号，**禁止跨间隙拉直线，也绝不外推**；空数组扫描的 0 仍参与连线；
- 可用线段与目标时间窗取交集，窗端点落在扫描之间时只在对应线段内插值，不借窗外
  远处的点填缺口；
- 在交集上积分 `max(插值强度 - background, 0)`；线段穿过背景值时先求交点再积分
  剩余三角形，**不先把两端截零再做整个梯形**；
- 状态：窗口完整被线段覆盖为 `complete`；覆盖正时长但不完整为 `partial`（只报实际
  覆盖部分面积）；完全没有可积时长为 `absent`（面积留空）。真实覆盖而信号全 0 时
  面积为 0（如演示中的 T3，complete + area=0），与 absent 严格区分。

### 流式内存与失败隔离

iterparse 增量解析，每个 spectrum 处理完即从 DOM 摘除并释放，扫描数组用完即释放；
除参数定义与目标配置外只保留每极性上一个扫描时间与各目标累计积分状态，不积存
全部谱图或完整色谱。实测扫描数 20 倍（200→4000）、单谱规模不变时峰值 RSS 持平
（1.00x），明细/输出文件随扫描数线性增长。样本在文件后段失败时不产汇总行、半成品
明细被删除，只进 `diagnostics.json`；批内其他样本照常完成，批命令以非零状态
（退出码 4）提示有失败；产物先写暂存目录，整批结束才换入，上一次成功结果保持原样。

### 人工夹具与独立核对（run_lcms_reference_check.py，158 项检查）

夹具覆盖参数组引用、32/64 位混合精度、none/zlib 混合压缩、分钟时间、交错极性、
ppm 闭区间边界、同质量多峰、非等距时间、穿背景交点、长间隙与合法空扫描；另含 9 个
必拒夹具（profile、无 centroid、长度错配、重复时间、坏 zlib、缺失参数组、残缺 XML、
极性互斥、Numpress）。参考程序用标准库**独立解码**并从已知峰表/分段直线标量重算
期望，不 import 任何生产解码或积分函数；另附完全手写的面积/状态锚点，同时约束生产
输出与参考实现。


## 复杂抽样问卷估计（org_er.survey）：抽样设计 + 应答调整 + 边际校准 + 删一 PSU jackknife

面向“表里有未应答者、直接删行再平均会失真”的调查数据：从抽样设计、应答调整、
总体边际校准到方差估计走一条**固定、可追溯**的分析链，而不是单一加权平均函数。
只依赖标准库。

```bash
python3 scripts/make_survey_sample.py           # 生成小样本与失败场景（data/survey/）
python3 scripts/survey.py --data data/survey/survey.csv \
                          --design data/survey/design.json \
                          --out output/survey
python3 scripts/survey.py --show output/survey   # 读回估计表与诊断
python3 scripts/run_survey_demo.py               # 端到端演示（111 项检查全过）
python3 scripts/run_survey_reference_check.py output/survey
                                                 # 独立标量参考交叉核对（不 import 生产代码）
```

### 输入

数据 CSV 每行一名**被抽中**的人（含未应答者），列：

```
unit_id, stratum, psu, base_weight, response, adjustment_cell, domain, outcome
```

外加设计 JSON 中列出的若干**校准分类列**（如 `sex`、`age_band`）。

- `unit_id` 非空且唯一；`stratum` 抽样层、`psu` 层内共同抽取的初级单位、
  `adjustment_cell` 应答调整格、`domain` 分析子人群，均不得为空；
- `base_weight` 正有限数；`response` 只接受 0/1；`response=0` 不提供 outcome
  （可空），`response=1` 的 outcome 必须有限；
- **坏行整批失败并指出原行号**（身份重复同时给出首次出现行、设计列空、非法权重、
  response/outcome 不一致、缺校准列），绝不悄悄丢弃或补一个默认值。

设计 JSON：

```json
{"must_include_strata": ["CERT"],
 "calibration": {"columns": [
   {"name": "sex",      "categories": {"M": 450, "F": 550}},
   {"name": "age_band", "categories": {"young": 400, "mid": 350, "old": 250}}]},
 "max_iterations": 200, "tolerance": 1e-10}
```

- `must_include_strata` 是必入层（含单 PSU 必入层）；**未声明的单 PSU 层是设计非法**，
  不能把单 PSU 层的方差默认为 0；其余层至少 2 个 PSU；
- 校准列按配置顺序参与 IPF；每个类别显式给出已知总体人数；**各列人数之和必须一致**；
- `max_iterations` / `tolerance` 可配置，也可用 `--max-iterations` / `--tolerance` 覆盖。

### 权重链（全部人上执行，不先筛 domain）

1. **应答调整**：对每个 `adjustment_cell`，被抽中者（含未应答）基础权重和 ÷
   应答者基础权重和 = 该格应答者的调整倍数。某格没有应答者 -> 停止并解释，
   **禁止偷偷合并调整格**。
2. **边际校准（IPF）**：只在应答者上、按设计列顺序轮流缩放，每轮记录最大相对误差，
   直到所有边际相对误差 < 容差。权重保持为正。以下都**不得标成校准成功**：
   已知正控制格无样本支持、目标为 0 的格却有权重、零分母、边际互相矛盾（不收敛）、
   超过迭代上限。
3. **点估计**：`domain` 是分析子人群，**不是另一次抽样**——应答调整与校准始终在
   全部人上进行，再按 domain 取子集。均值的分母是该人群最终权重和；无应答者的
   子人群给出缺失估计与原因。

产物里每人都有**基础 / 应答调整后 / 校准后**三列权重，点估计可由这些权重重算。

### 方差（分层删一 PSU jackknife，每次复制重做整条链）

对一个**非必入层** h（m 个 PSU）的每个 PSU r：删去该 PSU 的所有人，同层其他 PSU
的基础权重乘 `m/(m-1)`，其他层不变；然后**从复制后的基础权重重新做应答调整与
边际校准**，再算估计。必入层不产生删除复制。

```
Var(θ) = Σ_h (m_h - 1)/m_h × Σ_{r∈h} (θ_r − θ_full)²
区间    = θ ± 1.96 × sqrt(Var)
```

禁止直接对全样本最终权重乘删除系数（演示中两种做法差到 0.2178，见 §4）。

复制中校准失败、调整格消失、目标人群分母变 0 时，记录**哪层哪 PSU 在哪个环节
失效**，该估计量的方差与区间留空，**不拿成功复制凑方差**；已完成的点估计与复制
诊断照常保留。没有非必入层可删时，方差同样留空（不把必入层方差当作 0）。

### 产物（`<out>/` 一次发布）

| 文件 | 内容 |
|---|---|
| `estimates.csv` | 全体/每个 domain 的加权人数、outcome 总量、均值 + 方差、SE、95% 区间、有效状态与原因 |
| `person_weights.csv` | 逐人基础 / 应答调整后 / 校准后权重（可重算点估计） |
| `adjustment_cells.csv` | 每格人数、权重和、倍数与状态 |
| `calibration_rounds.csv` / `calibration_margins.csv` | 每轮最大相对误差与逐类别误差；最终权重和 vs 控制量 |
| `strata_psus.csv` | 层 / PSU 清单、必入标记、是否产生复制 |
| `replicates.csv` / `replicate_estimates.csv` | 每次删除复制的状态/失败环节与全体/各 domain 估计 |
| `diagnostics.json` | 结构性诊断、复制失败明细、每个估计量的有效状态 |
| `index.json` | 输入路径与 sha256、结构摘要、校准摘要、可复核自检结果 |
| `design.copy.json` / `data.copy.csv` | 输入快照（数据只读复制，**不覆盖输入**） |

全样本链路失败时不发布到 `<out>`，写 `<out>.failed-<批次>/`（失败诊断 + 输入快照），
上一次成功结果原样保留。数据/设计非法退出码 2；全样本链路失败退出码 3；
有复制失败但点估计可算时退出码 0 并醒目标记。

### 人工夹具与核验

`data/survey/` 的主夹具故意让**同一 PSU 内的人相似**（outcome 以 PSU 中心值为主）、
**应答率随调整格变化**（11/13、8/11、7/11）、**校准分布与样本不同**（样本男性应答者
15/26，设计要求 M=450/F=550），含必入层 CERT 与无应答者子人群 `island`。

- `run_survey_reference_check.py` 不 import 任何生产模块，用标量循环独立实现整条
  权重链与每次复制，逐人核对权重、全体/子人群点估计、每个复制的估计与方差；
- `tiny.csv` 只有 6 人、1 层 2 PSU、单校准列，最终权重是精确分数（倍数 9/8、
  校准 16/15、删除 32/27），演示用 `Fraction` 精确手算核对全部权重、点估计、
  每次删除后的估计和方差；
- 演示还覆盖交换输入行序、重命名 PSU（估计与逐人权重不变）、失败场景各自保留
  什么/不能算什么、坏数据逐行定位、输入文件未被改写。

## 演示场景（run_demo.py，19 项断言全过）

1. **S1 跨来源同机构合并**：GB（强边+弱边双证据，强边优先）、CN（全角注册号经 NFKC 规范化后相同）、DE；
2. **S2 共享域名不误并**：`gmail.com`（免费邮箱）、`wordpress.com`（共享托管）被排除，
   名称相似甚至相同也不产生边，候选说明中保留 `host_not_evidence(...)` 解释；
3. **S3 相似名称但注册号冲突**：Globex 弱边（域名相同、相似度 1.0）因 `regno_conflict` 被拒；
4. **S4 桥接边撤回导致分裂**：撤回桥接条目后 Nimbus 分量一分为二，迁移映射输出 split/removed；
5. **S5 注册号修订引发候选变化**：占位值 `N/A` 修订为合法值后进入注册号索引
   （候选对 3→4），产生新强边并合并；
6. **S6 输入顺序打乱仍稳定**：3 个随机种子重放，结果签名完全一致。

## 核验（verify.py）

1. 配置样例自检（合法/非法/占位值）；
2. **小规模穷举参考程序**：对全部条目两两评估规则，核验候选不漏掉任何规则可接受的边，
   且管线判定（边类型、分数）与穷举完全一致；
3. 约束不变量：同实体同国家至多一个有效注册号；
4. 乱序稳定性；
5. **多轮修订后的增量结果 == 在全部当前条目上全量重算**。

## 候选节省在哪

以 round 0 为例：65 条目，全配对 2080，实际候选 51（节省 97.55%）——

1. 候选只来自三个等值索引，不做两两比较；
2. token 阻塞限定同国家，跨国条目互不产生候选；
3. 注册号索引只含合法值（占位值、非法格式被排除），域名索引只含可作证据的域名
   （免费邮箱、共享托管域被排除），无效键不产生候选；
4. 规则可接受边必然落在注册号或域名索引内（穷举核验通过），token 通道提供近似命中审计，
   因此候选是规则可接受边的紧致超集。规模越大、键越稀疏，节省越显著（全配对 O(N²)，
   阻塞候选约 O(Σ块大小²)）。

## 稳态通量分析（org_er.flux）：FBA 最大生物量 + 近最优 FVA 反应可变范围

只有反应网络与摄取上限、没有动力学参数时，用线性规划求稳态通量。LP 全部交给
SciPy HiGHS（`linprog(method="highs")`），本作业不实现求解器、不引入热力学假设、
不自动消除内部循环（type III pathway 会如实保留为无界循环）。依赖见
`requirements-flux.txt`（scipy + numpy）。

```bash
python3 -m pip install -r requirements-flux.txt
python3 scripts/make_flux_sample.py        # 生成三套微型网络与条件（data/flux/）
python3 scripts/flux.py --network data/flux/demo_network.json \
                        --conditions data/flux/demo_conditions.json \
                        --out output/flux --retain-fraction 1.0 [--tolerance 1e-7]
python3 scripts/flux.py --show output/flux                                  # 读回
python3 scripts/run_flux_demo.py                  # 端到端演示（22 项断言全过）
python3 scripts/run_flux_reference_check.py       # 手算+独立 LP+见证逐项核验（2182 项）
```

### 输入纪律（严格校验，报错带条件 id 与数组位置）

- `metabolites`：唯一 `id` + 布尔 `boundary`；边界物质不进守恒方程，
  内部物质必须满足 S·v=0。
- `reactions`：唯一 `id`、以代谢物 id 为键的 `stoichiometry`（负=消耗、正=生成，
  系数须非零有限，禁止引用不存在的物质）、`lower`/`upper`（`null`=该方向无界，
  其余有限且 lower≤upper）。**可逆反应用负下界表达，绝不先取绝对值或拆反应**。
- `biomass_reaction` 必须存在且下界非负，目标是最大化其净通量。
- 条件文件每个条件给 `replacements`（替换上下界）与 `knockouts`（固定为 0）；
  同一反应不许同时替换又敲除；未知反应、重复条件 id 在求解前报清楚位置。
  条件之间互不继承，每个都从原网络不可变快照重新派生上下界（不改原网络）。
  替换允许 `lower>upper`——那是“故意不可行”实验，由 LP 可行性判定，不当输入错误。

### 分析链（FBA → 近最优 FVA，两端点各自独立求解）

1. **FBA**：在 S·v=0 与有效上下界下最大化生物量，记录状态
   `optimal / infeasible / unbounded / numerical_failure` 与一组最优通量见证；
2. **FVA**：仅当目标有有限最优值 z（**含 z=0 但系统可行**）时，在同一可行域加
   `v_biomass ≥ retain_fraction × z`，对每条反应分别独立做 min/max 两个 LP。
   端点绝不由 FBA 的那组解推导，也不把不同反应的端点拼成一组“可行通量”；
3. **分类**（按判零容差）：`blocked`（两端都在容差内）、`forward_only`、
   `reverse_only`、`reversible`（能正能负）、`unbounded_min/max/both`、
   `unavailable`（端点数值失败，绝不混作 blocked）。

### 状态与数值严格分离（不拿 0 掩盖问题）

- 条件不可行、目标无界、数值失败都不填 0：不可行不做 FVA；**目标无界时不做依赖
  有限 z 的 FVA，也不给无界反应偷加大上限**；
- 有限目标下单个反应端点仍可能无界：端点状态与方向（min/max）分别记录，
  其他已算出的反应不丢失；
- 每个有限解都保存**完整见证通量**（FBA 一个 JSON、FVA 每端点一行 JSONL），
  独立代回守恒、上下界与生物量保留约束，记录最大残差；超声明容差的解不标有效，
  该端点改记 `unavailable`；
- 输入错误退出码 2；求解器数值失败/见证超容差退出码 3，且该批次不发布为正式
  结果（诊断进 `.failed`，上一次成功产物原样保留）；条件级不可行/无界只醒目警告，
  其他合法条件照常可读回，命令退出码仍为 0。

### 产物（`<out>/` 一次原子发布，重跑不摧毁上一次结果）

| 文件 | 内容 |
|---|---|
| `index.json` | 输入路径与 sha256、实际 retain_fraction/tolerance、网络与条件快照、逐条件结果 |
| `condition_summary.csv` | 每条件 FBA 状态、z（不可行/无界留空）、是否做 FVA、见证计数、警告 |
| `fva_ranges.csv` | 逐条件逐反应的 min/max 端点值与**端点状态**、类别、保留阈值、求解器消息 |
| `conditions/<id>/condition.json` | 该条件实际应用的替换/敲除（含原始界与生效界）、FBA 状态 |
| `conditions/<id>/witnesses/fba.json` | FBA 最优通量见证 + 三项残差诊断 |
| `conditions/<id>/witnesses/fva.jsonl` | 每个 FVA 端点一行：定位到条件/反应/方向的完整通量与残差 |
| `diagnostics.json` | 运行状态、条件级警告、硬失败信息 |

### 微型夹具与手算锚点（run_flux_reference_check.py，不 import 生产代码）

主网络含限量碳源（EX_C≤10）、两条替代合成路线（syn1/p1 与 syn2/p2，经 M/X）、
可逆反应 iso（M↔X，界 [-8,8]）、与主通路无关的内部循环 cyc1/cyc2（界 [0,3]）。
六个条件：基线、限碳（上限 4）、切断 syn1、切断 syn2、敲除 GROWTH（z=0 仍可行）、
故意不可行（EX_C 要求 6≤v≤4）。另两套网络分别演示**目标无界**与
**有限目标（z=5）下循环通量两端无界**。参考核验用守恒方程手算交点给出期望
（如基线 z*=10、限碳 z*=4、iso 在基线为 [-8,8] 可逆），再用独立 LP 重解每个端点，
并对每个见证逐项重算约束；同时验证 retain 1.0→0.5 范围放宽、条件乱序后数值与
状态一致（仅 `order` 随输入位置变化）。

## 目录结构

```
config/er_config.json   全部规则配置（含样例）
org_er/                 管线包：normalize / rules / entity / pipeline / store / revisions / reference / verifylib
org_er/contrib/         署名贡献核算子包：normalize / parse / dedup / account / store / verify / job
org_er/volprep/         体数据整备子包：geometry / resample / manifest / job / reference / selfcheck
org_er/rnaseq/          RNA-seq 片段计数子包：annotation / intervaltree / cigar / counting / outputs
org_er/lcms/            LC-MS 提取子包：cv / mzml / targets / extract / integral / job / errors
org_er/survey/         复杂抽样问卷估计子包：design / data / weights / jackknife / job / errors
org_er/flux/           稳态通量分析子包：model（校验/条件快照）/ solver（FBA/FVA/残差）/ job（编排/发布/读回）
scripts/                入口：make_sample_data / build / revise / verify / run_demo
                        + make_contrib_sample / contrib / run_contrib_demo / run_contrib_regression（核算作业）
                        + volprep / run_volprep_demo（体数据整备）
                        + rnaseq / make_rnaseq_sample / run_rnaseq_demo / run_rnaseq_reference_check
                          / run_rnaseq_sparse_check（RNA-seq 计数）
                        + lcms / make_lcms_sample / run_lcms_demo / run_lcms_reference_check
                          / run_lcms_memory_check（LC-MS 提取）
                        + survey / make_survey_sample / run_survey_demo / run_survey_reference_check
                          （复杂抽样问卷估计）
                        + flux / make_flux_sample / run_flux_demo / run_flux_reference_check
                          （稳态通量 FBA+FVA）
requirements-volprep.txt 体数据整备依赖（numpy、nibabel）
requirements-rnaseq.txt RNA-seq 计数依赖（pysam）
requirements-lcms.txt   LC-MS 提取依赖（numpy）
requirements-flux.txt   稳态通量分析依赖（scipy HiGHS、numpy）
data/                   生成的样本与修订事件、论文目录样本 papers.jsonl、
                        旧版 HUGEINT 账目固定样本 legacy_contrib_hugeint.duckdb、
                        RNA-seq 人工夹具 data/rnaseq/（GTF/SAM/BAM/样本表/故障夹具）、
                        调查夹具 data/survey/（主夹具/精确手算夹具/失败场景/坏数据）
output/                 DuckDB 库与导出的交付表（含 output/contrib/ 核算交付表、
                        output/volprep/ 整备产物、output/rnaseq/ 计数矩阵与逐片段判定、
                        output/survey/ 问卷估计表与逐人权重）
```
