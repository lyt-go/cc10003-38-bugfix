"""演示场景引导：注册 v3 口径与 DATA-PAY_CHECK 验证任务。

独立于 seed.py 的通用样例数据，只在应用启动时幂等执行一次，
让修复后的异常样例（3.4% 差异 / 2% 阈值）开箱可见。测试中可手动 reset。
"""
from __future__ import annotations

import threading

from app.store import store
from app.verification import verification_service

_BOOTSTRAP_LOCK = threading.Lock()
_done = False

PAY_TASK_CODE = "DATA-PAY_CHECK"
PAY_METRIC_CODE = "PAY_METRIC"
PAY_BASELINE_DIGEST = "baseline_pay_20261012"
PAY_REVISION = "v3"
PAY_CASE_ID = "PAY-VERIFY-20261012-D47A"
# 与 checker_trace 对应的可复现样本：基准 8042、实际 8315、差异 273（3.4%）。
PAY_ROWS = [
    {
        "row_key": "PAY-VERIFY-20261012-D47A",
        "axis_value": "ALL_REGIONS",
        "expected_rows": 8042,
        "actual_rows": 8315,
    },
    {
        # 重试入口样例：region 维度缺失，单独归类 EMPTY_AXIS。
        "row_key": "EMPTY_REGION_38",
        "axis_value": "",
        "expected_rows": 0,
        "actual_rows": 0,
    },
]


def bootstrap_demo() -> None:  # pragma: no cover - 启动副作用，逻辑由服务测试覆盖
    global _done
    with _BOOTSTRAP_LOCK:
        if _done:
            return
        verification_service.register_metric_revision({
            "metric_code": PAY_METRIC_CODE,
            "metric_revision": PAY_REVISION,
            "tolerance": "0.02",
            "tolerance_mode": "absolute_ratio",
            "compare_axis": "region",
            "baseline_digest": PAY_BASELINE_DIGEST,
        })
        rows = store.rows("dataval")
        if not any(row.get("任务编号") == PAY_TASK_CODE for row in rows):
            rows.append({
                "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
                "status": "草稿",
                "pending": True,
                "abnormal": False,
                "任务编号": PAY_TASK_CODE,
                "验证数据集": "支付明细近线核对",
                "验证类型": "指标口径核对",
                "基准数据": PAY_BASELINE_DIGEST,
                "对比维度": "region",
                "差异阈值": "0.02",
                "验证周期": "每日",
                "最近验证结果": "待执行",
                "metric_code": PAY_METRIC_CODE,
            })
        _done = True
