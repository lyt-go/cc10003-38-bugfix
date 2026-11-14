"""判定器验收：边界、零样本、缺失维度/基准、D47A 异常样例与逐行可追溯。"""
from __future__ import annotations

import unittest

from app.verification.engine import (
    ABNORMAL,
    BASELINE_MISSING,
    EMPTY_AXIS,
    PASS,
    ZERO_SAMPLE,
    VerdictError,
    evaluate_run,
    judge_row,
)
from app.verification.engine import VerdictContext

BASE = {
    "checker_case_id": "CASE-1",
    "baseline_digest": "baseline_pay_20261012",
    "compare_axis": "region",
    "tolerance": "0.02",
    "tolerance_mode": "absolute_ratio",
    "metric_revision": "v3",
}


def ctx(**overrides):
    payload = {**BASE, "rows": [{"row_key": "CTX-PLACEHOLDER", "axis_value": "cn",
                                 "expected_rows": 1, "actual_rows": 1}], **overrides}
    return VerdictContext.from_payload(payload)


class BoundaryTests(unittest.TestCase):
    def test_diff_equal_to_tolerance_passes(self):
        # 200/10000 = 0.0200，恰好等于 2% 边界 -> 显式按通过处理
        row = judge_row(ctx(), {"row_key": "EQ", "axis_value": "cn",
                                "expected_rows": 10000, "actual_rows": 10200})
        self.assertEqual(row["verdict"], PASS)
        self.assertEqual(row["diff_ratio"], "0.020000")
        self.assertEqual(row["reason"], "RATIO_WITHIN_TOLERANCE")

    def test_diff_just_above_tolerance_is_abnormal(self):
        # 201/10000 = 0.0201，严格大于 2% -> 异常
        row = judge_row(ctx(), {"row_key": "GT", "axis_value": "cn",
                                "expected_rows": 10000, "actual_rows": 10201})
        self.assertEqual(row["verdict"], ABNORMAL)
        self.assertEqual(row["diff_ratio"], "0.020100")
        self.assertEqual(row["reason"], "RATIO_EXCEEDS_TOLERANCE")

    def test_negative_diff_uses_absolute_value(self):
        row = judge_row(ctx(), {"row_key": "NEG", "axis_value": "cn",
                                "expected_rows": 10000, "actual_rows": 9800})
        self.assertEqual(row["verdict"], PASS)
        self.assertEqual(row["diff_ratio"], "0.020000")

    def test_float_precision_boundary_not_misjudged(self):
        # 0.15 + 0.05 风格的浮点陷阱：Decimal 口径下 0.2 阈值必须稳定通过
        row = judge_row(ctx(tolerance="0.2"), {"row_key": "FP", "axis_value": "cn",
                                               "expected_rows": 10, "actual_rows": 12})
        self.assertEqual(row["verdict"], PASS)


class StructuralDefectTests(unittest.TestCase):
    def test_zero_sample_is_failure_not_pass(self):
        row = judge_row(ctx(), {"row_key": "ZERO", "axis_value": "cn",
                                "expected_rows": 0, "actual_rows": 0})
        self.assertEqual(row["verdict"], ZERO_SAMPLE)
        self.assertIsNone(row["diff_ratio"])
        self.assertEqual(row["reason"], "ZERO_EXPECTED_ROWS")

    def test_empty_axis_value_is_empty_axis(self):
        row = judge_row(ctx(), {"row_key": "EMPTY_REGION_38", "axis_value": "",
                                "expected_rows": 100, "actual_rows": 100})
        self.assertEqual(row["verdict"], EMPTY_AXIS)
        self.assertEqual(row["reason"], "COMPARE_AXIS_VALUE_EMPTY")

    def test_missing_compare_axis_is_classified_separately(self):
        row = judge_row(ctx(compare_axis=""), {"row_key": "NOAXIS", "axis_value": "cn",
                                               "expected_rows": 100, "actual_rows": 100})
        self.assertEqual(row["verdict"], EMPTY_AXIS)
        self.assertEqual(row["reason"], "COMPARE_AXIS_MISSING")

    def test_missing_baseline_digest_is_classified_separately(self):
        row = judge_row(ctx(baseline_digest=""), {"row_key": "NOBASE", "axis_value": "cn",
                                                  "expected_rows": 100, "actual_rows": 100})
        self.assertEqual(row["verdict"], BASELINE_MISSING)
        self.assertEqual(row["reason"], "BASELINE_DIGEST_MISSING")

    def test_defect_precedence_baseline_over_axis(self):
        row = judge_row(ctx(baseline_digest="", compare_axis=""),
                        {"row_key": "BOTH", "axis_value": "", "expected_rows": 0, "actual_rows": 0})
        self.assertEqual(row["verdict"], BASELINE_MISSING)


