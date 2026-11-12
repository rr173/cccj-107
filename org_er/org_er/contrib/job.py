"""核算作业编排：计算 -> 暂存 -> 核验 -> 导出并核验 -> 原子切换 -> 清理/恢复。

运行方式见 scripts/contrib.py。核心不变量：
  * 精确分数在 Python 中用 Fraction 累计，落库为任意精度整数（BIGNUM），
    展示用小数单独存放并标注“仅近似”。
  * 新批次先写暂存 schema，不触碰当前成功结果；只有暂存结果通过独立核验、
    CSV 完整导出并通过回读核验后，才用**一个事务**切换当前表。
  * 任何失败/进程终止都留下可辨认的状态记录（contrib_state）与暂存物；
    下次启动由 recover_pending 自动回到一份完整结果（要么是旧批次，要么是已提交的新批次）。
"""
from __future__ import annotations

import csv
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import store as cstore
from .account import account_groups
from .dedup import dedup_lines
from .parse import parse_catalog


@dataclass
class JobSummary:
    n_lines: int = 0
    n_ok: int = 0
    n_rejected: int = 0
    n_groups: int = 0
    n_counted: int = 0
    n_quarantined: int = 0
    n_author_rows: int = 0
    institution_year: dict = field(default_factory=dict)   # (entity_id, year) -> "num/den"
    unassigned_year: dict = field(default_factory=dict)    # year -> "num/den"
    reject_reasons: dict = field(default_factory=dict)     # reason_prefix -> count
    batch_id: str = ""


def _summary(account, lines, ok_lines, groups, batch_id="") -> JobSummary:
    summary = JobSummary(
        n_lines=len(lines),
        n_ok=len(ok_lines),
        n_rejected=len(lines) - len(ok_lines),
        n_groups=len(groups),
        n_counted=sum(1 for g in groups if g.status == "counted"),
        n_quarantined=sum(1 for g in groups if g.status == "quarantined"),
        n_author_rows=len(account.authors),
        institution_year={k: f"{v[0].numerator}/{v[0].denominator}"
                          for k, v in sorted(account.institution_year.items())},
        unassigned_year={y: f"{v[0].numerator}/{v[0].denominator}"
                         for y, v in sorted(account.unassigned_year.items())},
        batch_id=batch_id,
    )
    for l in lines:
        if l.status == "rejected":
            key = l.reject_reason.split(":", 1)[0]
            summary.reject_reasons[key] = summary.reject_reasons.get(key, 0) + 1
    return summary


def _staging_paths(out_dir, batch_id):
    if not out_dir:
        return None, None
    out = Path(out_dir)
    staging = out.parent / f"{out.name}.staging-{batch_id}"
    prev = out.parent / f"{out.name}.prev-{batch_id}"
    return str(staging), str(prev)


# ---------------------------------------------------------------- 导出核验
def verify_export(con, outdir, *, schema="main") -> dict:
    """回读导出的 CSV，与数据库当前（或暂存）表的精确值逐格比对。

    只比较导出 SQL 自身的输出：精确整数列按十进制字符串比较（绝不经浮点），
    展示小数只核对其标注列存在，不参与“精确值”比对。
    """
    out = Path(outdir)
    problems = []
    checked = {}
    for name, sql in cstore.CSV_EXPORTS:
        p = out / name
        if not p.exists():
            problems.append(f"{name}: 缺失")
            continue
        expected = con.execute(sql.format(s=f'"{schema}"')).fetchall()
        with open(p, newline="", encoding="utf-8") as f:
            actual = [row for row in csv.reader(f)]
        if not actual:
            problems.append(f"{name}: 空文件（连表头都没有）")
            continue
        if len(actual) - 1 != len(expected):
            problems.append(f"{name}: 行数 {len(actual) - 1} != 数据库 {len(expected)}")
            continue

        def cell(v):
            if v is None:
                return ""
            if isinstance(v, bool):
                return "true" if v else "false"
            return str(v)

        for i, (exp, act) in enumerate(zip(expected, actual[1:]), start=2):
            exp_s = [cell(v) for v in exp]
            if exp_s != act:
                problems.append(f"{name}: 第 {i} 行与数据库不一致（回读={act[:4]} 期望={exp_s[:4]}）")
                break
        checked[name] = len(expected)
    manifest = cstore.read_manifest(outdir)
    if not manifest:
        problems.append(f"{cstore.MANIFEST_FILE}: 缺失（整套 CSV 未标记完成）")
    return {"ok": not problems, "problems": problems, "row_counts": checked,
            "manifest": manifest}


