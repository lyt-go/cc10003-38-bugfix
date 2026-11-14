"""verification_run / metric_revision 的持久化仓库。

当前进程用内存表实现，但方法签名与约束按关系库设计，便于后续平移：
- verification_run 的唯一键为 (entry_id, run_id)，run_id 即幂等键；
  重试记录以 attempt_of 指向首轮 run，重试自身仍有独立 run_id；
- 每个验证任务一把互斥锁：同一 entry 的并发执行/重试串行化，
  重复触发同一 run_id 直接返回已存在的完整结论，绝不产生第二条结论；
- metric_revision 以 (metric_code, revision) 唯一，换版只追加不改写，
  旧 run 记录保留原 revision，天然支持回滚审计。
"""
from __future__ import annotations

import threading
from typing import Any

from app.verification.engine import FAILED


class VerificationRepository:
    def __init__(self) -> None:
        self._runs: list[dict[str, Any]] = []
        self._revisions: list[dict[str, Any]] = []
        self._locks: dict[int, threading.RLock] = {}
        self._registry_lock = threading.Lock()

    def reset(self) -> None:
        """测试专用：清空内存表与锁。"""
        with self._registry_lock:
            self._runs.clear()
            self._revisions.clear()
            self._locks.clear()
        # 复位服务层的回填水位，保证用例之间互不串状态。
        from app.verification.service import verification_service

        verification_service._seeded = False

    def lock_for(self, entry_id: int) -> threading.RLock:
        with self._registry_lock:
            if entry_id not in self._locks:
                self._locks[entry_id] = threading.RLock()
            return self._locks[entry_id]

    # ---------------- verification_run ----------------

    def find_run(self, entry_id: int, run_id: str) -> dict[str, Any] | None:
        for run in self._runs:
            if run["entry_id"] == entry_id and run["run_id"] == run_id:
                return run
        return None

    def list_runs(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        if entry_id is None:
            return list(self._runs)
        return [run for run in self._runs if run["entry_id"] == entry_id]

    def latest_run(self, entry_id: int) -> dict[str, Any] | None:
        """最新一次「真实」运行；LEGACY 回填占位不参与当前结论与版本判定。"""
        runs = [run for run in self.list_runs(entry_id)
                if not str(run.get("verdict", "")).startswith("LEGACY_")]
        return runs[-1] if runs else None

    def latest_record(self, entry_id: int) -> dict[str, Any] | None:
        """最新一条运行记录（含 LEGACY 回填占位），供详情视图展示历史结论。"""
        runs = self.list_runs(entry_id)
        return runs[-1] if runs else None

    def save_snapshot(self, record: dict[str, Any]) -> dict[str, Any]:
        """执行前落口径快照（RUNNING 占位）。

        幂等：同一 (entry_id, run_id) 已存在终态时直接返回旧记录，
        调用方据此跳过重复执行，保证并发写入只有唯一一条结论；
        FAILED 是唯一可复活状态：重置为 RUNNING 并刷新快照、清掉 error，
        同 run_id 的重新执行仍只保留这一条记录。
        """
        existing = self.find_run(record["entry_id"], record["run_id"])
        if existing is not None:
            if existing["status"] != FAILED:
                return existing
            existing.clear()
            existing.update(record)
            existing.setdefault("status", "RUNNING")
            existing.pop("error", None)
            return existing
        stored = dict(record)
        stored.setdefault("status", "RUNNING")
        self._runs.append(stored)
        return stored

    def complete_run(self, entry_id: int, run_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
        """终态落库：RUNNING 占位被整体替换为完整结论，失败则只允许 FAILED。

        终态记录不可二次改写，杜绝 retry 把异常清单覆盖成空、把异常刷成 passed。
        """
        run = self.find_run(entry_id, run_id)
        if run is None:
            raise KeyError(f"run {run_id} 未建立快照，禁止无快照写结论")
        if run["status"] != "RUNNING":
            # 并发下第二个完成动作只会拿到既有终态，不覆盖任何字段。
            return run
        run.update(outcome)
        run["status"] = run["verdict"]
        return run

    def mark_failed(self, entry_id: int, run_id: str, error: str) -> dict[str, Any]:
        run = self.find_run(entry_id, run_id)
        if run is None:
            raise KeyError(f"run {run_id} 未建立快照，禁止无快照写失败")
        if run["status"] != "RUNNING":
            return run
        # 失败态显式保留空异常清单字段而不是删掉，并阻断「已验证」展示。
        run.setdefault("abnormal_result", [])
        run.setdefault("results", [])
        run["verdict"] = FAILED
        run["status"] = FAILED
        run["error"] = error
        return run

    # ---------------- metric_revision ----------------

    def upsert_revision(self, revision: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """登记指标口径版本。返回 (记录, 是否新建)；已存在则原样返回不改写。"""
        for existing in self._revisions:
            if (
                existing["metric_code"] == revision["metric_code"]
                and existing["revision"] == revision["revision"]
            ):
                return existing, False
        stored = dict(revision)
        stored.setdefault("active", True)
        self._revisions.append(stored)
        return stored, True

    def list_revisions(self, metric_code: str | None = None) -> list[dict[str, Any]]:
        if metric_code is None:
            return list(self._revisions)
        return [r for r in self._revisions if r["metric_code"] == metric_code]

    def active_revision(self, metric_code: str) -> dict[str, Any] | None:
        candidates = [
            r for r in self.list_revisions(metric_code)
            if r.get("active") and r.get("status") == "published"
        ]
        return candidates[-1] if candidates else None

    def deactivate_revisions(self, metric_code: str, *, keep: str | None = None) -> None:
        for revision in self._revisions:
            if revision["metric_code"] == metric_code and revision["revision"] != keep:
                revision["active"] = False

    def activate_revision(self, metric_code: str, revision: str) -> dict[str, Any]:
        target = next(
            (r for r in self.list_revisions(metric_code) if r["revision"] == revision),
            None,
        )
        if target is None:
            raise KeyError(f"指标 {metric_code} 不存在口径版本 {revision}，无法回滚")
        self.deactivate_revisions(metric_code, keep=revision)
        target["active"] = True
        target["status"] = "published"
        return target


verification_repo = VerificationRepository()
