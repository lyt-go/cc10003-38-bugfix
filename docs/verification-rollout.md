# 近线验证判定器上线、回填与回滚说明

## 1. 问题与修复口径

线上故障（DATA-PAY_CHECK：diff_ratio=0.034、tolerance=0.02 却显示已验证；retry 把
anomaly_rows 覆盖为 0；读取时 baseline_digest / compare_axis 为空）的根因与对应修复：

| 根因 | 修复点 |
| --- | --- |
| 判定口径不统一，超过阈值也能被手工/重试刷成 passed | 判定统一收敛到 `app/verification/engine.py`，路由与服务层无 verdict 决策权；`DatavalService.run_action` 对非 PASS 引擎结论拦截「标记已验证」 |
| 执行前未冻结口径，读取链路字段丢失 | 执行前先写 RUNNING 快照（baseline_digest、compare_axis、tolerance、metric_revision），读取只认快照（`service.execute` + `repository.save_snapshot`） |
| 边界规则模糊、零样本/缺失维度混入通过 | Decimal 计算；`diff_ratio <= tolerance` 判 PASS（0.0200 通过），严格大于判 ABNORMAL（0.0201 异常）；ZERO_SAMPLE、EMPTY_AXIS、BASELINE_MISSING 单独归类 |
| retry 覆盖异常清单 | 重试生成独立 run（`attempt_of` 指回原 run），`region_missing_only` 只处理缺失维度行；终态记录不可二次改写 |
| 换版后旧结论被新口径解读 | run 永久绑定执行时的 metric_revision；换版只追加新版本并把旧 run 标记 stale，新 revision 必须重新执行；跨版本重试直接拒绝 |
| 重复触发/并发写入产生多结论或半成品 | entry 级互斥锁 + (entry_id, run_id) 唯一幂等键；FAILED 是唯一可同 run_id 复活的中间态，失败停在「验证中」且不产生 passed |

## 2. 代码链路（验证执行 → 指标读取）

```
POST /api/dataval/{id}/verification/runs
  -> routers/dataval.py（只编排，不判定）
  -> verification/service.py.execute
       1. repo.lock_for(entry_id) 加任务级互斥锁
       2. _resolve_snapshot 合并「请求覆盖 > 激活 revision > 任务字段」，冻结四件套
       3. repo.save_snapshot 落 RUNNING（幂等：终态直接返回旧记录）
       4. engine.evaluate_run 纯函数判定（Decimal、边界、结构缺陷归类、逐行 row_key/reason）
       5. repo.complete_run 落终态（不可改写）/ mark_failed 落 FAILED
       6. _sync_entry_status 回写任务状态（非 PASS 不可能显示已验证）
GET /api/dataval（列表）与 GET /api/dataval/{id}（详情）
  -> DatavalService -> verification_service.decorate_entry（同源视图，含快照与 abnormal_result）
GET /api/dataval/{id}/verification/runs[/{run_id}]
  -> 直接读 run，快照字段与逐行清单随结论返回，可追到 metric_revision
```

## 3. 走现有数据流水线与构建上线流程

1. **代码合并**：后端改动（`app/verification/*`、dataval 服务/路由、metric_revision 路由）
   与前端改动（dataval、metric 页面）随正常 PR 评审合并；CI 执行
   `make test-backend` 与前端 `npm run build`（vue-tsc 类型检查 + 构建）。
2. **数据库变更**：`backend/migrations/001_verification_run.sql` 纳入现有迁移工具
   （Flyway/Liquibase 等）随发布窗口执行，先建 `metric_revision`、`verification_run` 两张表。
3. **镜像构建**：沿用现有 `backend/Dockerfile`、`frontend/Dockerfile`，无新系统依赖；
   判定器只用标准库（Decimal、threading），requirements 不新增包。
4. **部署顺序**：先执行迁移（只建新表、不动旧表），再发布后端，最后发布前端。
   新接口为纯增量，旧版本实例不读新表，可灰度、可并存。
5. **启动引导**：后端启动时 `ensure_backfill()` 回填存量、`mark_backfill_watermark()`
   立水位，随后 `bootstrap_demo()` 幂等注册 v3 口径与 PAY_CHECK 任务。

## 4. 存量验证结果回填兼容（不丢历史异常）

- 回填**只追加不重算**：每个历史 dataval 任务补一条 `run_id = legacy-{id}` 的记录，
  `verdict` 为 `LEGACY_PASS` / `LEGACY_ABNORMAL`，历史异常任务的 abnormal_result
  自带 `row_key=legacy-{id}` 与原因 `LEGACY_BACKFILL`，历史异常一条不丢。
- **幂等**：以 `(entry_id, run_id)` 唯一约束兜底，重复执行迁移/重复启动不会产生重复记录；
  SQL 文件末尾附带校验查询，返回 0 行才表示所有历史异常都已回填。
- **水位隔离**：应用启动首轮回填后立水位，之后新建/引导的任务不会被误标成 LEGACY，
  必须经过真实执行链路才有结论。
- **字段兼容**：历史任务没有 metric_revision 的统一记为 `legacy`；旧表结构保持不动，
  新结论全部落在新表，回滚旧版本代码时旧表读写不受影响。
- 内存实现对应 `VerificationService.ensure_backfill()`，数据库实现对应迁移脚本第 3 段，
  两者口径一致，测试 `BackfillTests` 锁定行为。

## 5. 指标换版与回滚

- 换版：`POST /api/metric-revisions` 只追加新 revision 并切换 active 指针；
  旧 verification_run 的 metric_revision 与结论原样保留，列表显示「口径过期」标记，
  必须用新 revision 重新执行才能产生新结论。
- 回滚：`POST /api/metric-revisions/rollback` 仅把 active 指针切回指定 revision，
  **不改写任何历史 run**；回滚后按当前激活口径，较新 revision 的 run 会显示 stale。
- 数据回滚（代码回退）：迁移只建新表，回退旧版本代码无需删表；若需清理，
  按迁移逆序手工 drop 新表即可，旧 dataval 表数据不受影响。

## 6. 验收覆盖（后端 36 个用例，标准库 unittest，无额外依赖）

```
cd backend && .venv/bin/python -m unittest discover -s tests
```

- 边界：diff_ratio=0.0200 通过、0.0201 异常、浮点边界 Decimal 稳定；
- 零样本：expected_rows=0 -> ZERO_SAMPLE 失败；
- 缺失维度：compare_axis 整体缺失 / 行级 axis_value 为空 -> EMPTY_AXIS；
- 缺失基准：baseline_digest 缺失 -> BASELINE_MISSING（显式空值不被激活口径补回）；
- 换版：旧 run 保留 v3 且 stale，新 revision 重新执行，跨版本重试被拒；
- 回滚：active 指针切回，历史 run 结论与 revision 不变；
- 并发/幂等：8 线程同 run_id 只落一条完整终态；重复触发返回同一结论；
- 失败态：空样本等 FAILED 不留 passed、不缺异常清单，同 run_id 可复活；
- 回填：LEGACY_ABNORMAL 保留历史异常且幂等；列表与详情结论同源；
- 可追溯：每个 abnormal_result 行均含 row_key、reason、metric_revision、baseline_digest。
