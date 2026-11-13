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

## 目录结构

```
config/er_config.json   全部规则配置（含样例）
org_er/                 管线包：normalize / rules / entity / pipeline / store / revisions / reference / verifylib
org_er/contrib/         署名贡献核算子包：normalize / parse / dedup / account / store / verify / job
org_er/volprep/         体数据整备子包：geometry / resample / manifest / job / reference / selfcheck
scripts/                入口：make_sample_data / build / revise / verify / run_demo
                        + make_contrib_sample / contrib / run_contrib_demo / run_contrib_regression（核算作业）
                        + volprep / run_volprep_demo（体数据整备）
requirements-volprep.txt 体数据整备依赖（numpy、nibabel）
data/                   生成的样本与修订事件、论文目录样本 papers.jsonl、
                        旧版 HUGEINT 账目固定样本 legacy_contrib_hugeint.duckdb
output/                 DuckDB 库与导出的交付表（含 output/contrib/ 核算交付表、output/volprep/ 整备产物）
```