class RunEvaluationTests(unittest.TestCase):
    def test_d47a_three_point_four_percent_is_abnormal(self):
        # 故障复现样例：8042 -> 8315，差异 273，diff_ratio≈0.034，阈值 0.02
        result = evaluate_run({**BASE, "rows": [
            {"row_key": "PAY-VERIFY-20261012-D47A", "axis_value": "ALL",
             "expected_rows": 8042, "actual_rows": 8315},
        ]})
        self.assertEqual(result["verdict"], ABNORMAL)
        self.assertEqual(result["baseline_rows"], 8042)
        self.assertEqual(result["actual_rows"], 8315)
        self.assertEqual(result["anomaly_rows"], 273)
        self.assertEqual(result["baseline_digest"], "baseline_pay_20261012")
        self.assertEqual(result["metric_revision"], "v3")
        self.assertEqual(len(result["abnormal_result"]), 1)
        only = result["abnormal_result"][0]
        self.assertEqual(only["row_key"], "PAY-VERIFY-20261012-D47A")
        self.assertAlmostEqual(float(only["diff_ratio"]), 273 / 8042, places=5)

    def test_every_row_keeps_row_key_reason_and_revision(self):
        result = evaluate_run({**BASE, "rows": [
            {"row_key": "R1", "axis_value": "cn", "expected_rows": 1000, "actual_rows": 1000},
            {"row_key": "R2", "axis_value": "us", "expected_rows": 1000, "actual_rows": 1100},
        ]})
        self.assertEqual(result["verdict"], ABNORMAL)
        for row in result["results"]:
            self.assertTrue(row["row_key"])
            self.assertTrue(row["reason"])
            self.assertEqual(row["metric_revision"], "v3")
        self.assertEqual({r["row_key"] for r in result["abnormal_result"]}, {"R2"})

    def test_run_level_zero_sample_verdict(self):
        result = evaluate_run({**BASE, "rows": [
            {"row_key": "Z", "axis_value": "cn", "expected_rows": 0, "actual_rows": 0},
        ]})
        self.assertEqual(result["verdict"], ZERO_SAMPLE)

    def test_run_level_empty_axis_verdict(self):
        result = evaluate_run({**BASE, "rows": [
            {"row_key": "E", "axis_value": " ", "expected_rows": 10, "actual_rows": 10},
        ]})
        self.assertEqual(result["verdict"], EMPTY_AXIS)

    def test_all_pass_run(self):
        result = evaluate_run({**BASE, "rows": [
            {"row_key": "P1", "axis_value": "cn", "expected_rows": 1000, "actual_rows": 1005},
        ]})
        self.assertEqual(result["verdict"], PASS)
        self.assertEqual(result["abnormal_result"], [])
        self.assertEqual(result["anomaly_rows"], 0)


class InputValidationTests(unittest.TestCase):
    def test_missing_revision_rejected(self):
        with self.assertRaises(VerdictError):
            evaluate_run({**BASE, "metric_revision": "", "rows": [
                {"row_key": "X", "axis_value": "cn", "expected_rows": 1, "actual_rows": 1},
            ]})

    def test_missing_row_key_rejected(self):
        with self.assertRaises(VerdictError):
            evaluate_run({**BASE, "rows": [
                {"axis_value": "cn", "expected_rows": 1, "actual_rows": 1},
            ]})

    def test_empty_rows_rejected(self):
        with self.assertRaises(VerdictError):
            evaluate_run({**BASE, "rows": []})

    def test_bad_tolerance_rejected(self):
        with self.assertRaises(VerdictError):
            VerdictContext.from_payload({**BASE, "tolerance": "abc",
                                         "rows": [{"row_key": "X"}]})


if __name__ == "__main__":
    unittest.main()
