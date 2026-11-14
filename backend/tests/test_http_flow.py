"""HTTP 层集成测试：不依赖 httpx，直接用 ASGI 传输驱动 FastAPI 路由。

覆盖从请求进路由到结论落库、再读回的完整链路，确认路由层没有绕过引擎口径，
列表与详情结论同源、异常清单随结论一起返回。
"""
from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any

from app.main import app
from app.store import store
from app.verification import verification_service
from app.verification.bootstrap import PAY_TASK_CODE, bootstrap_demo


class AsgiCaller:
    """最小 ASGI 调用器：把一次 HTTP 调用翻译成 scope/receive/send。"""

    def __init__(self, asgi_app) -> None:
        self.app = asgi_app

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        raw = json.dumps(body).encode() if body is not None else b""
        if "?" in path:
            pure_path, query = path.split("?", 1)
        else:
            pure_path, query = path, ""
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": pure_path,
            "raw_path": pure_path.encode(),
            "query_string": query.encode(),
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(raw)).encode())],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        chunks: list[bytes] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": raw, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                chunks.append(("status", message["status"]))  # type: ignore[arg-type]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        asyncio.run(self.app(scope, receive, send))
        status = next(c[1] for c in chunks if isinstance(c, tuple) and c[0] == "status")
        payload = b"".join(c for c in chunks if isinstance(c, bytes))
        return status, json.loads(payload or b"null")


class HttpVerificationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        verification_service.repo.reset()
        store._tables = {name: [dict(row) for row in rows]
                         for name, rows in store._tables.items()}
        import app.verification.bootstrap as bootstrap_module
        bootstrap_module._done = False
        bootstrap_demo()
        verification_service.mark_backfill_watermark()
        self.api = AsgiCaller(app)
        self.entry_id = next(
            row["id"] for row in store.rows("dataval") if row.get("任务编号") == PAY_TASK_CODE
        )

    def test_full_http_flow_abnormal_then_readback(self):
        path = f"/api/dataval/{self.entry_id}/verification/runs"
        status, body = self.api.call("POST", path, {"values": {
            "run_id": "http-1",
            "checker_case_id": "PAY-VERIFY-20261012-D47A",
            "rows": [{"row_key": "PAY-VERIFY-20261012-D47A", "axis_value": "ALL",
                      "expected_rows": 8042, "actual_rows": 8315}],
        }})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])  # 异常时 ActionResult.ok 必须为 False
        self.assertEqual(body["entry"]["verdict"], "ABNORMAL")

        # 列表与详情读回的结论同源，且快照字段不缺
        _, listed = self.api.call("GET", f"/api/dataval?keyword={PAY_TASK_CODE}")
        row = listed["items"][0]
        self.assertEqual(row["verification"]["verdict"], "ABNORMAL")
        self.assertEqual(row["verification"]["metric_revision"], "v3")
        _, detail = self.api.call("GET", f"/api/dataval/{self.entry_id}")
        self.assertEqual(detail["verification"]["run_id"],
                         row["verification"]["run_id"])

        # 单 run 读回，异常清单可追溯
        _, run = self.api.call(
            "GET", f"/api/dataval/{self.entry_id}/verification/runs/http-1")
        self.assertEqual(run["abnormal_result"][0]["metric_revision"], "v3")
        self.assertEqual(run["abnormal_result"][0]["row_key"],
                         "PAY-VERIFY-20261012-D47A")

    def test_http_manual_pass_guard(self):
        path = f"/api/dataval/{self.entry_id}/verification/runs"
        self.api.call("POST", path, {"values": {
            "run_id": "http-2",
            "rows": [{"row_key": "X", "axis_value": "a",
                      "expected_rows": 100, "actual_rows": 200}],
        }})
        status, body = self.api.call(
            "POST", f"/api/dataval/{self.entry_id}/actions",
            {"values": {"action": "标记已验证"}})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])
        self.assertIn("禁止手工标记已验证", body["message"])

    def test_http_revision_switch_and_rollback(self):
        status, body = self.api.call("POST", "/api/metric-revisions", {"values": {
            "metric_code": "PAY_METRIC", "metric_revision": "v4",
            "tolerance": "0.05", "compare_axis": "region",
            "baseline_digest": "baseline_pay_20261012",
        }})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, body = self.api.call("POST", "/api/metric-revisions/rollback", {"values": {
            "metric_code": "PAY_METRIC", "metric_revision": "v3",
        }})
        self.assertTrue(body["ok"])
        _, revisions = self.api.call(
            "GET", "/api/metric-revisions?metric_code=PAY_METRIC")
        active = [r for r in revisions["items"] if r["active"]]
        self.assertEqual([r["revision"] for r in active], ["v3"])


if __name__ == "__main__":
    unittest.main()
