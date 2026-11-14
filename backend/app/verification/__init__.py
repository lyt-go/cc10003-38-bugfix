"""近线数据验证：判定引擎、运行记录仓库与编排服务。

模块划分：
- engine：纯函数判定器，不依赖存储，边界/零样本/缺失维度/缺失基准全部在这里归口；
- repository：verification_run 持久化（当前为内存实现，接口按数据库表设计），
  负责幂等键、按任务加锁和并发写入唯一化；
- service：验证执行、定向重试、指标换版、存量回填与读取视图的编排。
"""
from __future__ import annotations

from app.verification.engine import (
    ABSOLUTE_RATIO,
    VERDICTS,
    VerdictError,
    evaluate_run,
    judge_row,
)
from app.verification.repository import VerificationRepository, verification_repo
from app.verification.service import VerificationService, verification_service

__all__ = [
    "ABSOLUTE_RATIO",
    "VERDICTS",
    "VerdictError",
    "evaluate_run",
    "judge_row",
    "VerificationRepository",
    "verification_repo",
    "VerificationService",
    "verification_service",
]
