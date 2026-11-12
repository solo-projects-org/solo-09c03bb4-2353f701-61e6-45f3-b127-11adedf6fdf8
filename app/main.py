"""FastAPI 入口：Swagger 可发起采集、查询目录与进度。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException, Query

from . import db
from .harvester import (
    HarvestConflictError,
    RevokeError,
    RevokeNotFoundError,
    harvest,
    revoke_run,
)
from .oai_client import OAIClient, OAIError
from .schemas import (
    BibRecordOut,
    HarvestRequest,
    HarvestResponse,
    ProgressResponse,
    RevokeResponse,
    RunOut,
)

OAI_ENDPOINT = os.environ.get(
    "OAI_ENDPOINT", "http://oai-source:8080/oai"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="联合目录补采采集服务",
    description=(
        "通过 OAI-PMH ListRecords(oai_dc) 按日期补采单馆书目。\n\n"
        "- 首次采集另填起始日；之后从上次成功采集的截止日（含当天）续采\n"
        "- 续页只用 resumptionToken；令牌失效则丢弃已取页、按原区间重抓一次\n"
        "- 整批采完才在一个事务内提交书目变更和截止日，无匹配也推进截止日\n"
        "- 撤销 API 只能撤销升级后最近一次成功的补采，单事务恢复目录与进度，\n"
        "  之后失败的批次不影响撤销；撤销后不可再撤销本批或更早批次"
    ),
    version="1.1.0",
    lifespan=lifespan,
)


def _validate_iso_date(value: str, field: str) -> str:
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"{field} 必须是 YYYY-MM-DD 格式"
        ) from exc
    return value


@app.post(
    "/api/v1/harvest",
    response_model=HarvestResponse,
    summary="发起一次补采",
    tags=["采集"],
)
def trigger_harvest(req: HarvestRequest) -> HarvestResponse:
    _validate_iso_date(req.until_date, "截止日")
    if req.from_date is not None:
        _validate_iso_date(req.from_date, "起始日")
        if req.from_date > req.until_date:
            raise HTTPException(
                status_code=422, detail="起始日不能晚于截止日（起止日都包含）"
            )

    with OAIClient(OAI_ENDPOINT) as client:
        try:
            summary = harvest(
                client,
                until_date=req.until_date,
                from_date=req.from_date,
            )
        except HarvestConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OAIError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "message": "采集失败，本批书目与截止日均未提交（已回滚）",
                    "oai_error": exc.code,
                    "detail": exc.message,
                },
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"采集失败，本批变更已回滚：{exc}",
            ) from exc

    return HarvestResponse(**summary.__dict__)


@app.post(
    "/api/v1/runs/{run_id}/revoke",
    response_model=RevokeResponse,
    summary="撤销一次成功补采（仅允许最近一次成功批次）",
    tags=["采集"],
)
def trigger_revoke(run_id: int) -> RevokeResponse:
    try:
        summary = revoke_run(run_id)
    except RevokeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RevokeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HarvestConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        # 撤销事务已整体回滚：目录、进度、运行历史均保持撤销前状态
        raise HTTPException(
            status_code=500,
            detail=f"撤销失败，全部变更已回滚：{exc}",
        ) from exc
    return RevokeResponse(**summary.__dict__)


@app.get(
    "/api/v1/catalog",
    response_model=list[BibRecordOut],
    summary="查询本地联合目录",
    tags=["目录"],
)
def list_catalog(
    status: str = Query(
        "active",
        description="active=在架（默认），withdrawn=已撤下，all=全部",
    ),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[BibRecordOut]:
    allowed = {"active", "withdrawn", "all"}
    if status not in allowed:
        raise HTTPException(
            status_code=422, detail=f"status 只支持 {sorted(allowed)}"
        )
    conn = db.get_connection()
    try:
        if status == "all":
            rows = conn.execute(
                "SELECT * FROM bib_records ORDER BY identifier LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bib_records WHERE status = ? "
                "ORDER BY identifier LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        return [BibRecordOut(**dict(row)) for row in rows]
    finally:
        conn.close()


@app.get(
    "/api/v1/catalog/{identifier}",
    response_model=BibRecordOut,
    summary="按 OAI 标识查询单条书目",
    tags=["目录"],
)
def get_record(identifier: str) -> BibRecordOut:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM bib_records WHERE identifier = ?", (identifier,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="记录不存在")
        return BibRecordOut(**dict(row))
    finally:
        conn.close()


@app.get(
    "/api/v1/progress",
    response_model=ProgressResponse,
    summary="查询采集进度（上次成功截止日）",
    tags=["进度"],
)
def get_progress() -> ProgressResponse:
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT last_until, last_harvest_at FROM harvest_progress WHERE id = 1"
        ).fetchone()
        if row is None:
            return ProgressResponse(last_until=None, last_harvest_at=None)
        return ProgressResponse(**dict(row))
    finally:
        conn.close()


@app.get(
    "/api/v1/runs",
    response_model=list[RunOut],
    summary="查询采集运行历史",
    tags=["进度"],
)
def list_runs(limit: int = Query(20, ge=1, le=200)) -> list[RunOut]:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM harvest_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [RunOut(**dict(row)) for row in rows]
    finally:
        conn.close()


@app.get("/health", tags=["系统"], summary="健康检查")
def health() -> dict:
    return {"status": "ok"}
