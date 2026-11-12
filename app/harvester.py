"""采集编排：保证“翻页失败不让半批记录入库”。

流程：
1. 首次采集由调用方提供起始日；之后起始日取 harvest_progress.last_until
   （上次成功采集的截止日，含当天），实现无缝续采。
2. ListRecords 按日期拉取 oai_dc，续页仅用 resumptionToken。
3. resumptionToken 失效（badResumptionToken）时丢弃已取页，按原区间整体重抓一次。
4. 所有页取完后，在单个 SQLite 事务内 upsert/撤下书目并推进截止日；
   无匹配记录同样推进截止日。事务失败则整批回滚，进度不变。
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from . import db
from .oai_client import OAIClient, OAIError, OAIRecord

logger = logging.getLogger("harvester")

# 进程内互斥：同一时刻只允许一个采集或撤销任务（单进程部署），
# 补采从读取进度到结束与撤销全程互斥，重叠时拒绝后发请求
_OPERATION_LOCK = threading.Lock()


class HarvestConflictError(RuntimeError):
    pass


class RevokeNotFoundError(RuntimeError):
    """撤销目标运行不存在（映射 404）。"""


class RevokeError(RuntimeError):
    """撤销条件不满足（映射 409）。"""


@dataclass
class HarvestSummary:
    from_date: str
    until_date: str
    pages: int
    fetched: int
    inserted: int
    updated: int
    withdrawn: int
    retries: int
    run_id: int
    status: str = "success"
    note: str | None = None


@dataclass
class RevokeSummary:
    run_id: int
    restored: int
    deleted: int
    prev_last_until: str | None
    revoked_at: str


def _fetch_full_window(client: OAIClient, from_date: str | None,
                       until_date: str) -> tuple[list[OAIRecord], int, int]:
    """完整拉取一个日期区间。

    返回 (全部记录, 页数, 因 badResumptionToken 触发的重抓次数)。
    令牌失效时丢弃已取页，按原区间重新开始，只重抓一次。
    """
    retries = 0

    def _attempt() -> tuple[list[OAIRecord], int]:
        nonlocal retries
        collected: list[OAIRecord] = []
        pages = 0
        page = client.list_records_first(
            from_date=from_date, until_date=until_date
        )
        pages += 1
        collected.extend(page.records)
        token = page.resumption_token

        while token:
            try:
                page = client.list_records_resume(token)
            except OAIError as exc:
                if exc.code != "badResumptionToken":
                    raise
                # 令牌失效：本批已取页全部作废，按原区间重抓一次
                retries += 1
                logger.warning(
                    "resumptionToken 失效，丢弃已取的 %d 页，按区间 %s~%s 重抓",
                    pages, from_date, until_date,
                )
                return _retry_once()
            pages += 1
            collected.extend(page.records)
            token = page.resumption_token
        return collected, pages

    def _retry_once() -> tuple[list[OAIRecord], int]:
        collected: list[OAIRecord] = []
        pages = 0
        page = client.list_records_first(
            from_date=from_date, until_date=until_date
        )
        pages += 1
        collected.extend(page.records)
        token = page.resumption_token
        while token:
            # 重抓期间再次失效属于不可恢复错误，直接失败（不循环重试）
            page = client.list_records_resume(token)
            pages += 1
            collected.extend(page.records)
            token = page.resumption_token
        return collected, pages

    records, pages = _attempt()
    return records, pages, retries


def harvest(client: OAIClient, *, until_date: str,
            from_date: str | None) -> HarvestSummary:
    """执行一次采集。from_date 为 None 时从上次成功截止日续采。"""
    if not _OPERATION_LOCK.acquire(blocking=False):
        raise HarvestConflictError("已有采集或撤销任务正在执行，请稍后再试")
    try:
        return _harvest_locked(client, until_date=until_date,
                               from_date=from_date)
    finally:
        _OPERATION_LOCK.release()


def _harvest_locked(client: OAIClient, *, until_date: str,
                    from_date: str | None) -> HarvestSummary:
    conn = db.get_connection()
    run_id: int | None = None
    try:
        # 起始日：显式传入优先，否则取上次成功采集的截止日
        effective_from = from_date or db.get_last_until(conn)
        run_id = db.start_run(conn, effective_from or "", until_date)
        conn.commit()

        try:
            records, pages, retries = _fetch_full_window(
                client, effective_from, until_date
            )
        except OAIError as exc:
            if exc.code == "noRecordsMatch":
                # 无匹配：不写书目，但仍推进截止日
                return _commit_empty_window(
                    conn, run_id, effective_from, until_date
                )
            raise

        # 整批落库：单事务，任何异常整体回滚，半批数据不会入库
        with db.transaction(conn):
            # 批前进度在同一事务、同一把互斥锁下读取，保证撤销可准确回退
            prev_last_until, prev_harvest_at = db.get_progress_snapshot(conn)
            inserted = updated = withdrawn = 0
            for record in records:
                if record.deleted:
                    # 遇删除标记就撤下；库中不存在则落 withdrawn 墓碑
                    existed = db.snapshot_bib_record(
                        conn, run_id=run_id, identifier=record.identifier
                    )
                    if existed:
                        db.withdraw_bib_record(
                            conn, identifier=record.identifier,
                            datestamp=record.datestamp,
                        )
                    else:
                        # 本批新增的墓碑：登记触碰标识，撤销时直接删除
                        db.insert_run_touched(
                            conn, run_id=run_id, identifier=record.identifier
                        )
                        db.insert_withdrawn_tombstone(
                            conn, identifier=record.identifier,
                            datestamp=record.datestamp,
                        )
                    withdrawn += 1
                    continue
                existed = db.snapshot_bib_record(
                    conn, run_id=run_id, identifier=record.identifier
                )
                if not existed:
                    # 本批新增：登记触碰标识（pre_image 为 NULL），撤销时删除
                    db.insert_run_touched(
                        conn, run_id=run_id, identifier=record.identifier
                    )
                outcome = db.upsert_bib_record(
                    conn,
                    identifier=record.identifier,
                    datestamp=record.datestamp,
                    fields=_first_values(record.dc),
                    raw_dc=json.dumps(record.dc, ensure_ascii=False),
                )
                if outcome == "inserted":
                    inserted += 1
                else:
                    updated += 1

            db.advance_progress(conn, until_date)
            db.finish_run(
                conn, run_id, status="success", pages=pages,
                fetched=len(records), inserted=inserted, updated=updated,
                withdrawn=withdrawn, retries=retries, error=None,
                snapshot_version=db.SNAPSHOT_VERSION,
                prev_last_until=prev_last_until,
                prev_harvest_at=prev_harvest_at,
            )
            conn.commit()

        logger.info(
            "采集成功 区间=%s~%s 页=%d 取=%d 增=%d 改=%d 撤=%d 重抓=%d",
            effective_from, until_date, pages, len(records),
            inserted, updated, withdrawn, retries,
        )
        return HarvestSummary(
            from_date=effective_from or "",
            until_date=until_date,
            pages=pages,
            fetched=len(records),
            inserted=inserted,
            updated=updated,
            withdrawn=withdrawn,
            retries=retries,
            run_id=run_id,
        )
    except Exception as exc:
        # 失败：确保事务回滚，截止日不推进；另起事务记录失败历史
        conn.rollback()
        logger.exception("采集失败，已回滚本批全部变更：%s", exc)
        if run_id is not None:
            try:
                fail_conn = db.get_connection()
                try:
                    db.finish_run(
                        fail_conn, run_id, status="failed",
                        pages=0, fetched=0, inserted=0, updated=0, withdrawn=0,
                        retries=0, error=str(exc),
                    )
                    fail_conn.commit()
                finally:
                    fail_conn.close()
            except Exception:  # pragma: no cover - 历史记录失败不掩盖主错误
                logger.exception("写入失败运行历史时出错")
        raise
    finally:
        conn.close()


def _commit_empty_window(conn, run_id: int, from_date: str | None,
                         until_date: str) -> HarvestSummary:
    with db.transaction(conn):
        prev_last_until, prev_harvest_at = db.get_progress_snapshot(conn)
        db.advance_progress(conn, until_date)
        db.finish_run(
            conn, run_id, status="success", pages=0, fetched=0,
            inserted=0, updated=0, withdrawn=0, retries=0, error=None,
            snapshot_version=db.SNAPSHOT_VERSION,
            prev_last_until=prev_last_until,
            prev_harvest_at=prev_harvest_at,
        )
        conn.commit()
    logger.info("区间 %s~%s 无匹配记录，截止日仍推进至 %s",
                from_date, until_date, until_date)
    return HarvestSummary(
        from_date=from_date or "",
        until_date=until_date,
        pages=0,
        fetched=0,
        inserted=0,
        updated=0,
        withdrawn=0,
        retries=0,
        run_id=run_id,
        note="区间内无匹配记录，截止日已推进",
    )


def revoke_run(run_id: int) -> RevokeSummary:
    """撤销一次成功补采。

    只允许撤销升级后最近一次成功的补采；之后的失败批次不影响判定。
    与采集共用进程锁，重叠时拒绝后发请求。目录恢复、进度回退、运行标记
    在同一个 SQLite 事务内完成，任一步失败整体回滚。
    """
    if not _OPERATION_LOCK.acquire(blocking=False):
        raise HarvestConflictError("已有采集或撤销任务正在执行，请稍后再试")
    try:
        return _revoke_locked(run_id)
    finally:
        _OPERATION_LOCK.release()


def _revoke_locked(run_id: int) -> RevokeSummary:
    conn = db.get_connection()
    try:
        with db.transaction(conn):
            run = db.get_run(conn, run_id)
            if run is None:
                # 不存在属于客户端寻址错误：回滚只读事务并转 404
                conn.rollback()
                raise RevokeNotFoundError(f"采集运行 {run_id} 不存在")

            if run["snapshot_version"] is None:
                raise RevokeError(
                    f"运行 {run_id} 是升级前的旧批次，缺少撤销快照，无法撤销"
                )
            if run["status"] == "revoked":
                raise RevokeError(f"运行 {run_id} 已撤销，不能重复撤销")
            if run["status"] != "success":
                raise RevokeError(
                    f"运行 {run_id} 状态为 {run['status']}，只有成功的补采可撤销"
                )

            latest = db.get_latest_terminal_run(conn)
            if latest is None or latest["id"] != run_id:
                latest_id = latest["id"] if latest is not None else "无"
                raise RevokeError(
                    f"运行 {run_id} 不是最近一次成功的补采"
                    f"（最近可撤销批次为 {latest_id}），无法撤销"
                )

            restored = db.restore_bib_snapshot(conn, run_id)
            deleted = db.delete_unsnapshotted_touched(conn, run_id)
            db.restore_progress(
                conn, run["prev_last_until"], run["prev_harvest_at"]
            )
            revoked_at = db.utc_now_iso()
            db.mark_run_revoked(conn, run_id, revoked_at=revoked_at)
            db.delete_snapshots(conn, run_id)
            conn.commit()

        logger.info(
            "撤销成功 run=%d 恢复改写=%d 删除新增=%d 进度回退至 %s",
            run_id, restored, deleted,
            run["prev_last_until"] or "未采集",
        )
        return RevokeSummary(
            run_id=run_id,
            restored=restored,
            deleted=deleted,
            prev_last_until=run["prev_last_until"],
            revoked_at=revoked_at,
        )
    finally:
        conn.close()


def _first_values(dc: dict) -> dict:
    """Dublin Core 多值字段取首值落列，完整多值保留在 raw_dc。"""
    return {key: (values[0] if values else None) for key, values in dc.items()}