# ---------------------------------------------------------------- 恢复
def recover_pending(con, *, out_db=None, verbose=False) -> dict:
    """把任何未完成的运行收敛到一份完整结果；返回说明（恢复动作与保住/采用哪一批）。

    running / failed：新批次从未提交（切换是单事务），当前表仍是上一次成功结果；
      丢弃可辨认的暂存 schema 与临时 CSV，必要时从当前表重建缺失的 CSV。
    committed：数据库已切换到新批次但 CSV 目录尚未发布；继续完成目录切换。
    """
    state = cstore.get_state(con)
    status = state["status"]
    info = {"status": status, "batch_id": state["batch_id"],
            "previous_batch_id": state["previous_batch_id"],
            "action": "none", "kept": None, "message": ""}

    if status == "ready":
        if cstore.detect_format(con) == "empty":
            info["kept"] = state["batch_id"]
        else:
            info["kept"] = state["batch_id"] or cstore.get_meta(con, "batch_id")
        return info

    if status in ("running", "failed"):
        # 未提交的新批次：可辨认的未完成数据全部丢弃；旧结果原样保留
        dropped = cstore.cleanup_staging_artifacts(
            con, out_dir=state["out_dir"], keep_csv_dir=None)
        info["action"] = "discard_uncommitted_stage"
        info["dropped_schemas"] = dropped["schemas"]
        info["dropped_dirs"] = dropped["dirs"]
        fmt = cstore.detect_format(con)
        if fmt == "empty":
            cstore.set_state(con, status="ready", batch_id=None, previous_batch_id=None,
                             staging_schema=None, staging_csv_dir=None, prev_csv_dir=None,
                             message=f"丢弃未完成的批次 {state['batch_id']}；此前没有成功结果")
            info["kept"] = None
            info["message"] = "此前没有成功结果"
            return info
        current_batch = cstore.get_meta(con, "batch_id")
        info["kept"] = current_batch or "(旧版账目，无批次号)"
        note = ""
        if state["out_dir"]:
            if cstore.detect_format(con) == "legacy_hugeint":
                # 旧格式还没有展示列，无法按新格式导出；先交给 --reuse-existing 无损转换，
                # 本次恢复只清未完成数据，绝不改写旧账目
                note = "；旧 HUGEINT 账目未转换，CSV 待 --reuse-existing 转换后重新导出"
            else:
                _ensure_csv_matches(con, state["out_dir"], current_batch)
        cstore.set_state(con, status="ready", batch_id=current_batch,
                         previous_batch_id=None, staging_schema=None,
                         staging_csv_dir=None, prev_csv_dir=None,
                         message=f"丢弃未完成的批次 {state['batch_id']}；保留上一批次 {current_batch}")
        info["message"] = f"保留批次 {current_batch}{note}"
        return info

    if status == "committed":
        # 数据库已切换，只需完成 CSV 目录的原子发布
        if not state["out_dir"]:
            cstore.set_state(con, status="ready", batch_id=state["batch_id"],
                             previous_batch_id=None, staging_schema=None,
                             staging_csv_dir=None, prev_csv_dir=None,
                             message=f"批次 {state['batch_id']} 已完成（未要求 CSV）")
            info.update(action="finish_committed_no_csv", kept=state["batch_id"],
                        message=f"采用已提交批次 {state['batch_id']}")
            return info
        cstore.publish_csvs(con, out_dir=state["out_dir"],
                            staging_dir=state["staging_csv_dir"],
                            prev_dir=state["prev_csv_dir"],
                            batch_id=state["batch_id"])
        exp = verify_export(con, state["out_dir"])
        if not exp["ok"]:
            raise ValueError(f"已提交批次 {state['batch_id']} 的 CSV 发布核验失败: "
                             f"{exp['problems'][:3]}")
        cstore.set_state(con, status="ready", batch_id=state["batch_id"],
                         previous_batch_id=None, staging_schema=None,
                         staging_csv_dir=None, prev_csv_dir=None,
                         message=f"完成已提交批次 {state['batch_id']} 的 CSV 发布")
        info.update(action="finish_committed_publication", kept=state["batch_id"],
                    message=f"采用已提交批次 {state['batch_id']}")
        return info

    raise ValueError(f"无法识别的作业状态: {status}")


