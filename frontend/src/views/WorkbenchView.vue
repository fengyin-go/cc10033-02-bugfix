<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th v-for="column in derivedFields" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="String(row.id)"
          :data-testid="`row-${row.id}`"
          :data-status="String(row.status)"
          :class="{ 'row-selected': selected?.id === row.id }"
          @click="selected = row"
        >
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td
            v-for="column in derivedFields"
            :key="column"
            :data-testid="`cell-${row.id}-${column}`"
            :class="cellClass(column, row[column])"
            :title="String(row[column] ?? '')"
          >
            {{ formatCell(row[column]) }}
          </td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click.stop="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + derivedFields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <aside v-if="selected" class="row-detail" data-testid="row-detail">
      <header class="row-detail-head">
        <strong>{{ detailTitle }}</strong>
        <button class="link" type="button" data-testid="close-detail" @click="selected = null">收起</button>
      </header>
      <dl class="row-detail-grid">
        <template v-for="item in detailItems" :key="item.label">
          <dt>{{ item.label }}</dt>
          <dd :data-testid="`detail-${item.label}`">{{ item.value }}</dd>
        </template>
      </dl>
      <p v-if="selected['校验明细']" class="row-detail-note">
        {{ selected['校验明细'] }}
      </p>
    </aside>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const selected = ref<EntryRow | null>(null)
const derivedFields = computed(() => meta.derivedFields ?? [])
const detailFields = computed(() => [
  ...meta.fields,
  'status',
  ...derivedFields.value,
  '键类型族',
  '主键发布快照',
  '发布校验通过',
  '可用列',
  '校验明细',
])
const detailItems = computed(() =>
  detailFields.value
    .filter((field) => selected.value?.[field] !== undefined)
    .map((field) => ({
      label: field === 'status' ? '当前状态' : field,
      value: formatCell(selected.value?.[field]),
    })),
)
const detailTitle = computed(() => {
  if (!selected.value) return ''
  return String(selected.value[meta.fields[0]] ?? `#${selected.value.id}`)
})

function formatCell(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (Array.isArray(value)) return value.length ? value.join('、') : '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function cellClass(column: string, value: unknown): string {
  if (column === '键类型冲突' && value === '是') return 'cell-warn'
  if (column === '发布校验' && String(value ?? '').includes('未通过')) return 'cell-warn'
  if (column === '发布校验' && String(value ?? '').includes('通过')) return 'cell-ok'
  if (column === '关联键可用') {
    if (value === '是') return 'cell-ok'
    if (value === '否') return 'cell-muted'
  }
  if (column === '审核迟到' && value === '是') return 'cell-warn'
  if (column === '快照一致' && value === '否') return 'cell-warn'
  return ''
}

// 指标名称来自模块配置（如「已发布数」「停用数」），按名称去掉「数」取出对应状态统计，
// 不再假设状态在数组中的位置。
function statusCountFor(metricLabel: string): number {
  const status = metricLabel.replace(/\s*数$/, '')
  if (!meta.statuses.includes(status)) return 0
  return rows.value.filter((row) => String(row.status) === status).length
}

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: statusCountFor(meta.metrics[1]) },
  { label: meta.metrics[2], value: statusCountFor(meta.metrics[2]) },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    if (selected.value) {
      selected.value = payload.items.find((item) => item.id === selected.value?.id) ?? null
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>
