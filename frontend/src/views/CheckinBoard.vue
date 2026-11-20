<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  ArrowPathIcon,
  CheckCircleIcon,
  ClockIcon,
  XCircleIcon,
  MinusCircleIcon,
  QuestionMarkCircleIcon,
} from '@heroicons/vue/24/outline'
import UiBadge from '@/components/ui/UiBadge.vue'
import UiButton from '@/components/ui/UiButton.vue'
import UiCard from '@/components/ui/UiCard.vue'
import { useAuthStore } from '@/stores/auth'

type CheckinSiteRow = {
  phone: string
  phone_masked: string
  site: string
  status: string
  time: string | null
  detail: string
  points: number | string | null
  increase: number | string | null
}

type CheckinBoardResponse = { items: CheckinSiteRow[]; total: number }

const auth = useAuthStore()
const rows = ref<CheckinSiteRow[]>([])
const loading = ref(false)
const error = ref('')
const updatedAt = ref<string | null>(null)

type StatusKind = 'success' | 'checked' | 'failed' | 'pending' | 'unknown'
const STATUS_META: Record<StatusKind, { label: string; variant: 'success' | 'info' | 'error' | 'warning' | 'default' }> = {
  success: { label: '签到成功', variant: 'success' },
  checked: { label: '今日已签到', variant: 'info' },
  failed: { label: '签到失败', variant: 'error' },
  pending: { label: '等待重试', variant: 'warning' },
  unknown: { label: '未知', variant: 'default' },
}

function statusOf(row: CheckinSiteRow): StatusKind {
  return row.status in STATUS_META ? (row.status as StatusKind) : 'unknown'
}

function formatTime(value: string | null): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function displayPoints(value: number | string | null): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function formatIncrease(value: number | string | null): string {
  if (value === null || value === undefined || value === '') return '—'
  const text = String(value)
  return text.startsWith('+') || text.startsWith('-') ? text : `+${text}`
}

const summary = computed(() => {
  const items = rows.value
  return {
    total: items.length,
    success: items.filter((r) => statusOf(r) === 'success').length,
    failed: items.filter((r) => statusOf(r) === 'failed').length,
    checked: items.filter((r) => statusOf(r) === 'checked').length,
  }
})

const fetchBoard = async () => {
  loading.value = true
  error.value = ''
  try {
    const data = await auth.request<CheckinBoardResponse>('/ops/checkin-sites')
    rows.value = data.items || []
    updatedAt.value = new Date().toLocaleTimeString('zh-CN')
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(fetchBoard)

function statusIcon(status: StatusKind) {
  if (status === 'success') return CheckCircleIcon
  if (status === 'failed') return XCircleIcon
  if (status === 'checked') return ClockIcon
  if (status === 'pending') return ClockIcon
  return QuestionMarkCircleIcon
}
</script>

<template>
  <div class="space-y-6">
    <UiCard>
      <div class="flex flex-wrap items-center justify-between gap-4">
        <div class="flex items-center gap-3">
          <MinusCircleIcon class="h-7 w-7 text-primary-600" aria-hidden="true" />
          <div>
            <h1 class="text-2xl font-semibold tracking-tight text-slate-950">签到看板</h1>
            <p class="mt-0.5 text-sm text-slate-500">各站点的签到状态与当前积分概览</p>
          </div>
        </div>
        <div class="flex items-center gap-3">
          <span v-if="updatedAt" class="text-xs text-slate-400">更新于 {{ updatedAt }}</span>
          <UiButton size="sm" variant="secondary" :loading="loading" @click="fetchBoard">
            <ArrowPathIcon class="h-4 w-4" aria-hidden="true" />
            刷新
          </UiButton>
        </div>
      </div>

      <div class="mt-5 grid grid-cols-2 gap-3 sm:grid-cols-4">
        <div class="rounded-[16px] border border-slate-200 bg-slate-50 px-4 py-3">
          <p class="text-xs text-slate-500">站点总数</p>
          <p class="mt-1 text-2xl font-semibold text-slate-900">{{ summary.total }}</p>
        </div>
        <div class="rounded-[16px] border border-emerald-200 bg-emerald-50 px-4 py-3">
          <p class="text-xs text-emerald-600">签到成功</p>
          <p class="mt-1 text-2xl font-semibold text-emerald-700">{{ summary.success }}</p>
        </div>
        <div class="rounded-[16px] border border-rose-200 bg-rose-50 px-4 py-3">
          <p class="text-xs text-rose-600">签到失败</p>
          <p class="mt-1 text-2xl font-semibold text-rose-700">{{ summary.failed }}</p>
        </div>
        <div class="rounded-[16px] border border-sky-200 bg-sky-50 px-4 py-3">
          <p class="text-xs text-sky-600">今日已签到</p>
          <p class="mt-1 text-2xl font-semibold text-sky-700">{{ summary.checked }}</p>
        </div>
      </div>
    </UiCard>

    <UiCard>
      <div v-if="error" class="rounded-[16px] bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</div>
      <div v-else-if="loading && !rows.length" class="space-y-2">
        <div v-for="n in 4" :key="n" class="h-14 animate-pulse rounded-[16px] bg-slate-100" />
      </div>
      <div v-else-if="!rows.length" class="rounded-[16px] bg-slate-50 px-4 py-10 text-center text-sm text-slate-500">
        还没有签到记录。站点完成一次签到后，状态与积分会显示在这里。
      </div>
      <div v-else class="overflow-x-auto">
        <table class="w-full min-w-[640px] text-left text-sm">
          <thead>
            <tr class="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-400">
              <th class="px-3 py-3 font-medium">账号</th>
              <th class="px-3 py-3 font-medium">站点</th>
              <th class="px-3 py-3 font-medium">状态</th>
              <th class="px-3 py-3 font-medium">当前积分</th>
              <th class="px-3 py-3 font-medium">本次增减</th>
              <th class="px-3 py-3 font-medium">上次签到</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr v-for="(row, index) in rows" :key="`${row.phone}-${row.site}-${index}`" class="hover:bg-slate-50/70">
              <td class="px-3 py-3 font-mono text-xs text-slate-600">{{ row.phone_masked || '—' }}</td>
              <td class="px-3 py-3 font-medium text-slate-900">{{ row.site }}</td>
              <td class="px-3 py-3">
                <UiBadge :variant="STATUS_META[statusOf(row)].variant" size="sm">
                  <component :is="statusIcon(statusOf(row))" class="mr-1 inline-block h-3.5 w-3.5" aria-hidden="true" />
                  {{ STATUS_META[statusOf(row)].label }}
                </UiBadge>
                <p v-if="row.detail && statusOf(row) === 'failed'" class="mt-1 text-xs text-slate-400">{{ row.detail }}</p>
              </td>
              <td class="px-3 py-3">
                <span
                  :class="displayPoints(row.points) === '—' ? 'text-slate-300' : 'font-semibold text-slate-900'"
                >{{ displayPoints(row.points) }}</span>
              </td>
              <td class="px-3 py-3">
                <span
                  :class="[
                    formatIncrease(row.increase) === '—'
                      ? 'text-slate-300'
                      : formatIncrease(row.increase).startsWith('-')
                        ? 'text-rose-600'
                        : 'text-emerald-600',
                  ]"
                >{{ formatIncrease(row.increase) }}</span>
              </td>
              <td class="px-3 py-3 text-xs text-slate-500">{{ formatTime(row.time) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </UiCard>

    <p class="px-1 text-xs text-slate-400">
      积分由签到成功的返回消息自动解析；未提供积分的站点显示为「—」，不影响签到功能。数据随每次签到完成自动更新。
    </p>
  </div>
</template>