def _ensure_csv_matches(con, out_dir, current_batch, *, schema="main"):
    """旧批次仍在库里但 out 目录缺失/是别的批次：从当前表整套重建并原子换入。"""
    manifest = cstore.read_manifest(out_dir) if Path(out_dir).exists() else None
    if manifest and current_batch and manifest.get("batch_id") == current_batch:
        exp = verify_export(con, out_dir, schema=schema)
        if exp["ok"]:
            return
    # 导出的临时目录必须与目标目录同父目录，避免跨文件系统 rename 失败
    out = Path(out_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.parent / f"{out.name}.recover-{current_batch or 'current'}"
    shutil.rmtree(tmp, ignore_errors=True)
    cstore.export_csvs(con, tmp, schema=schema, batch_id=current_batch)
    exp = verify_export(con, tmp, schema=schema)
    if not exp["ok"]:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ValueError(f"恢复导出核验失败: {exp['problems'][:3]}")
    cstore.publish_csvs(con, out_dir=out_dir, staging_dir=str(tmp),
                        prev_dir=str(out) + f".prev-recover-{current_batch or 'current'}",
                        batch_id=current_batch)


# ---------------------------------------------------------------- 新运行
def run_job(*, er_db: str, round_no: int, catalog: str, priority: list,
            out_db: str, out_dir: str = None, con=None, verify: bool = True):
    """执行核算作业，返回 (summary, 作业库连接)。

    er_db     机构归并结果所在的 DuckDB 库（只读挂载，绝不写入）
    round_no  使用哪一轮归并结果
    catalog   论文目录 JSONL
    priority  供应方优先级（高 -> 低）；未列入的供应方并列最低
    out_db    作业库路径（':memory:' 或文件）；con 传入时复用该连接
    out_dir   给定时导出 CSV 交付表
    verify    新结果在切换前执行独立核验（默认开启）
    """
    from .verify import verify_run

    con = con or cstore.open_out_db(out_db)
    cstore.ensure_state(con)
    recover_pending(con, out_db=out_db)  # 先把上一次未完成运行收敛，再读机构映射

    # 先完成输入侧校验（轮次/目录），失败时不留下任何运行状态
    entity_map = _load_entities(con, er_db, round_no)
    lines = parse_catalog(catalog)
    ok_lines = [l for l in lines if l.status == "ok"]
    groups = dedup_lines(ok_lines, priority)
    account = account_groups(groups, entity_map)

    batch = cstore.new_batch_id()
    prev_batch = cstore.get_meta(con, "batch_id")
    stage = cstore.STAGE_PREFIX + batch
    stage_csv, prev_csv = _staging_paths(out_dir, batch)
    cstore.set_state(con, status="running", batch_id=batch, previous_batch_id=prev_batch,
                     staging_schema=stage, out_dir=str(out_dir) if out_dir else None,
                     staging_csv_dir=stage_csv, prev_csv_dir=prev_csv,
                     started_at=cstore._now(),
                     message=f"正在运行批次 {batch}，当前成功结果仍为 {prev_batch}")

    meta = {
        "er_db": str(er_db), "round_no": round_no, "catalog": str(catalog),
        "priority": ",".join(priority), "n_entity_keys": len(entity_map),
        "batch_id": batch, "format_version": cstore.FORMAT_VERSION,
        "fraction_format": cstore.FRACTION_FORMAT,
        "out_db": str(out_db), "out_dir": str(out_dir) if out_dir else "",
    }

    try:
        # 阶段 0：先提交暂存 schema 本身，使任何中断都留下“可辨认的未完成数据”
        # （空表/部分表 + 状态记录），恢复时整批丢弃；当前结果不受影响。
        con.execute("BEGIN TRANSACTION")
        cstore.create_tables(con, stage)
        con.execute("COMMIT")

        # 阶段 1：完整写入暂存 schema 并提交（暂存物显式命名，可辨认、可丢弃）
        con.execute("BEGIN TRANSACTION")
        cstore.write_tables(con, stage, meta=meta, lines=lines, groups=groups, account=account)
        con.execute("COMMIT")

        # 阶段 2：在暂存表上独立核验（绝不信任核算代码自身）
        if verify:
            checks = verify_run(con, schema=stage)
            if not checks["ok"]:
                bad = {k: v["detail"] for k, v in checks.items()
                       if k != "ok" and not v["ok"]}
                raise ValueError(f"暂存结果独立核验失败: {bad}")

        # 阶段 3：整套导出到临时目录，并回读核验精确分子/分母
        if out_dir:
            Path(stage_csv).parent.mkdir(parents=True, exist_ok=True)
            cstore.export_csvs(con, stage_csv, schema=stage, batch_id=batch)
            exp = verify_export(con, stage_csv, schema=stage)
            if not exp["ok"]:
                raise ValueError(f"导出核验失败: {exp['problems'][:3]}")

        # 阶段 4：单事务切换当前表 + 记 committed（崩溃只会整体回滚到旧结果）
        con.execute("BEGIN TRANSACTION")
        cstore.swap_tables(con, stage)
        cstore.set_state(con, status="committed", batch_id=batch,
                         previous_batch_id=prev_batch, staging_schema=stage,
                         staging_csv_dir=stage_csv, prev_csv_dir=prev_csv,
                         message=f"批次 {batch} 已切换，等待 CSV 发布")
        con.execute("COMMIT")
    except Exception as e:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        cstore.set_state(con, status="failed", batch_id=batch,
                         previous_batch_id=prev_batch, staging_schema=stage,
                         staging_csv_dir=stage_csv, prev_csv_dir=prev_csv,
                         message=f"批次 {batch} 失败（{e}）；上一成功结果 {prev_batch} 未动")
        if isinstance(e, ValueError):
            raise
        raise ValueError(f"批次 {batch} 未成为当前结果；保住的是上一份成功结果"
                         f"{(' ' + prev_batch) if prev_batch else '（此前无成功结果）'}：{e}") from e

    # 阶段 5：发布 CSV 目录（结束后才把状态记为 ready）
    if out_dir:
        try:
            cstore.publish_csvs(con, out_dir=out_dir, staging_dir=stage_csv,
                                prev_dir=prev_csv, batch_id=batch)
            exp = verify_export(con, out_dir)
            if not exp["ok"]:
                raise ValueError(f"发布后的 CSV 核验失败: {exp['problems'][:3]}")
        except Exception as e:
            # 数据库已切换到本批次、CSV 尚未确认发布：保留 committed 状态供下次自动续做；
            # 交付目录要么还是旧批次整套、要么已是新批次整套，不会新旧混合。
            cstore.set_state(con, status="committed", batch_id=batch,
                             previous_batch_id=prev_batch, staging_schema=None,
                             staging_csv_dir=stage_csv, prev_csv_dir=prev_csv,
                             message=f"批次 {batch} 已入库，CSV 发布未完成（{e}）；"
                                     f"重启会自动完成发布")
            raise ValueError(f"批次 {batch} 已写入作业库，但 CSV 发布未完成；"
                             f"旧交付表仍是上一份完整结果。重启或 --reuse-existing "
                             f"会自动完成发布：{e}") from e
        # 发布后再次确认当前表就是本批次（防止并发/误操作）
        cur = cstore.get_meta(con, "batch_id")
        if cur != batch:
            raise ValueError(f"当前结果批次 {cur} 与本次批次 {batch} 不一致")
    cstore.set_state(con, status="ready", batch_id=batch, previous_batch_id=None,
                     staging_schema=None, staging_csv_dir=None, prev_csv_dir=None,
                     message=f"批次 {batch} 已完成")
    cstore.drop_all_stage_schemas(con)

    return _summary(account, lines, ok_lines, groups, batch), con


def _load_entities(con, er_db, round_no):
    already = {r[0] for r in con.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    if "er" not in already:
        cstore.attach_er_readonly(con, er_db)
    try:
        return cstore.load_entity_map(con, round_no)
    finally:
        try:
            con.execute("DETACH er")
        except Exception:
            pass


# ---------------------------------------------------------------- 复用/迁移已有结果
def reuse_existing(*, out_db: str, out_dir: str = None, con=None, verify: bool = True):
    """识别并核验已有作业库；旧 HUGEINT 格式无损转换为新格式并重新导出。

    不重新计算目录，因此不需要机构库与目录（对应“现有小账目也能被识别、核验、重新导出”）。
    转换前备份数据库文件；转换走同一暂存/切换链路，失败则回滚并保留原账目。
    返回 (info, con)。
    """
    from .verify import verify_run

    con = con or cstore.open_out_db(out_db)
    rec = recover_pending(con, out_db=out_db)  # 只读判断；无状态时不会写库
    fmt = cstore.detect_format(con)
    info = {"detected_format": fmt, "recovered": rec, "migrated": False,
            "verified_before": None, "verified_after": None, "backup": None,
            "batch_id": cstore.get_meta(con, "batch_id")}
    if fmt == "empty":
        raise ValueError("作业库中没有可复用的核算结果")

    # 先核验已有账目（旧格式也能直接核验）。核验失败时不做任何写入，原库保持原样。
    pre = verify_run(con)
    info["verified_before"] = pre
    if not pre["ok"]:
        raise ValueError(f"已有账目核验失败，拒绝转换/导出: {pre}")

    if fmt == "legacy_hugeint":
        if verify and cstore.is_file_db(out_db):
            info["backup"] = str(cstore.backup_db(out_db))
        cstore.ensure_state(con)  # 核验通过后才开始写状态/暂存
        batch = cstore.new_batch_id()
        stage = cstore.STAGE_PREFIX + batch
        stage_csv, prev_csv = _staging_paths(out_dir, batch)
        prev_batch = cstore.get_meta(con, "batch_id")
        cstore.set_state(con, status="running", batch_id=batch,
                         previous_batch_id=prev_batch, staging_schema=stage,
                         out_dir=str(out_dir) if out_dir else None,
                         staging_csv_dir=stage_csv, prev_csv_dir=prev_csv,
                         started_at=cstore._now(),
                         message=f"旧 HUGEINT 账目迁移批次 {batch}")
        try:
            con.execute("BEGIN TRANSACTION")
            cstore.build_stage_from_legacy(con, stage, batch_id=batch, extra_meta={
                "out_db": str(out_db), "out_dir": str(out_dir) if out_dir else "",
            })
            con.execute("COMMIT")
            post = verify_run(con, schema=stage)
            info["verified_after"] = post
            if not post["ok"]:
                raise ValueError(f"迁移后的暂存结果核验失败: {post}")
            if out_dir:
                Path(stage_csv).parent.mkdir(parents=True, exist_ok=True)
                cstore.export_csvs(con, stage_csv, schema=stage, batch_id=batch)
                exp = verify_export(con, stage_csv, schema=stage)
                if not exp["ok"]:
                    raise ValueError(f"迁移导出核验失败: {exp['problems'][:3]}")
            con.execute("BEGIN TRANSACTION")
            cstore.swap_tables(con, stage)
            cstore.set_state(con, status="committed", batch_id=batch,
                             previous_batch_id=prev_batch, staging_schema=stage,
                             staging_csv_dir=stage_csv, prev_csv_dir=prev_csv,
                             message=f"迁移批次 {batch} 已切换，等待 CSV 发布")
            con.execute("COMMIT")
        except Exception as e:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
            cstore.set_state(con, status="failed", batch_id=batch,
                             previous_batch_id=prev_batch, staging_schema=stage,
                             staging_csv_dir=stage_csv, prev_csv_dir=prev_csv,
                             message=f"迁移失败（{e}）；原 HUGEINT 账目保留")
            raise ValueError(f"旧格式转换失败，原账目保留: {e}")
        if out_dir:
            cstore.publish_csvs(con, out_dir=out_dir, staging_dir=stage_csv,
                                prev_dir=prev_csv, batch_id=batch)
            exp = verify_export(con, out_dir)
            if not exp["ok"]:
                raise ValueError(f"迁移后 CSV 核验失败: {exp['problems'][:3]}")
        cstore.set_state(con, status="ready", batch_id=batch, previous_batch_id=None,
                         staging_schema=None, staging_csv_dir=None, prev_csv_dir=None,
                         message=f"旧 HUGEINT 账目已迁移为批次 {batch}")
        cstore.drop_all_stage_schemas(con)
        info.update(migrated=True, batch_id=batch)
        info["verified_after"] = verify_run(con)
    else:
        batch = cstore.get_meta(con, "batch_id")
        if out_dir:
            _ensure_csv_matches(con, out_dir, batch)
            info["verified_after"] = verify_run(con)
            info["export"] = verify_export(con, out_dir)
        else:
            info["verified_after"] = verify_run(con)
    return info, con
