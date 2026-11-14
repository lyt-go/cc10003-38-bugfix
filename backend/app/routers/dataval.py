"""数据验证接口：任务维护 + 近线验证执行/重试/读取全链路。

新增的验证类接口只编排 verification_service，判定逻辑全部在 engine 内，
路由层不做任何 verdict 决策，保证「异常判定与指标定义同版本」。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.dataval import DatavalService
from app.verification import verification_service

router = APIRouter(prefix="/api/dataval", tags=["数据验证"])

service = DatavalService()

LIST_FIELDS = ["任务编号", "验证数据集", "验证类型", "基准数据", "对比维度", "差异阈值", "验证周期", "最近验证结果"]
STATUSES = ["草稿", "验证中", "已验证", "异常"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="草稿、验证中、已验证、异常"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤数据验证列表；列表结论与详情同源，不做二次拼装。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据验证清单：返回当前全量数据（含引擎结论视图与逐行异常清单）。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "dataval", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条验证任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"验证任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条验证任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="验证任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """手工状态动作；非 PASS 引擎结论下「标记已验证」会被护栏拒绝。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/verification/runs", response_model=ActionResult)
def execute_verification(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行近线验证。

    执行前冻结 baseline_digest/compare_axis/tolerance/metric_revision 快照；
    同一 run_id 重复触发或并发写入只返回唯一完整结论。
    """
    run, message = verification_service.execute(entry_id, payload.values)
    if run is None:
        return ActionResult(ok=False, message=message)
    ok = run.get("verdict") == "PASS"
    return ActionResult(ok=ok, message=message, entry=run)


@router.post("/{entry_id}/verification/runs/{run_id}/retry", response_model=ActionResult)
def retry_verification(entry_id: int, run_id: str, payload: EntryPayload) -> ActionResult:
    """定向重试：只处理缺失对比维度的行，原异常清单保留在原 run 不被覆盖。"""
    run, message = verification_service.retry(entry_id, run_id, payload.values)
    if run is None:
        return ActionResult(ok=False, message=message)
    ok = run.get("verdict") == "PASS"
    return ActionResult(ok=True, message=message, entry=run) if ok else ActionResult(
        ok=False, message=message, entry=run
    )


@router.get("/{entry_id}/verification/runs", response_model=dict)
def list_verification_runs(entry_id: int) -> dict[str, Any]:
    """读取任务下全部验证运行（含历史 revision 与重试链），用于详情页审计。"""
    if service.get_entry(entry_id) is None:
        raise HTTPException(status_code=404, detail=f"验证任务 {entry_id} 不存在或已归档")
    runs = verification_service.list_runs(entry_id)
    return {"entry_id": entry_id, "total": len(runs), "items": runs}


@router.get("/{entry_id}/verification/runs/{run_id}", response_model=dict)
def get_verification_run(entry_id: int, run_id: str) -> dict:
    """读取单次运行的完整结论；快照字段与 abnormal_result 永远随结论一起返回。"""
    run = verification_service.get_run(entry_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"验证运行 {run_id} 不存在")
    return run
