"""指标口径版本管理接口：换版只追加、回滚切激活版本，历史验证结论永不改写。"""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas import ActionResult, EntryPayload
from app.verification import verification_service

router = APIRouter(prefix="/api/metric-revisions", tags=["指标口径版本"])


@router.get("", response_model=dict)
def list_revisions(metric_code: str | None = None) -> dict:
    """列出口径版本；带 metric_code 时只看单个指标，用于详情页结论对齐。"""
    items = verification_service.repo.list_revisions(metric_code)
    return {"total": len(items), "items": items}


@router.post("", response_model=ActionResult)
def register_revision(payload: EntryPayload) -> ActionResult:
    """登记新口径版本。新版本激活后旧验证结论标记 stale，必须重新执行验证。"""
    revision, message = verification_service.register_metric_revision(payload.values)
    if revision is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=revision)


@router.post("/rollback", response_model=ActionResult)
def rollback_revision(payload: EntryPayload) -> ActionResult:
    """回滚到指定 revision：只切换激活指针，verification_run 历史记录保持原样。"""
    metric_code = str(payload.values.get("metric_code") or "").strip()
    revision = str(payload.values.get("metric_revision") or "").strip()
    target, message = verification_service.rollback_revision(metric_code, revision)
    if target is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=target)
