"""验证执行编排：口径快照、幂等执行、定向重试、换版/回滚与读取视图。

链路约束（对应线上故障的四个根因）：
1. 执行前先把 baseline_digest / compare_axis / tolerance / metric_revision 落进
   verification_run 快照，之后读取只认快照，不允许再出现字段为空；
2. 结论只来自 engine.evaluate_run，超过 tolerance 只能得到 ABNORMAL，
   服务层没有任何把异常刷成「已验证」的路径；
3. 同一 run_id 重复触发与并发写入共用 entry 级锁 + 幂等键，只产生唯一完整结论；
4. 指标换版后旧 run 的 metric_revision 原样保留并标记 stale，新 revision 必须
   重新执行才能产生新结论；回滚只是切换 active revision，历史 run 永不改写。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from app.store import store
from app.verification.engine import (
    ABSOLUTE_RATIO,
    FAILED,
    PASS,
    SNAPSHOT_FIELDS,
    VerdictError,
    evaluate_run,
)
from app.verification.repository import verification_repo

DATAVAL_MODULE = "dataval"

# 验证任务字段 -> 执行快照字段。任务上允许手工登记口径，执行时冻结进 run。
TASK_FIELD_MAP = {
    "baseline_digest": "基准数据",
    "compare_axis": "对比维度",
    "tolerance": "差异阈值",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class VerificationService:
    def __init__(self) -> None:
        self.repo = verification_repo
        self._seed_lock = threading.Lock()
        self._seeded = False

    # ---------------- 指标口径版本 ----------------

    def register_metric_revision(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        metric_code = str(values.get("metric_code") or "").strip()
        revision = str(values.get("metric_revision") or values.get("revision") or "").strip()
        if not metric_code or not revision:
            return None, "metric_code 与 metric_revision 均为必填"
        tolerance = values.get("tolerance")
        if tolerance is None:
            return None, "新口径必须声明 tolerance"
        try:
            record = {
                "metric_code": metric_code,
                "revision": revision,
                "tolerance": str(tolerance),
                "tolerance_mode": str(values.get("tolerance_mode") or ABSOLUTE_RATIO).strip(),
                "compare_axis": str(values.get("compare_axis") or "").strip() or None,
                "baseline_digest": str(values.get("baseline_digest") or "").strip() or None,
                "status": str(values.get("status") or "published").strip(),
                "active": bool(values.get("active", True)),
                "created_at": _now(),
            }
        except Exception as exc:  # pragma: no cover - 防御性兜底
            return None, f"口径登记参数不合法：{exc}"
        stored, created = self.repo.upsert_revision(record)
        if stored["active"] and stored["status"] == "published":
            self.repo.deactivate_revisions(metric_code, keep=revision)
        if not created:
            return stored, "口径版本已存在，未覆盖既有定义"
        return stored, "口径版本已登记"

    def rollback_revision(self, metric_code: str, revision: str) -> tuple[dict[str, Any] | None, str]:
        try:
            target = self.repo.activate_revision(metric_code, revision)
        except KeyError as exc:
            return None, str(exc)
        return target, f"已回滚到 {revision}；历史验证结论保持原 revision 不变，需按新激活口径重新执行"

    def revision_status(self, entry: dict[str, Any]) -> dict[str, Any]:
        """判断任务最新真实 run 的口径是否仍为当前激活版本（LEGACY 占位不参与）。"""
        latest = self.repo.latest_run(int(entry["id"]))
        metric_code = str(entry.get("metric_code") or entry.get("任务编号") or "").strip()
        active = self.repo.active_revision(metric_code)
        if latest is None:
            return {"stale": False, "current_revision": active["revision"] if active else None}
        run_revision = latest.get("metric_revision")
        current = active["revision"] if active else None
        return {
            "stale": current is not None and run_revision != current,
            "run_revision": run_revision,
            "current_revision": current,
        }

    # ---------------- 执行 ----------------

    def _resolve_snapshot(self, entry: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        """合并任务登记口径与本次请求覆盖值，执行前冻结四个快照字段。

        优先级：请求显式覆盖 > 当前激活 metric_revision 的定义 > 任务登记字段。
        激活版本必须压过任务上的陈旧口径，否则换版后仍会按旧 tolerance 判定。
        """
        metric_code = str(entry.get("metric_code") or entry.get("任务编号") or "").strip()
        active = self.repo.active_revision(metric_code)
        revision = override.get("metric_revision")
        if not revision and active is not None:
            revision = active["revision"]

        snapshot: dict[str, Any] = {}
        for snap_field, task_field in TASK_FIELD_MAP.items():
            if snap_field in override:
                # 请求显式给值（含空串）一律尊重：空串表示该口径字段缺失，
                # 必须保留下来让判定器归类，不能被激活版本/任务字段悄悄补回。
                snapshot[snap_field] = override.get(snap_field)
                continue
            active_value = active.get(snap_field) if active is not None else None
            snapshot[snap_field] = active_value or entry.get(task_field)
        snapshot["metric_revision"] = revision
        snapshot["tolerance_mode"] = str(
            override.get("tolerance_mode")
            or (active.get("tolerance_mode") if active else None)
            or ABSOLUTE_RATIO
        ).strip()
        return snapshot

    def execute(
        self,
        entry_id: int,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        attempt_of: str | None = None,
        retry_scope: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(DATAVAL_MODULE, entry_id)
        if entry is None:
            return None, f"验证任务 {entry_id} 不存在或已归档"
        run_id = (run_id or str(payload.get("run_id") or "")).strip() or f"run-{uuid.uuid4().hex}"
        lock = self.repo.lock_for(entry_id)
        with lock:
            duplicate = self.repo.find_run(entry_id, run_id)
            if duplicate is not None and duplicate["status"] != FAILED:
                # 幂等命中：重复触发/并发双写都拿到同一条完整结论；
                # FAILED 是唯一允许同 run_id 复活的中间态，不在此短路。
                if duplicate["status"] == "RUNNING":
                    return duplicate, "同一验证运行正在执行中，已复用既有 run"
                return duplicate, f"验证运行 {run_id} 已存在，返回唯一既有结论"

            snapshot = self._resolve_snapshot(entry, payload)
            record = {
                "entry_id": entry_id,
                "run_id": run_id,
                "attempt_of": attempt_of,
                "retry_scope": retry_scope,
                "checker_case_id": str(payload.get("checker_case_id") or entry.get("任务编号") or ""),
                "trace": str(payload.get("trace") or "").strip() or None,
                "status": "RUNNING",
                "created_at": _now(),
                # 执行前先落快照：读取链路永远能拿到这四个字段。
                **{field: snapshot.get(field) for field in SNAPSHOT_FIELDS},
                "tolerance_mode": snapshot["tolerance_mode"],
            }
            try:
                rows = self._prepare_rows(payload, attempt_of, retry_scope)
            except VerdictError as exc:
                # 入参/范围错误也要先留快照再标 FAILED，禁止无记录的静默失败。
                stored = self.repo.save_snapshot(record)
                failed = self.repo.mark_failed(entry_id, run_id, str(exc))
                self._sync_entry_status(entry, failed)
                return failed, f"验证执行被拒绝：{exc}"
            record["rows_submitted"] = len(rows)
            self.repo.save_snapshot(record)
            try:
                outcome = evaluate_run({**snapshot, "rows": rows,
                                        "checker_case_id": record["checker_case_id"],
                                        "trace": record["trace"]})
            except VerdictError as exc:
                failed = self.repo.mark_failed(entry_id, run_id, str(exc))
                self._sync_entry_status(entry, failed)
                return failed, f"验证执行被拒绝：{exc}"
            outcome.update({
                "entry_id": entry_id,
                "run_id": run_id,
                "attempt_of": attempt_of,
                "retry_scope": retry_scope,
                "created_at": record["created_at"],
                "finished_at": _now(),
                "rows_submitted": len(rows),
            })
            run = self.repo.complete_run(entry_id, run_id, outcome)
            self._sync_entry_status(entry, run)
            return run, self._success_message(run)

    def _prepare_rows(
        self, payload: dict[str, Any], attempt_of: str | None, retry_scope: str | None
    ) -> list[dict[str, Any]]:
        rows = [dict(row) for row in (payload.get("rows") or [])]
        if attempt_of and retry_scope == "region_missing_only":
            # 定向重试：只接收缺失维度的行；其余异常保留在原 run 的清单里不被覆盖。
            rows = [row for row in rows if not str(row.get("axis_value") or "").strip()]
            if not rows:
                raise VerdictError("region_missing_only 重试必须提供 axis_value 为空的缺失维度行")
        if not rows:
            raise VerdictError("缺少 rows 对比样本，拒绝无样本执行")
        for index, row in enumerate(rows, start=1):
            row.setdefault("row_key", f"row-{index}")
        return rows

    def retry(
        self, entry_id: int, run_id: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        origin = self.repo.find_run(entry_id, run_id)
        if origin is None:
            return None, f"原验证运行 {run_id} 不存在，无法重试"
        if origin["status"] == "RUNNING":
            return origin, "原验证运行仍在执行，无需重试"
        scope = str(payload.get("retry_scope") or "region_missing_only").strip()
        if scope != "region_missing_only":
            return None, f"暂不支持的重试范围：{scope}"
        # 重试必须沿用原口径快照；先比对当前激活 revision，换版后的重试直接拒绝，
        # 强制走新 revision 全量执行（不能被下面的 origin 快照短路）。
        entry = store.find(DATAVAL_MODULE, entry_id)
        metric_code = str(entry.get("metric_code") or entry.get("任务编号") or "").strip()
        active = self.repo.active_revision(metric_code)
        if active is not None and active["revision"] != origin.get("metric_revision"):
            return None, (
                f"指标已换版（{origin.get('metric_revision')} -> {active['revision']}），"
                "旧 run 不允许按新口径重试，请按新 revision 重新执行完整验证"
            )
        # 重试必须沿用原口径快照，不接受请求里夹带的其他 revision。
        payload = dict(payload)
        for field in SNAPSHOT_FIELDS:
            payload.setdefault(field, origin.get(field))
        if payload.get("metric_revision") != origin.get("metric_revision"):
            return None, (
                f"请求口径 {payload.get('metric_revision')} 与原 run "
                f"{origin.get('metric_revision')} 不一致，拒绝跨版本重试"
            )
        retry_run_id = f"{run_id}:retry:{uuid.uuid4().hex[:12]}"
        return self.execute(
            entry_id, payload, run_id=retry_run_id, attempt_of=run_id, retry_scope=scope
        )

    # ---------------- 读取视图 ----------------

    def get_run(self, entry_id: int, run_id: str) -> dict[str, Any] | None:
        return self.repo.find_run(entry_id, run_id)

    def list_runs(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        return self.repo.list_runs(entry_id)

    def decorate_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """给 dataval 列表/详情行挂上引擎结论视图，列表与详情同源，杜绝结论错位。"""
        view = dict(entry)
        # 展示取最新记录（含 LEGACY 历史结论）；版本陈旧判定只认真实 run。
        latest = self.repo.latest_record(int(entry["id"]))
        revision_info = self.revision_status(entry)
        view["revision_status"] = revision_info
        if latest is None:
            view["verification"] = None
            return view
        view["verification"] = {
            "run_id": latest["run_id"],
            "verdict": latest.get("verdict"),
            "status": latest.get("status"),
            "reason": latest.get("reason"),
            "metric_revision": latest.get("metric_revision"),
            "baseline_digest": latest.get("baseline_digest"),
            "compare_axis": latest.get("compare_axis"),
            "tolerance": latest.get("tolerance"),
            "tolerance_mode": latest.get("tolerance_mode"),
            "baseline_rows": latest.get("baseline_rows"),
            "actual_rows": latest.get("actual_rows"),
            "anomaly_rows": latest.get("anomaly_rows"),
            "abnormal_result": latest.get("abnormal_result", []),
            "stale": revision_info["stale"],
            "attempt_of": latest.get("attempt_of"),
            "retry_scope": latest.get("retry_scope"),
            "trace": latest.get("trace"),
            "finished_at": latest.get("finished_at"),
            "error": latest.get("error"),
        }
        return view

    def mark_backfill_watermark(self) -> None:
        """记录存量回填水位：此后新建/引导的任务不再被当作历史数据回填。"""
        with self._seed_lock:
            self._seeded = True

    def ensure_backfill(self) -> int:
        """存量回填：为没有 verification_run 的历史任务补一条 LEGACY 记录。

        只在回填水位（应用启动首轮回填）之前生效：水位之后登记的新任务
        必须走真实执行链路，不会凭空得到 LEGACY 结论。
        幂等且只追加：历史异常以 verdict=LEGACY_ABNORMAL 原样保留，
        不重算、不覆盖；回填后列表与详情同样走 decorate_entry 统一视图。
        """
        with self._seed_lock:
            if self._seeded:
                return 0
            count = 0
            for entry in store.rows(DATAVAL_MODULE):
                entry_id = int(entry["id"])
                if self.repo.list_runs(entry_id):
                    continue
                was_abnormal = bool(entry.get("abnormal")) or entry.get("status") == "异常"
                record = {
                    "entry_id": entry_id,
                    "run_id": f"legacy-{entry_id}",
                    "attempt_of": None,
                    "retry_scope": None,
                    "checker_case_id": str(entry.get("任务编号") or f"LEGACY-{entry_id}"),
                    "trace": None,
                    "status": "LEGACY_ABNORMAL" if was_abnormal else "LEGACY_PASS",
                    "verdict": "LEGACY_ABNORMAL" if was_abnormal else "LEGACY_PASS",
                    "reason": "LEGACY_BACKFILL",
                    "baseline_digest": entry.get("基准数据"),
                    "compare_axis": entry.get("对比维度"),
                    "tolerance": entry.get("差异阈值"),
                    "tolerance_mode": ABSOLUTE_RATIO,
                    "metric_revision": entry.get("metric_revision") or "legacy",
                    "baseline_rows": None,
                    "actual_rows": None,
                    "anomaly_rows": None,
                    "abnormal_result": (
                        [{
                            "row_key": f"legacy-{entry_id}",
                            "verdict": "LEGACY_ABNORMAL",
                            "reason": "LEGACY_BACKFILL",
                            "metric_revision": entry.get("metric_revision") or "legacy",
                        }]
                        if was_abnormal else []
                    ),
                    "results": [],
                    "created_at": _now(),
                    "finished_at": _now(),
                    "rows_submitted": 0,
                }
                self.repo.save_snapshot(record)
                count += 1
            self._seeded = True
            return count

    # ---------------- 内部工具 ----------------

    def _sync_entry_status(self, entry: dict[str, Any], run: dict[str, Any]) -> None:
        """把引擎终态映射回任务展示字段；任何非 PASS 结论都不可能显示「已验证」。"""
        verdict = run.get("verdict")
        entry["metric_revision"] = run.get("metric_revision")
        entry["baseline_digest"] = run.get("baseline_digest")
        entry["compare_axis"] = run.get("compare_axis")
        entry["tolerance_value"] = run.get("tolerance")
        entry["last_run_id"] = run["run_id"]
        entry["最近验证结果"] = verdict
        if verdict == PASS:
            entry["status"] = "已验证"
            entry["pending"] = False
            entry["abnormal"] = False
        elif verdict == FAILED:
            # 失败留在「验证中」并带 error，禁止出现 passed 但清单缺失的状态。
            entry["status"] = "验证中"
            entry["pending"] = True
            entry["abnormal"] = False
        else:
            entry["status"] = "异常"
            entry["pending"] = True
            entry["abnormal"] = True

    @staticmethod
    def _success_message(run: dict[str, Any]) -> str:
        verdict = run["verdict"]
        scope_note = f"，重试范围={run['retry_scope']}" if run.get("retry_scope") else ""
        if verdict == PASS:
            return f"验证通过（{run['run_id']}，revision={run['metric_revision']}{scope_note}）"
        return (
            f"验证结论 {verdict}（{run['run_id']}，revision={run['metric_revision']}{scope_note}，"
            f"异常 {len(run.get('abnormal_result', []))} 行，可逐行追溯原因）"
        )


verification_service = VerificationService()
