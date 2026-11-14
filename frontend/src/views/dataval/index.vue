<template>
  <section class="page" data-module="dataval">
    <header class="page-head">
      <div>
        <h2>数据验证管理</h2>
        <p class="page-desc">近线核对指标口径：执行前冻结基准摘要、对比维度、阈值与口径版本，超阈值只能产出异常结论并逐行可追溯。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出数据验证清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>任务编号</span>
        <input v-model="keyword" placeholder="按任务编号检索" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>任务编号</th>
          <th>验证数据集</th>
          <th>对比维度</th>
          <th>差异阈值</th>
          <th>引擎结论</th>
          <th>口径版本</th>
          <th>异常行数</th>
          <th>状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td>{{ row['任务编号'] ?? '—' }}</td>
          <td>{{ row['验证数据集'] ?? '—' }}</td>
          <td>{{ row.verification?.compare_axis ?? row['对比维度'] ?? '—' }}</td>
          <td>{{ row.verification?.tolerance ?? row['差异阈值'] ?? '—' }}</td>
          <td>
            <span :class="['verdict-badge', verdictClass(row.verification?.verdict)]">
              {{ row.verification?.verdict ?? '未执行' }}
            </span>
            <span v-if="row.verification?.stale" class="stale-tag" title="指标已换版，结论按旧口径产出，需重新执行">口径过期</span>
          </td>
          <td>
            {{ row.verification?.metric_revision ?? '—' }}
            <div v-if="row.revision_status?.current_revision" class="muted-text">
              当前激活：{{ row.revision_status.current_revision }}
            </div>
          </td>
          <td>{{ row.verification?.anomaly_rows ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="openExecute(row)">执行验证</button>
            <button class="link" type="button" @click="openDetail(row)">结论详情</button>
            <button
              v-if="row.verification?.verdict && row.verification.verdict !== 'PASS'"
              class="link"
              type="button"
              @click="openRetry(row)"
            >
              缺失维度重试
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="9" class="empty-state">暂无数据验证数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据验证记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 执行验证弹窗：口径字段默认取任务/激活版本，可显式清空以模拟缺失场景 -->
    <div v-if="executeOpen" class="modal-mask" @click.self="executeOpen = false">
      <div class="modal">
        <h3>执行验证 · {{ activeRow?.['任务编号'] }}</h3>
        <label class="form-item"><span>checker_case_id</span>
          <input v-model="executeForm.checker_case_id" placeholder="如 PAY-VERIFY-20261012-D47A" /></label>
        <label class="form-item"><span>baseline_digest（留空=基准缺失）</span>
          <input v-model="executeForm.baseline_digest" placeholder="baseline_pay_20261012" /></label>
        <label class="form-item"><span>compare_axis（留空=维度缺失）</span>
          <input v-model="executeForm.compare_axis" placeholder="region" /></label>
        <label class="form-item"><span>tolerance</span>
          <input v-model="executeForm.tolerance" placeholder="0.02" /></label>
        <label class="form-item"><span>metric_revision</span>
          <input v-model="executeForm.metric_revision" placeholder="v3" /></label>
        <label class="form-item"><span>expected_rows / actual_rows（单行）</span>
          <div class="inline-inputs">
            <input v-model.number="executeForm.expected_rows" type="number" placeholder="expected_rows" />
            <input v-model.number="executeForm.actual_rows" type="number" placeholder="actual_rows" />
          </div>
        </label>
        <label class="form-item"><span>axis_value（留空=该维度缺失）</span>
          <input v-model="executeForm.axis_value" placeholder="ALL_REGIONS" /></label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="executeOpen = false">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitExecute">
            {{ submitting ? '执行中…' : '开始执行' }}
          </button>
        </div>
      </div>
    </div>

    <!-- 重试弹窗：region_missing_only，只补缺失维度 -->
    <div v-if="retryOpen" class="modal-mask" @click.self="retryOpen = false">
      <div class="modal">
        <h3>缺失维度重试 · {{ activeRow?.['任务编号'] }}</h3>
        <p class="page-desc">重试范围固定 region_missing_only，沿用原 run 的 {{ activeRow?.verification?.metric_revision }} 口径，只处理 axis_value 为空的行。</p>
        <label class="form-item"><span>原 run_id</span>
          <input :value="activeRow?.verification?.run_id" disabled /></label>
        <label class="form-item"><span>缺失行 row_key</span>
          <input v-model="retryForm.row_key" placeholder="EMPTY_REGION_38" /></label>
        <label class="form-item"><span>expected_rows / actual_rows</span>
          <div class="inline-inputs">
            <input v-model.number="retryForm.expected_rows" type="number" />
            <input v-model.number="retryForm.actual_rows" type="number" />
          </div>
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="retryOpen = false">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitRetry">提交重试</button>
        </div>
      </div>
    </div>

    <!-- 结论详情：快照四件套 + 逐行异常清单，可追溯原 revision -->
    <div v-if="detailOpen" class="modal-mask" @click.self="detailOpen = false">
      <div class="modal modal-wide">
        <h3>验证结论详情 · {{ activeRow?.['任务编号'] }}</h3>
        <div v-if="detail" class="detail-grid">
          <div><span class="muted-text">run_id</span><strong>{{ detail.run_id }}</strong></div>
          <div><span class="muted-text">verdict / reason</span><strong>{{ detail.verdict }} / {{ detail.reason }}</strong></div>
          <div><span class="muted-text">metric_revision</span><strong>{{ detail.metric_revision }}</strong></div>
          <div><span class="muted-text">tolerance（{{ detail.tolerance_mode }}）</span><strong>{{ detail.tolerance }}</strong></div>
          <div><span class="muted-text">baseline_digest</span><strong>{{ detail.baseline_digest ?? '（缺失，已归类 BASELINE_MISSING）' }}</strong></div>
          <div><span class="muted-text">compare_axis</span><strong>{{ detail.compare_axis ?? '（缺失，已归类 EMPTY_AXIS）' }}</strong></div>
          <div><span class="muted-text">baseline / actual / anomaly</span>
            <strong>{{ detail.baseline_rows }} / {{ detail.actual_rows }} / {{ detail.anomaly_rows }}</strong></div>
          <div v-if="detail.error"><span class="muted-text">error</span><strong class="error-text">{{ detail.error }}</strong></div>
        </div>
        <h4>逐行结论（abnormal_result，含 row_key 与判定原因）</h4>
        <table class="data-table">
          <thead>
            <tr><th>row_key</th><th>verdict</th><th>reason</th><th>diff_ratio</th><th>revision</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in detail?.abnormal_result ?? []" :key="item.row_key">
              <td>{{ item.row_key }}</td>
              <td><span :class="['verdict-badge', verdictClass(item.verdict)]">{{ item.verdict }}</span></td>
              <td>{{ item.reason }}</td>
              <td>{{ item.diff_ratio ?? '—' }}</td>
              <td>{{ item.metric_revision }}</td>
            </tr>
            <tr v-if="!(detail?.abnormal_result ?? []).length">
              <td colspan="5" class="empty-state">无异常行（PASS 或结构缺失待重试）</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn primary" type="button" @click="detailOpen = false">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Verdict = string | null | undefined
interface Row extends Record<string, any> {}
interface RunDetail extends Record<string, any> {}

const ENDPOINT = '/api/dataval'

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const submitting = ref(false)

const executeOpen = ref(false)
const retryOpen = ref(false)
const detailOpen = ref(false)
const activeRow = ref<Row | null>(null)
const detail = ref<RunDetail | null>(null)

const executeForm = ref({
  checker_case_id: '',
  baseline_digest: '',
  compare_axis: '',
  tolerance: '',
  metric_revision: '',
  expected_rows: 0,
  actual_rows: 0,
  axis_value: '',
})
const retryForm = ref({ row_key: '', expected_rows: 0, actual_rows: 0 })

const stats = computed(() => [
  { label: '验证任务总数', value: total.value },
  { label: '异常任务', value: rows.value.filter(r => r.abnormal).length },
  { label: '口径过期待重跑', value: rows.value.filter(r => r.verification?.stale).length },
])

function verdictClass(verdict: Verdict): string {
  if (verdict === 'PASS' || verdict === 'LEGACY_PASS') return 'verdict-pass'
  if (!verdict) return 'verdict-none'
  if (verdict === 'FAILED') return 'verdict-failed'
  return 'verdict-abnormal'
}

function resetFilters() {
  keyword.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openExecute(row: Row) {
  activeRow.value = row
  executeForm.value = {
    checker_case_id: String(row.verification?.checker_case_id ?? row['任务编号'] ?? ''),
    baseline_digest: String(row.verification?.baseline_digest ?? row['基准数据'] ?? ''),
    compare_axis: String(row.verification?.compare_axis ?? row['对比维度'] ?? ''),
    tolerance: String(row.verification?.tolerance ?? row['差异阈值'] ?? ''),
    metric_revision: String(row.verification?.metric_revision ?? ''),
    expected_rows: 0,
    actual_rows: 0,
    axis_value: '',
  }
  executeOpen.value = true
}

function openRetry(row: Row) {
  activeRow.value = row
  retryForm.value = { row_key: '', expected_rows: 0, actual_rows: 0 }
  retryOpen.value = true
}

async function openDetail(row: Row) {
  activeRow.value = row
  detail.value = null
  const runId = row.verification?.run_id
  if (!runId) {
    errorMessage.value = '该任务尚无验证运行记录'
    return
  }
  const response = await request(`${ENDPOINT}/${row.id}/verification/runs/${runId}`)
  if (!response.ok) {
    errorMessage.value = '结论详情读取失败'
    return
  }
  detail.value = await response.json()
  detailOpen.value = true
}

async function submitExecute() {
  if (!activeRow.value) return
  errorMessage.value = ''
  submitting.value = true
  try {
    const values: Record<string, unknown> = {
      checker_case_id: executeForm.value.checker_case_id || activeRow.value['任务编号'],
      // 空串显式表达「字段缺失」，后端会归类为 BASELINE_MISSING / EMPTY_AXIS
      baseline_digest: executeForm.value.baseline_digest,
      compare_axis: executeForm.value.compare_axis,
      rows: [{
        row_key: executeForm.value.checker_case_id || 'ROW-1',
        axis_value: executeForm.value.axis_value,
        expected_rows: executeForm.value.expected_rows,
        actual_rows: executeForm.value.actual_rows,
      }],
    }
    if (executeForm.value.tolerance) values.tolerance = executeForm.value.tolerance
    if (executeForm.value.metric_revision) values.metric_revision = executeForm.value.metric_revision
    const response = await request(`${ENDPOINT}/${activeRow.value.id}/verification/runs`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      // 异常结论也是正常业务返回（ok=false 表示非 PASS），消息直接展示引擎判定
      errorMessage.value = payload.message || '验证执行未通过'
    }
    executeOpen.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '验证执行失败'
  } finally {
    submitting.value = false
  }
}

async function submitRetry() {
  if (!activeRow.value) return
  errorMessage.value = ''
  submitting.value = true
  try {
    const runId = activeRow.value.verification?.run_id
    const response = await request(`${ENDPOINT}/${activeRow.value.id}/verification/runs/${runId}/retry`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          retry_scope: 'region_missing_only',
          rows: [{
            row_key: retryForm.value.row_key || 'EMPTY_REGION_38',
            axis_value: '',
            expected_rows: retryForm.value.expected_rows,
            actual_rows: retryForm.value.actual_rows,
          }],
        },
      }),
    })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      errorMessage.value = payload.message || '重试未通过'
    }
    retryOpen.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重试失败'
  } finally {
    submitting.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = keyword.value ? `?keyword=${encodeURIComponent(keyword.value)}` : ''
  try {
    const response = await request(`${ENDPOINT}${query}`)
    if (!response.ok) {
      throw new Error('验证任务列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据验证列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.verdict-badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; white-space: nowrap; }
.verdict-pass { background: #e6f7ec; color: #1a7f37; }
.verdict-abnormal { background: #fdeaea; color: #b42318; }
.verdict-failed { background: #fff4e5; color: #b45309; }
.verdict-none { background: #eef1f5; color: #64748b; }
.stale-tag { margin-left: 6px; font-size: 11px; color: #b45309; background: #fff4e5; padding: 1px 6px; border-radius: 8px; }
.muted-text { display: block; color: var(--muted); font-size: 11px; margin-bottom: 2px; }
.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; width: 460px; max-height: 86vh; overflow: auto; }
.modal-wide { width: 760px; }
.form-item { display: block; margin-bottom: 10px; }
.form-item span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.form-item input { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.inline-inputs { display: flex; gap: 8px; }
.inline-inputs input { flex: 1; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 12px; }
.detail-grid > div { background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; padding: 8px; }
.modal h4 { margin: 14px 0 8px; }
</style>
