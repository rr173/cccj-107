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
python3 scripts/run_contrib_demo.py            # 端到端演示（18 项断言全过）
```

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

### 贡献核算（精确有理数）

每篇计入论文总贡献为 1：先均分给作者（`1/作者数`），作者份额再均分给他能定位到的
**不同**机构（同一机构多条署名先去重再分）。部分署名能定位时只在已定位机构间分配，
未定位引用保留待核查；署名全部未定位或无署名的作者，整份记入**待归属**。
隔离论文不计贡献。分子/分母以精确有理数（HUGEINT）保存，不用小数累计。

### 交付表（output/contrib/*.csv）

| 表 | 内容 |
|---|---|
| `contrib_author_details.csv` | 逐位作者分摊明细：去重来源、作者位置、署名原样、去重后机构、未定位引用、每次除法依据 |
| `contrib_institution_year.csv` | 按机构、年份汇总的贡献量（精确分数 + 展示用小数） |
| `contrib_unassigned_year.csv` | 待归属按年汇总 |
| `contrib_papers.csv` / `contrib_paper_versions.csv` | 论文去重结果、选取理由与全部版本差异 |
| `contrib_quarantine.csv` | 隔离论文及冲突版本 |
| `contrib_rejected_lines.csv` | 坏行及原因 |

### 独立核验（--verify / run_contrib_demo.py）

不复用核算代码路径，从落库结果反推：每篇计入论文的机构份额 + 待归属份额恰好为 1；
作者份额 == 1/作者数且分摊结构一致；机构×年份汇总由明细逐行重算可逐项追到作者明细；
隔离论文零分摊；每年机构汇总 + 待归属 == 该年计入论文数；ok 行恰好进一组、坏行不进组。
演示另验证：机构库作业前后全表快照一致、目录乱序读入结果签名一致、
同一目录在不同轮次下的定位差异（轮次即只读对照的版本）。

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
scripts/                入口：make_sample_data / build / revise / verify / run_demo
                        + make_contrib_sample / contrib / run_contrib_demo（核算作业）
data/                   生成的样本与修订事件、论文目录样本 papers.jsonl
output/                 DuckDB 库与导出的交付表（含 output/contrib/ 核算交付表）
```
