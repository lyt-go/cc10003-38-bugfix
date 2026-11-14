"""数据验证业务规则：状态流转、字段校验与筛选口径都收在这里。

近线判定上线后，任务状态的权威来源是 verification 引擎：
- 列表与详情统一经 verification_service.decorate_entry 输出，结论同源；
- 手工「标记已验证」加护栏：存在非 PASS 的引擎结论时禁止手工翻转，
  从根上杜绝「异常任务却显示已验证」。
"""
from __future__ import annotations

from typing import Any

from app.store import store
from app.verification import verification_service
from app.verification.engine import PASS

MODULE = "dataval"
REQUIRED_FIELDS = ["任务编号", "验证数据集", "验证类型"]
STATUS_ORDER = ["草稿", "验证中", "已验证", "异常"]
ACTION_RULES = {"提交验证": "验证中", "标记已验证": "已验证", "标记异常": "异常"}
NEGATIVE_ACTIONS = []


class DatavalService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        verification_service.ensure_backfill()
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [verification_service.decorate_entry(row) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        verification_service.ensure_backfill()
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return verification_service.decorate_entry(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 登记时允许同时写入口径字段，执行前会被冻结进 verification_run 快照。
        for task_field in ("基准数据", "对比维度", "差异阈值", "验证周期"):
            if values.get(task_field) is not None:
                entry[task_field] = values.get(task_field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"验证任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据验证可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        latest = verification_service.repo.latest_run(entry_id)
        if action == "标记已验证" and latest is not None and latest.get("verdict") != PASS:
            return None, (
                f"引擎结论为 {latest.get('verdict')}（run={latest['run_id']}，"
                f"revision={latest.get('metric_revision')}），禁止手工标记已验证；"
                "请先处理异常清单或按当前口径重新执行"
            )
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return verification_service.decorate_entry(entry), f"验证任务已{action}"
