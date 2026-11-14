"""服务层验收：执行链路、幂等并发、重试范围、换版回滚、存量回填与读取快照。"""
from __future__ import annotations

import threading
import unittest

from app.store import store
from app.verification import verification_service
from app.verification.bootstrap import (
    PAY_BASELINE_DIGEST,
    PAY_CASE_ID,
    PAY_METRIC_CODE,
    PAY_REVISION,
    PAY_TASK_CODE,
    bootstrap_demo,
)
from app.verification.engine import ABNORMAL, EMPTY_AXIS, FAILED, PASS, ZERO_SAMPLE


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        verification_service.repo.reset()
        store._tables = {name: [dict(row) for row in rows]
                         for name, rows in store._tables.items()}
        # bootstrap_demo 的进程级标志需要复位，保证每个用例独立引导
        import app.verification.bootstrap as bootstrap_module
        bootstrap_module._done = False
        bootstrap_demo()
        self.entry_id = next(
            row["id"] for row in store.rows("dataval") if row.get("任务编号") == PAY_TASK_CODE
        )

    def _payload(self, **overrides):
        payload = {
            "checker_case_id": PAY_CASE_ID,
            "rows": [{
                "row_key": PAY_CASE_ID,
                "axis_value": "ALL_REGIONS",
                "expected_rows": 8042,
                "actual_rows": 8315,
            }],
        }
        payload.update(overrides)
        return payload


