"""请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class HarvestRequest(BaseModel):
    until_date: str = Field(
        ...,
        alias="until",
        description="采集截止日（含当天），格式 YYYY-MM-DD",
        examples=["2026-10-15"],
    )
    from_date: str | None = Field(
        default=None,
        alias="from",
        description=(
            "起始日（含当天），首次采集另填；之后留空则自动从上次成功"
            "采集的截止日续采"
        ),
        examples=["2026-10-01"],
    )

    model_config = {"populate_by_name": True}


class HarvestResponse(BaseModel):
    status: str = Field(..., description="success / failed")
    from_date: str = Field(..., description="实际使用的起始日（含）")
    until_date: str = Field(..., description="截止日（含）")
    pages: int = Field(..., description="本次成功拉取的页数")
    fetched: int = Field(..., description="拉取到的记录总数（含删除标记）")
    inserted: int
    updated: int
    withdrawn: int = Field(..., description="因删除标记撤下的记录数")
    retries: int = Field(..., description="令牌失效后按原区间重抓的次数（0 或 1）")
    run_id: int
    note: str | None = None


class ProgressResponse(BaseModel):
    last_until: str | None = Field(
        None, description="上次成功采集的截止日（含）；无记录表示尚未采集"
    )
    last_harvest_at: str | None = None


class BibRecordOut(BaseModel):
    identifier: str
    datestamp: str
    status: str
    title: str | None
    creator: str | None
    date: str | None
    identifier_dc: str | None
    updated_at: str


class RunOut(BaseModel):
    id: int
    started_at: str
    finished_at: str | None
    from_date: str
    until_date: str
    status: str = Field(
        ..., description="running / success / failed / revoked（已撤销）"
    )
    pages: int
    fetched: int
    inserted: int
    updated: int
    withdrawn: int
    retries: int
    error: str | None
    snapshot_version: int | None = Field(
        None, description="非空表示升级后带快照的批次，可在满足条件时撤销"
    )
    prev_last_until: str | None = Field(
        None, description="该批采集前的续采进度（撤销后回退到此值）"
    )
    revoked_at: str | None = Field(None, description="撤销时间，未撤销为空")


class RevokeResponse(BaseModel):
    run_id: int = Field(..., description="被撤销的采集运行编号")
    status: str = Field("revoked", description="固定为 revoked")
    restored: int = Field(..., description="恢复到批前内容/状态的改写书目数")
    deleted: int = Field(..., description="移除的本批新增书目数（含墓碑）")
    prev_last_until: str | None = Field(
        None, description="回退后的续采截止日；null 表示退回未采集状态"
    )
    revoked_at: str = Field(..., description="撤销完成时间（UTC）")
