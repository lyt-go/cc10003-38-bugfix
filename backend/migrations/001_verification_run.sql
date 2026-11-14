-- 近线数据验证口径判定：数据库表结构迁移（PostgreSQL 方言示例）。
-- 对应内存仓库 app/verification/repository.py 的字段与唯一约束，
-- 上线时按现有数据流水线的迁移工具（Flyway/Liquibase/自研）顺序执行。

-- 1. 指标口径版本表：换版只追加，active 指针决定当前生效 revision。
CREATE TABLE IF NOT EXISTS metric_revision (
    id               BIGSERIAL PRIMARY KEY,
    metric_code      VARCHAR(64)  NOT NULL,
    revision         VARCHAR(32)  NOT NULL,
    tolerance        VARCHAR(32)  NOT NULL,           -- 用字符串存 Decimal，杜绝浮点边界误差
    tolerance_mode   VARCHAR(32)  NOT NULL DEFAULT 'absolute_ratio',
    compare_axis     VARCHAR(64),
    baseline_digest  VARCHAR(128),
    status           VARCHAR(32)  NOT NULL DEFAULT 'published',
    active           BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    CONSTRAINT uq_metric_revision_code_rev UNIQUE (metric_code, revision)
);
CREATE INDEX IF NOT EXISTS idx_metric_revision_active
    ON metric_revision (metric_code) WHERE active = TRUE;

-- 2. 验证运行表：执行前先落 RUNNING 快照，终态不可改写。
CREATE TABLE IF NOT EXISTS verification_run (
    id                BIGSERIAL PRIMARY KEY,
    entry_id          BIGINT       NOT NULL,
    run_id            VARCHAR(64)  NOT NULL,          -- 幂等键，重复触发只认这一条
    attempt_of        VARCHAR(64),                    -- 重试指向首轮 run_id
    retry_scope       VARCHAR(32),                    -- 如 region_missing_only
    checker_case_id   VARCHAR(64),
    trace             TEXT,
    status            VARCHAR(32)  NOT NULL,          -- RUNNING / PASS / ABNORMAL / ... / FAILED
    verdict           VARCHAR(32)  NOT NULL,
    reason            VARCHAR(64),
    baseline_digest   VARCHAR(128),                   -- 执行前冻结，允许 NULL=基准缺失
    compare_axis      VARCHAR(64),                    -- 执行前冻结，允许 NULL=维度缺失
    tolerance         VARCHAR(32),                    -- 执行前冻结
    tolerance_mode    VARCHAR(32)  NOT NULL DEFAULT 'absolute_ratio',
    metric_revision   VARCHAR(32)  NOT NULL,          -- 结论绑定的口径版本，永不被换版改写
    baseline_rows     BIGINT,
    actual_rows       BIGINT,
    anomaly_rows      BIGINT,
    rows_submitted    INTEGER      NOT NULL DEFAULT 0,
    abnormal_result   JSONB        NOT NULL DEFAULT '[]'::jsonb,  -- 逐行异常清单（含 row_key/reason/revision）
    results           JSONB        NOT NULL DEFAULT '[]'::jsonb,
    error             TEXT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    CONSTRAINT uq_verification_run UNIQUE (entry_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_verification_run_entry ON verification_run (entry_id, created_at);
CREATE INDEX IF NOT EXISTS idx_verification_run_revision ON verification_run (metric_revision);

-- 3. 存量回填：把旧 dataval 任务映射为 LEGACY 记录，历史异常原样保留、不重算。
--    幂等：只插入 verification_run 中尚不存在 (entry_id, run_id='legacy-'||id) 的任务。
INSERT INTO verification_run (
    entry_id, run_id, attempt_of, retry_scope, checker_case_id, status, verdict, reason,
    baseline_digest, compare_axis, tolerance, tolerance_mode, metric_revision,
    baseline_rows, actual_rows, anomaly_rows, abnormal_result, results,
    created_at, finished_at, rows_submitted
)
SELECT
    d.id,
    'legacy-' || d.id,
    NULL, NULL,
    COALESCE(d.task_code, 'LEGACY-' || d.id),
    CASE WHEN d.abnormal = TRUE OR d.status = '异常' THEN 'LEGACY_ABNORMAL' ELSE 'LEGACY_PASS' END,
    CASE WHEN d.abnormal = TRUE OR d.status = '异常' THEN 'LEGACY_ABNORMAL' ELSE 'LEGACY_PASS' END,
    'LEGACY_BACKFILL',
    d.baseline_ref,
    d.compare_axis,
    d.tolerance_value,
    'absolute_ratio',
    COALESCE(d.metric_revision, 'legacy'),
    NULL, NULL, NULL,
    CASE WHEN d.abnormal = TRUE OR d.status = '异常'
         THEN jsonb_build_array(jsonb_build_object(
                'row_key', 'legacy-' || d.id,
                'verdict', 'LEGACY_ABNORMAL',
                'reason', 'LEGACY_BACKFILL',
                'metric_revision', COALESCE(d.metric_revision, 'legacy')))
         ELSE '[]'::jsonb END,
    '[]'::jsonb,
    now(), now(), 0
FROM dataval d
WHERE NOT EXISTS (
    SELECT 1 FROM verification_run v
    WHERE v.entry_id = d.id AND v.run_id = 'legacy-' || d.id
);

-- 4. 回填校验：历史异常任务必须都能在 verification_run 找到对应 LEGACY_ABNORMAL。
--    上线后人工/流水线核对，返回 0 行才算通过，防止丢失历史异常。
SELECT d.id, d.task_code
FROM dataval d
WHERE (d.abnormal = TRUE OR d.status = '异常')
  AND NOT EXISTS (
      SELECT 1 FROM verification_run v
      WHERE v.entry_id = d.id AND v.verdict = 'LEGACY_ABNORMAL'
  );