class ExecutionTests(ServiceTestCase):
    def test_d47a_executes_abnormal_and_keeps_snapshot(self):
        run, message = verification_service.execute(self.entry_id, self._payload())
        self.assertEqual(run["verdict"], ABNORMAL)
        self.assertIn("ABNORMAL", message)
        # 执行前快照四件套必须随结论持久化，读取时不再为空
        self.assertEqual(run["baseline_digest"], PAY_BASELINE_DIGEST)
        self.assertEqual(run["compare_axis"], "region")
        self.assertEqual(run["tolerance"], "0.020000")
        self.assertEqual(run["metric_revision"], PAY_REVISION)
        self.assertEqual(run["anomaly_rows"], 273)
        # 异常清单完整且可追溯到原 revision
        self.assertEqual(len(run["abnormal_result"]), 1)
        self.assertEqual(run["abnormal_result"][0]["metric_revision"], PAY_REVISION)
        self.assertEqual(run["abnormal_result"][0]["row_key"], PAY_CASE_ID)
        entry = store.find("dataval", self.entry_id)
        self.assertEqual(entry["status"], "异常")
        self.assertTrue(entry["abnormal"])
        self.assertNotEqual(entry["status"], "已验证")

    def test_boundary_ratio_0020_passes_00201_abnormal(self):
        ok_run, _ = verification_service.execute(self.entry_id, self._payload(run_id="b1", rows=[
            {"row_key": "EQ", "axis_value": "cn", "expected_rows": 10000, "actual_rows": 10200},
        ]))
        self.assertEqual(ok_run["verdict"], PASS)
        bad_run, _ = verification_service.execute(self.entry_id, self._payload(run_id="b2", rows=[
            {"row_key": "GT", "axis_value": "cn", "expected_rows": 10000, "actual_rows": 10201},
        ]))
        self.assertEqual(bad_run["verdict"], ABNORMAL)

    def test_zero_sample_run_fails(self):
        run, _ = verification_service.execute(self.entry_id, self._payload(run_id="z1", rows=[
            {"row_key": "ZERO", "axis_value": "cn", "expected_rows": 0, "actual_rows": 0},
        ]))
        self.assertEqual(run["verdict"], ZERO_SAMPLE)
        self.assertEqual(store.find("dataval", self.entry_id)["status"], "异常")

    def test_empty_region_row_classified_and_retry_only_handles_missing_axis(self):
        first, _ = verification_service.execute(self.entry_id, self._payload(run_id="full-1", rows=[
            {"row_key": PAY_CASE_ID, "axis_value": "ALL_REGIONS",
             "expected_rows": 8042, "actual_rows": 8315},
            {"row_key": "EMPTY_REGION_38", "axis_value": "",
             "expected_rows": 120, "actual_rows": 120},
        ]))
        self.assertEqual(first["verdict"], EMPTY_AXIS)
        keys = {r["row_key"] for r in first["abnormal_result"]}
        self.assertIn("EMPTY_REGION_38", keys)
        self.assertIn(PAY_CASE_ID, keys)

        # 重试：只补缺失 region 维度；原 run 的异常清单不被覆盖、anomaly_rows 不清零
        retry, message = verification_service.retry(self.entry_id, "full-1", {
            "rows": [
                {"row_key": "EMPTY_REGION_38", "axis_value": "",
                 "expected_rows": 120, "actual_rows": 120},
                # 混入一个非缺失维度行，必须被重试范围过滤掉
                {"row_key": "SHOULD_DROP", "axis_value": "eu",
                 "expected_rows": 1, "actual_rows": 2},
            ],
        })
        self.assertEqual(retry["retry_scope"], "region_missing_only")
        self.assertEqual(retry["attempt_of"], "full-1")
        self.assertEqual([r["row_key"] for r in retry["results"]], ["EMPTY_REGION_38"])
        self.assertEqual(retry["metric_revision"], PAY_REVISION)
        # 原 run 完整保留
        origin = verification_service.get_run(self.entry_id, "full-1")
        self.assertEqual(len(origin["abnormal_result"]), 2)
        self.assertEqual(origin["anomaly_rows"], 273)
        self.assertIn("region_missing_only", message)

    def test_retry_after_revision_change_rejected(self):
        first, _ = verification_service.execute(self.entry_id, self._payload(run_id="rev-1"))
        verification_service.register_metric_revision({
            "metric_code": PAY_METRIC_CODE, "metric_revision": "v4",
            "tolerance": "0.01", "compare_axis": "region",
            "baseline_digest": PAY_BASELINE_DIGEST,
        })
        retry, message = verification_service.retry(self.entry_id, "rev-1", {
            "rows": [{"row_key": "EMPTY_REGION_38", "axis_value": "",
                      "expected_rows": 1, "actual_rows": 1}],
        })
        self.assertIsNone(retry)
        self.assertIn("重新执行", message)
        # 旧结论保留原 revision 且被标记 stale
        view = verification_service.decorate_entry(store.find("dataval", self.entry_id))
        self.assertEqual(view["verification"]["metric_revision"], "v3")
        self.assertTrue(view["revision_status"]["stale"])
        self.assertEqual(view["revision_status"]["current_revision"], "v4")

    def test_new_revision_requires_fresh_execution(self):
        verification_service.execute(self.entry_id, self._payload(run_id="old"))
        verification_service.register_metric_revision({
            "metric_code": PAY_METRIC_CODE, "metric_revision": "v4",
            "tolerance": "0.05", "compare_axis": "region",
            "baseline_digest": PAY_BASELINE_DIGEST,
        })
        new_run, _ = verification_service.execute(self.entry_id, self._payload(run_id="new"))
        self.assertEqual(new_run["metric_revision"], "v4")
        self.assertEqual(new_run["tolerance"], "0.050000")
        # 3.4% 在 5% 新口径下通过
        self.assertEqual(new_run["verdict"], PASS)
        # 历史 run 依旧保留 v3 异常结论
        old = verification_service.get_run(self.entry_id, "old")
        self.assertEqual(old["metric_revision"], "v3")
        self.assertEqual(old["verdict"], ABNORMAL)

    def test_rollback_restores_revision_without_rewriting_history(self):
        verification_service.execute(self.entry_id, self._payload(run_id="r3"))
        verification_service.register_metric_revision({
            "metric_code": PAY_METRIC_CODE, "metric_revision": "v4",
            "tolerance": "0.05", "compare_axis": "region",
            "baseline_digest": PAY_BASELINE_DIGEST,
        })
        target, message = verification_service.rollback_revision(PAY_METRIC_CODE, "v3")
        self.assertTrue(target["active"])
        self.assertIn("回滚", message)
        self.assertEqual(verification_service.repo.active_revision(PAY_METRIC_CODE)["revision"], "v3")
        old = verification_service.get_run(self.entry_id, "r3")
        self.assertEqual(old["metric_revision"], "v3")
        self.assertEqual(old["verdict"], ABNORMAL)

    def test_duplicate_run_id_returns_single_conclusion(self):
        first, _ = verification_service.execute(self.entry_id, self._payload(run_id="dup-1"))
        second, message = verification_service.execute(self.entry_id, self._payload(run_id="dup-1"))
        self.assertIs(first, second)
        self.assertIn("唯一既有结论", message)
        runs = verification_service.list_runs(self.entry_id)
        self.assertEqual(len([r for r in runs if r["run_id"] == "dup-1"]), 1)

    def test_concurrent_executions_produce_unique_complete_runs(self):
        outcomes: list = []
        errors: list = []

        def worker() -> None:
            try:
                run, _ = verification_service.execute(
                    self.entry_id, self._payload(run_id="concurrent-1"))
                outcomes.append(run["run_id"])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 8)
        self.assertEqual(set(outcomes), {"concurrent-1"})
        runs = [r for r in verification_service.list_runs(self.entry_id)
                if r["run_id"] == "concurrent-1"]
        self.assertEqual(len(runs), 1)
        self.assertIn(runs[0]["verdict"], (PASS, ABNORMAL))
        self.assertIsNotNone(runs[0].get("abnormal_result"))

    def test_failure_never_leaves_passed_without_anomaly_list(self):
        # 空样本触发 VerdictError -> FAILED，任务停在验证中且不允许出现已验证
        run, message = verification_service.execute(
            self.entry_id, self._payload(run_id="fail-1", rows=[]))
        self.assertEqual(run["verdict"], FAILED)
        self.assertEqual(run["abnormal_result"], [])
        self.assertIn("拒绝", message)
        entry = store.find("dataval", self.entry_id)
        self.assertEqual(entry["status"], "验证中")
        self.assertFalse(entry["abnormal"])
        # 同 run_id 可重新执行并得到完整终态（失败占位允许复活一次）
        rerun, _ = verification_service.execute(
            self.entry_id, self._payload(run_id="fail-1", rows=[
                {"row_key": PAY_CASE_ID, "axis_value": "ALL",
                 "expected_rows": 8042, "actual_rows": 8315},
            ]))
        self.assertEqual(rerun["verdict"], ABNORMAL)
        self.assertEqual(len(rerun["abnormal_result"]), 1)

    def test_read_path_snapshot_fields_never_blank(self):
        verification_service.execute(self.entry_id, self._payload(run_id="read-1"))
        fetched = verification_service.get_run(self.entry_id, "read-1")
        for field in ("baseline_digest", "compare_axis", "tolerance", "metric_revision"):
            self.assertTrue(str(fetched.get(field) or "").strip(), f"{field} 读取为空")
        view = verification_service.decorate_entry(store.find("dataval", self.entry_id))
        self.assertEqual(view["verification"]["baseline_digest"], PAY_BASELINE_DIGEST)
        self.assertEqual(view["verification"]["compare_axis"], "region")

    def test_explicit_empty_snapshot_not_backfilled_from_active_revision(self):
        # 显式传空串表示口径字段缺失，禁止被激活 v3 定义悄悄补回
        no_base, _ = verification_service.execute(self.entry_id, self._payload(
            run_id="nb-1", baseline_digest="", rows=[
                {"row_key": "NB", "axis_value": "cn", "expected_rows": 10, "actual_rows": 10}]))
        self.assertEqual(no_base["verdict"], "BASELINE_MISSING")
        self.assertIsNone(no_base["baseline_digest"])
        no_axis, _ = verification_service.execute(self.entry_id, self._payload(
            run_id="na-1", compare_axis="", rows=[
                {"row_key": "NA", "axis_value": "cn",
                 "expected_rows": 10, "actual_rows": 10}]))
        self.assertEqual(no_axis["verdict"], EMPTY_AXIS)
        self.assertIsNone(no_axis["compare_axis"])

    def test_manual_pass_blocked_when_engine_verdict_abnormal(self):
        from app.services.dataval import DatavalService
        verification_service.execute(self.entry_id, self._payload(run_id="guard-1"))
        svc = DatavalService()
        entry, message = svc.run_action(self.entry_id, "标记已验证")
        self.assertIsNone(entry)
        self.assertIn("禁止手工标记已验证", message)
        self.assertEqual(store.find("dataval", self.entry_id)["status"], "异常")


