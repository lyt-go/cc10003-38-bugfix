<template>
  <section class="page" data-module="metric">
    <header class="page-head">
      <div>
        <h2>指标口径管理</h2>
        <p class="page-desc">维护指标口径与版本：换版只追加不改写，旧验证结论保留原 revision，回滚仅切换激活版本。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openRevision">登记口径新版本</button>
        <button class="btn" type="button" @click="exportRows">导出指标口径清单</button>
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
        <span>指标编号</span>
        <input v-model="keyword" placeholder="按指标编号检索" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="keyword = ''; reload()">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" @click="runAction('提交口径评审', row)">提交口径评审</button>
            <button class="link" type="button" @click="runAction('发布指标', row)">发布指标</button>
            <button class="link" type="button" @click="viewRevisions(row)">版本记录</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无指标口径数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条指标口径记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <!-- 登记新口径版本 -->
    <div v-if="revisionOpen" class="modal-mask" @click.self="revisionOpen = false">
      <div class="modal">
        <h3>登记口径新版本</h3>
        <label class="form-item"><span>metric_code</span>
          <input v-model="revisionForm.metric_code" placeholder="如 PAY_METRIC" /></label>
        <label class="form-item"><span>metric_revision</span>
          <input v-model="revisionForm.metric_revision" placeholder="如 v4" /></label>
        <label class="form-item"><span>tolerance</span>
          <input v-model="revisionForm.tolerance" placeholder="如 0.02" /></label>
        <label class="form-item"><span>tolerance_mode</span>
          <input v-model="revisionForm.tolerance_mode" placeholder="absolute_ratio" /></label>
        <label class="form-item"><span>compare_axis</span>
          <input v-model="revisionForm.compare_axis" placeholder="region" /></label>
        <label class="form-item"><span>baseline_digest</span>
          <input v-model="revisionForm.baseline_digest" placeholder="baseline_pay_20261012" /></label>
        <p class="page-desc">新版本激活后，历史 verification_run 保留旧 revision 并标记口径过期，必须重新执行验证。</p>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="revisionOpen = false">取消</button>
          <button class="btn primary" type="button" :disabled="submitting" @click="submitRevision">登记并激活</button>
        </div>
      </div>
    </div>

    <!-- 版本记录与回滚 -->
    <div v-if="historyOpen" class="modal-mask" @click.self="historyOpen = false">
      <div class="modal modal-wide">
        <h3>口径版本记录 · {{ historyMetricCode }}</h3>
        <table class="data-table">
          <thead>
            <tr><th>revision</th><th>tolerance</th><th>axis</th><th>baseline_digest</th><th>状态</th><th>操作</th></tr>
          </thead>
          <tbody>
            <tr v-for="rev in revisionHistory" :key="rev.revision">
              <td>{{ rev.revision }}</td>
              <td>{{ rev.tolerance }}（{{ rev.tolerance_mode }}）</td>
              <td>{{ rev.compare_axis ?? '—' }}</td>
              <td>{{ rev.baseline_digest ?? '—' }}</td>
              <td>
                <span :class="rev.active ? 'tag-active' : 'tag-inactive'">
                  {{ rev.active ? '激活中' : '已停用' }}
                </span>
              </td>
              <td>
                <button v-if="!rev.active" class="link" type="button" @click="rollback(rev)">回滚到此版本</button>
                <span v-else class="muted-text">当前版本</span>
              </td>
            </tr>
            <tr v-if="!revisionHistory.length">
              <td colspan="6" class="empty-state">该指标暂无独立口径版本记录</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn primary" type="button" @click="historyOpen = false">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>
interface Revision extends Record<string, any> {}

const ENDPOINT = '/api/metric'
const REVISION_ENDPOINT = '/api/metric-revisions'
const columns = ['指标编号', '指标名称', '计算公式', '统计粒度', '时间窗口', '筛选条件', '精度位数', '口径版本']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const keyword = ref('')
const submitting = ref(false)

const revisionOpen = ref(false)
const historyOpen = ref(false)
const historyMetricCode = ref('')
const revisionHistory = ref<Revision[]>([])
const revisionForm = ref({
  metric_code: '',
  metric_revision: '',
  tolerance: '',
  tolerance_mode: 'absolute_ratio',
  compare_axis: '',
  baseline_digest: '',
})

const stats = ref([
  { label: '指标定义总数', value: 0 },
  { label: '已发布指标', value: 0 },
  { label: '口径评审中', value: 0 },
])

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openRevision() {
  revisionForm.value = {
    metric_code: '', metric_revision: '', tolerance: '',
    tolerance_mode: 'absolute_ratio', compare_axis: '', baseline_digest: '',
  }
  revisionOpen.value = true
}

async function submitRevision() {
  errorMessage.value = ''
  submitting.value = true
  try {
    const response = await request(REVISION_ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { ...revisionForm.value } }),
    })
    const payload = await response.json()
    if (!response.ok || payload.ok === false) {
      errorMessage.value = payload.message || '口径版本登记失败'
    } else {
      revisionOpen.value = false
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '口径版本登记失败'
  } finally {
    submitting.value = false
  }
}

async function viewRevisions(row: Row) {
  historyMetricCode.value = String(row['指标编号'] || '')
  revisionHistory.value = []
  // 指标表编号与验证侧 metric_code 可能不同，优先按指标编号查，查不到再给全量
  const response = await request(`${REVISION_ENDPOINT}?metric_code=${encodeURIComponent(historyMetricCode.value)}`)
  if (response.ok) {
    revisionHistory.value = (await response.json()).items ?? []
  }
  historyOpen.value = true
}

async function rollback(rev: Revision) {
  errorMessage.value = ''
  const response = await request(`${REVISION_ENDPOINT}/rollback`, {
    method: 'POST',
    body: JSON.stringify({
      values: { metric_code: rev.metric_code, metric_revision: rev.revision },
    }),
  })
  const payload = await response.json()
  if (!response.ok || payload.ok === false) {
    errorMessage.value = payload.message || '回滚失败'
    return
  }
  await viewRevisions({ '指标编号': rev.metric_code })
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('指标口径动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标口径操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = keyword.value ? `?keyword=${encodeURIComponent(keyword.value)}` : ''
  try {
    const response = await request(`${ENDPOINT}${query}`)
    if (!response.ok) {
      throw new Error('指标定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((r: Row) => r.status === '已发布').length
    stats.value[2].value = rows.value.filter((r: Row) => r.status === '口径评审中').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '指标口径列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; width: 460px; max-height: 86vh; overflow: auto; }
.modal-wide { width: 760px; }
.form-item { display: block; margin-bottom: 10px; }
.form-item span { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.form-item input { width: 100%; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 14px; }
.tag-active { background: #e6f7ec; color: #1a7f37; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.tag-inactive { background: #eef1f5; color: #64748b; padding: 2px 8px; border-radius: 10px; font-size: 12px; }
.muted-text { color: var(--muted); font-size: 12px; }
</style>
