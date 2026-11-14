"""验证指标口径判定器（纯函数，不依赖任何存储与框架）。

口径约定（tolerance_mode=absolute_ratio）：
- diff_ratio = |actual_rows - expected_rows| / expected_rows，全程用 Decimal 计算，
  避免 0.0200 这种边界值被二进制浮点误判；
- 边界规则显式化：diff_ratio <= tolerance 判 PASS（恰好等于阈值按通过处理），
  只有严格大于阈值才判 ABNORMAL；
- 结构类缺陷单独归类，绝不混进 PASS：
  * BASELINE_MISSING：执行快照里 baseline_digest 缺失；
  * EMPTY_AXIS：compare_axis 缺失，或某一行的维度取值为空；
  * ZERO_SAMPLE：expected_rows == 0，差异比例无定义，按失败处理；
- 每一行的结论都带 row_key 与 reason，abnormal_result 可逐行回溯到 metric_revision。

判定优先级（从高到低）：
BASELINE_MISSING > EMPTY_AXIS > ZERO_SAMPLE > ABNORMAL > PASS
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

# 结论常量：任务级与行级共用同一套口径，禁止各链路再自定义字符串。
PASS = "PASS"
ABNORMAL = "ABNORMAL"
ZERO_SAMPLE = "ZERO_SAMPLE"
EMPTY_AXIS = "EMPTY_AXIS"
BASELINE_MISSING = "BASELINE_MISSING"
FAILED = "FAILED"  # 仅服务层在执行本身失败时使用，判定器不会产出

VERDICTS = (PASS, ABNORMAL, ZERO_SAMPLE, EMPTY_AXIS, BASELINE_MISSING, FAILED)
# 除 PASS 外的结论都属于异常清单，读取链路据此保证「异常即有清单」。
DEFECT_VERDICTS = (ABNORMAL, ZERO_SAMPLE, EMPTY_AXIS, BASELINE_MISSING)

ABSOLUTE_RATIO = "absolute_ratio"
SUPPORTED_TOLERANCE_MODES = (ABSOLUTE_RATIO,)

# 判定原因码：行级 reason 与任务级 summary 统一取值，方便审计与前端展示。
REASON_BOUNDARY_PASS = "RATIO_WITHIN_TOLERANCE"
REASON_RATIO_ABNORMAL = "RATIO_EXCEEDS_TOLERANCE"
REASON_ZERO_SAMPLE = "ZERO_EXPECTED_ROWS"
REASON_EMPTY_AXIS = "COMPARE_AXIS_VALUE_EMPTY"
REASON_AXIS_MISSING = "COMPARE_AXIS_MISSING"
REASON_BASELINE_MISSING = "BASELINE_DIGEST_MISSING"

# 执行前必须落快照的四个口径字段，缺失任何一个都不允许进入比例判定。
SNAPSHOT_FIELDS = ("baseline_digest", "compare_axis", "tolerance", "metric_revision")


class VerdictError(ValueError):
    """入参本身不合法（区别于业务异常结论），例如 tolerance 无法解析。"""


@dataclass(frozen=True)
class VerdictContext:
    """一次验证执行的口径快照，构造后不可变，保证判定与指标定义同版本。"""

    checker_case_id: str
    baseline_digest: str | None
    compare_axis: str | None
    tolerance: Decimal
    tolerance_mode: str
    metric_revision: str
    rows: tuple[dict[str, Any], ...]
    trace: str | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "VerdictContext":
        mode = str(payload.get("tolerance_mode") or ABSOLUTE_RATIO).strip()
        if mode not in SUPPORTED_TOLERANCE_MODES:
            raise VerdictError(f"不支持的 tolerance_mode：{mode}")
        try:
            tolerance = Decimal(str(payload["tolerance"])).quantize(Decimal("0.000001"))
        except (InvalidOperation, KeyError) as exc:
            raise VerdictError("tolerance 缺失或不是合法数值") from exc
        if tolerance < 0:
            raise VerdictError("tolerance 不能为负数")
        revision = str(payload.get("metric_revision") or "").strip()
        if not revision:
            raise VerdictError("metric_revision 为空，拒绝在无口径版本下执行验证")
        rows = tuple(payload.get("rows") or ())
        if not rows:
            raise VerdictError("验证样本为空：至少需要一行对比数据")
        return cls(
            checker_case_id=str(payload.get("checker_case_id") or "").strip(),
            baseline_digest=_blank_to_none(payload.get("baseline_digest")),
            compare_axis=_blank_to_none(payload.get("compare_axis")),
            tolerance=tolerance,
            tolerance_mode=mode,
            metric_revision=revision,
            rows=rows,
            trace=_blank_to_none(payload.get("trace")),
        )


def _blank_to_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _to_count(value: Any, field: str, row_key: str) -> int:
    if isinstance(value, bool):  # bool 是 int 的子类，显式挡掉，避免 True 被当成 1
        raise VerdictError(f"行 {row_key} 的 {field} 必须是整数")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise VerdictError(f"行 {row_key} 的 {field} 缺失或不是整数") from exc
    if count < 0:
        raise VerdictError(f"行 {row_key} 的 {field} 不能为负数")
    return count


def judge_row(context: VerdictContext, row: dict[str, Any]) -> dict[str, Any]:
    """判定单行。返回结构自带 row_key、reason 与完整口径快照，可独立审计。"""
    row_key = str(row.get("row_key") or "").strip()
    if not row_key:
        raise VerdictError("存在缺少 row_key 的对比行，拒绝产出无法追溯的结论")

    base: dict[str, Any] = {
        "row_key": row_key,
        "checker_case_id": context.checker_case_id,
        "metric_revision": context.metric_revision,
        "tolerance": str(context.tolerance),
        "tolerance_mode": context.tolerance_mode,
        "compare_axis": context.compare_axis,
        "baseline_digest": context.baseline_digest,
        "expected_rows": None,
        "actual_rows": None,
        "diff_ratio": None,
        "anomaly_rows": 0,
        "verdict": None,
        "reason": None,
    }

    # 结构缺陷优先：基准摘要缺失 / 对比维度整体缺失，比例口径已经不成立。
    if context.baseline_digest is None:
        base.update(verdict=BASELINE_MISSING, reason=REASON_BASELINE_MISSING)
        return base
    if context.compare_axis is None:
        base.update(verdict=EMPTY_AXIS, reason=REASON_AXIS_MISSING)
        return base

    expected = _to_count(row.get("expected_rows"), "expected_rows", row_key)
    actual = _to_count(row.get("actual_rows"), "actual_rows", row_key)
    base["expected_rows"] = expected
    base["actual_rows"] = actual
    axis_value = _blank_to_none(row.get("axis_value"))
    base["axis_value"] = axis_value

    if axis_value is None:
        base.update(verdict=EMPTY_AXIS, reason=REASON_EMPTY_AXIS)
        return base
    if expected == 0:
        # 零样本：分母为 0，diff_ratio 无定义，单独归类为失败，不允许走比例分支。
        base.update(verdict=ZERO_SAMPLE, reason=REASON_ZERO_SAMPLE)
        return base

    diff = Decimal(abs(actual - expected))
    ratio = (diff / Decimal(expected)).quantize(Decimal("0.000001"))
    base["diff_ratio"] = str(ratio)
    base["anomaly_rows"] = int(diff)
    # 显式边界规则：等于阈值通过，严格大于阈值才异常。
    if ratio <= context.tolerance:
        base.update(verdict=PASS, reason=REASON_BOUNDARY_PASS)
    else:
        base.update(verdict=ABNORMAL, reason=REASON_RATIO_ABNORMAL)
    return base


def evaluate_run(payload: dict[str, Any]) -> dict[str, Any]:
    """执行一次完整判定，返回任务级结论与逐行结果。

    返回字段同时满足执行落库与读取链路：口径快照、汇总计数、abnormal_result
    （非 PASS 行的完整清单）以及全部行级 results，任何结论都能追到 metric_revision。
    """
    context = VerdictContext.from_payload(payload)
    row_results = [judge_row(context, row) for row in context.rows]

    verdicts = {row["verdict"] for row in row_results}
    if context.baseline_digest is None:
        run_verdict, summary = BASELINE_MISSING, REASON_BASELINE_MISSING
    elif context.compare_axis is None:
        run_verdict, summary = EMPTY_AXIS, REASON_AXIS_MISSING
    elif EMPTY_AXIS in verdicts:
        run_verdict, summary = EMPTY_AXIS, REASON_EMPTY_AXIS
    elif ZERO_SAMPLE in verdicts:
        run_verdict, summary = ZERO_SAMPLE, REASON_ZERO_SAMPLE
    elif ABNORMAL in verdicts:
        run_verdict, summary = ABNORMAL, REASON_RATIO_ABNORMAL
    else:
        run_verdict, summary = PASS, REASON_BOUNDARY_PASS

    abnormal_result = [
        dict(row) for row in row_results if row["verdict"] in DEFECT_VERDICTS
    ]
    baseline_total = sum(r["expected_rows"] or 0 for r in row_results)
    actual_total = sum(r["actual_rows"] or 0 for r in row_results)
    anomaly_rows = sum(r["anomaly_rows"] for r in row_results if r["verdict"] == ABNORMAL)

    return {
        "verdict": run_verdict,
        "reason": summary,
        "checker_case_id": context.checker_case_id,
        "baseline_digest": context.baseline_digest,
        "compare_axis": context.compare_axis,
        "tolerance": str(context.tolerance),
        "tolerance_mode": context.tolerance_mode,
        "metric_revision": context.metric_revision,
        "trace": context.trace,
        "baseline_rows": baseline_total,
        "actual_rows": actual_total,
        "anomaly_rows": anomaly_rows,
        "passed_rows": sum(1 for r in row_results if r["verdict"] == PASS),
        "abnormal_result": abnormal_result,
        "results": row_results,
    }