class BackfillTests(ServiceTestCase):
    def test_legacy_rows_backfilled_without_losing_abnormal_history(self):
        # 构造一条历史异常任务（无任何 run）
        rows = store.rows("dataval")
        legacy_id = max(int(r["id"]) for r in rows) + 1
        rows.append({
            "id": legacy_id, "status": "异常", "pending": True, "abnormal": True,
            "任务编号": "DATA-LEGACY-1", "验证数据集": "历史", "验证类型": "核对",
            "基准数据": "baseline_legacy", "对比维度": "region", "差异阈值": "0.02",
        })
        count = verification_service.ensure_backfill()
        self.assertGreaterEqual(count, 1)
        # 幂等：第二次不新增
        again = verification_service.ensure_backfill()
        self.assertEqual(again, 0)
        run = verification_service.get_run(legacy_id, f"legacy-{legacy_id}")
        self.assertEqual(run["verdict"], "LEGACY_ABNORMAL")
        self.assertEqual(run["baseline_digest"], "baseline_legacy")
        self.assertEqual(run["abnormal_result"][0]["row_key"], f"legacy-{legacy_id}")
        view = verification_service.decorate_entry(store.find("dataval", legacy_id))
        self.assertEqual(view["verification"]["verdict"], "LEGACY_ABNORMAL")

    def test_list_and_detail_share_same_verdict_view(self):
        from app.services.dataval import DatavalService
        verification_service.execute(self.entry_id, self._payload(run_id="align-1"))
        svc = DatavalService()
        listed, _ = svc.list_entries(keyword=PAY_TASK_CODE)
        detail = svc.get_entry(self.entry_id)
        self.assertEqual(listed[0]["verification"]["verdict"],
                         detail["verification"]["verdict"])
        self.assertEqual(listed[0]["verification"]["run_id"],
                         detail["verification"]["run_id"])


if __name__ == "__main__":
    unittest.main()
