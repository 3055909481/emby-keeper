<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { CheckCircleIcon, SparklesIcon } from '@heroicons/vue/24/outline'
import UiButton from '@/components/ui/UiButton.vue'
import UiCard from '@/components/ui/UiCard.vue'
import { useAuthStore } from '@/stores/auth'

type CapabilityForm = {
  llm_enabled: boolean
  llm_base_url: string
  llm_api_key: string
  llm_model: string
  llm_vision_model: string
  llm_proxy: string
  llm_timeout: number
  captcha_enabled: boolean
  captcha_api_url: string
  captcha_api_key: string
  captcha_proxy: string
  captcha_timeout: number
}

type CapabilityResponse = CapabilityForm & { env_overrides?: string[] }
type TestTarget = 'llm' | 'captcha'
type TestResponse = { success: boolean; detail: string }

const auth = useAuthStore()

const form = reactive<CapabilityForm>({
  llm_enabled: true,
  llm_base_url: '',
  llm_api_key: '',
  llm_model: 'gpt-4o-mini',
  llm_vision_model: '',
  llm_proxy: '',
  llm_timeout: 40,
  captcha_enabled: true,
  captcha_api_url: '',
  captcha_api_key: '',
  captcha_proxy: '',
  captcha_timeout: 60,
})

const loading = ref(false)
const saving = ref(false)
const testing = reactive<Record<TestTarget, boolean>>({ llm: false, captcha: false })
const testResult = reactive<Record<TestTarget, TestResponse | null>>({ llm: null, captcha: null })
const envOverrides = ref<string[]>([])
const error = ref('')
const notice = ref('')

const inputClass =
  'block w-full rounded-[18px] border-0 bg-slate-50 px-4 py-3 text-slate-900 ring-1 ring-inset ring-slate-200 focus:ring-2 focus:ring-primary-600 disabled:bg-slate-100'

const asMessage = (e: unknown) => (e instanceof Error ? e.message : String(e))

const load = async () => {
  loading.value = true
  error.value = ''
  try {
    const data = await auth.request<CapabilityResponse>('/settings/capability')
    const { env_overrides: overrides, ...rest } = data
    Object.assign(form, rest)
    envOverrides.value = overrides || []
  } catch (e) {
    error.value = asMessage(e)
  } finally {
    loading.value = false
  }
}

const save = async () => {
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    await auth.request('/settings/capability', {
      method: 'POST',
      body: JSON.stringify(form),
    })
    notice.value = '已保存，立即生效，无需重启'
    await load()
  } catch (e) {
    error.value = asMessage(e)
  } finally {
    saving.value = false
  }
}

const test = async (target: TestTarget) => {
  testing[target] = true
  testResult[target] = null
  try {
    testResult[target] = await auth.request<TestResponse>('/settings/capability/test', {
      method: 'POST',
      body: JSON.stringify({ target }),
    })
  } catch (e) {
    testResult[target] = { success: false, detail: asMessage(e) }
  } finally {
    testing[target] = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-8">
    <section class="overflow-hidden rounded-[30px] border border-slate-200/80 bg-gradient-to-br from-white via-slate-50 to-slate-100 shadow-[0_20px_60px_-32px_rgba(15,23,42,0.35)]">
      <div class="flex min-w-0 flex-col gap-4 px-4 py-5 sm:flex-row sm:items-center sm:justify-between sm:px-6 sm:py-6 xl:px-8">
        <div class="max-w-2xl space-y-2">
          <p class="text-xs font-semibold uppercase tracking-[0.22em] text-primary-600">Capability Services</p>
          <h1 class="text-2xl font-semibold tracking-tight text-slate-950 sm:text-3xl">AI 与验证码服务</h1>
          <p class="mt-2 text-sm leading-6 text-slate-600">
            为智能答题、话术推测、图片问答和 Turnstile 验证码配置自己的服务，替代作者的云端 Bot。
          </p>
        </div>
        <UiButton size="md" class="min-w-[120px] shrink-0" :loading="saving" :disabled="loading || saving" @click="save">
          <CheckCircleIcon v-if="notice" class="h-4 w-4" />
          <span>{{ notice ? '已保存' : '保存设置' }}</span>
        </UiButton>
      </div>
    </section>

    <div v-if="error" class="rounded-[20px] border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{{ error }}</div>
    <div v-if="notice" class="rounded-[20px] border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">{{ notice }}</div>

    <div v-if="envOverrides.length" class="rounded-[20px] border border-amber-200 bg-amber-50 px-4 py-4 text-sm text-amber-800">
      <p class="font-medium">以下配置由环境变量锁定，页面上的修改不会生效：</p>
      <p class="mt-1 font-mono text-xs">{{ envOverrides.join('、') }}</p>
      <p class="mt-2 text-xs">如需改用本页配置，请删除容器中对应的环境变量并重启。</p>
    </div>

    <section class="grid min-w-0 grid-cols-1 gap-6 2xl:grid-cols-2">
      <UiCard class="h-full overflow-hidden border-slate-200/70 bg-white/90">
        <template #header>
          <div class="flex items-start justify-between gap-4">
            <div>
              <h2 class="text-lg font-semibold tracking-tight text-slate-950">智能模型（OpenAI 兼容）</h2>
              <p class="mt-1 text-sm text-slate-500">用于智能答题、题库问答、图片视觉问答与自动水群话术。</p>
            </div>
            <SparklesIcon class="h-6 w-6 shrink-0 text-primary-500" aria-hidden="true" />
          </div>
        </template>
        <div class="space-y-4">
          <label class="flex items-center justify-between gap-3 rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700">
            <span>启用智能模型</span>
            <input v-model="form.llm_enabled" type="checkbox" class="h-4 w-4 rounded border-slate-300 text-primary-600 focus:ring-primary-600" />
          </label>
          <div class="grid gap-4 sm:grid-cols-2">
            <label class="space-y-2 text-sm text-slate-600 sm:col-span-2">
              <span class="font-medium text-slate-900">服务地址（Base URL）</span>
              <input v-model="form.llm_base_url" :disabled="!form.llm_enabled" type="text" :class="inputClass" placeholder="https://api.openai.com/v1" />
              <span class="block text-xs text-slate-400">填写到 /v1 为止；也支持 Ollama、DeepSeek、one-api 等兼容端点。</span>
            </label>
            <label class="space-y-2 text-sm text-slate-600 sm:col-span-2">
              <span class="font-medium text-slate-900">API 密钥</span>
              <input v-model="form.llm_api_key" :disabled="!form.llm_enabled" type="password" :class="inputClass" placeholder="sk-...（留空表示清除）" />
              <span class="block text-xs text-slate-400">已保存的密钥显示为 ********，保持不变即可；自建端点可留空。</span>
            </label>
            <label class="space-y-2 text-sm text-slate-600">
              <span class="font-medium text-slate-900">文本模型</span>
              <input v-model="form.llm_model" :disabled="!form.llm_enabled" type="text" :class="inputClass" placeholder="gpt-4o-mini" />
            </label>
            <label class="space-y-2 text-sm text-slate-600">
              <span class="font-medium text-slate-900">视觉模型</span>
              <input v-model="form.llm_vision_model" :disabled="!form.llm_enabled" type="text" :class="inputClass" placeholder="留空则复用文本模型" />
            </label>
            <label class="space-y-2 text-sm text-slate-600">
              <span class="font-medium text-slate-900">代理（可选）</span>
              <input v-model="form.llm_proxy" :disabled="!form.llm_enabled" type="text" :class="inputClass" placeholder="http://127.0.0.1:7890" />
            </label>
            <label class="space-y-2 text-sm text-slate-600">
              <span class="font-medium text-slate-900">超时（秒）</span>
              <input v-model.number="form.llm_timeout" :disabled="!form.llm_enabled" type="number" min="1" :class="inputClass" />
            </label>
          </div>
          <div class="flex flex-wrap items-center gap-3 border-t border-slate-200/80 pt-4">
            <UiButton size="sm" variant="secondary" :loading="testing.llm" :disabled="testing.llm" @click="test('llm')">测试模型连接</UiButton>
            <span class="text-xs text-slate-400">测试使用已保存的配置，会发起一次真实模型请求。</span>
          </div>
          <p v-if="testResult.llm" class="rounded-[16px] px-3 py-2 text-sm" :class="testResult.llm.success ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'">
            {{ testResult.llm.detail }}
          </p>
        </div>
      </UiCard>

      <UiCard class="h-full overflow-hidden border-slate-200/70 bg-white/90">
        <template #header>
          <div>
            <h2 class="text-lg font-semibold tracking-tight text-slate-950">验证码求解服务</h2>
            <p class="mt-1 text-sm text-slate-500">为 7 个使用 Turnstile 验证码的签到 / 监控站点提供 token。</p>
          </div>
        </template>
        <div class="space-y-4">
          <label class="flex items-center justify-between gap-3 rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-3 text-sm text-slate-700">
            <span>启用验证码服务</span>
            <input v-model="form.captcha_enabled" type="checkbox" class="h-4 w-4 rounded border-slate-300 text-primary-600 focus:ring-primary-600" />
          </label>
          <div class="grid gap-4 sm:grid-cols-2">
            <label class="space-y-2 text-sm text-slate-600 sm:col-span-2">
              <span class="font-medium text-slate-900">服务地址</span>
              <input v-model="form.captcha_api_url" :disabled="!form.captcha_enabled" type="text" :class="inputClass" placeholder="http://your-solver:8080/solve" />
            </label>
            <label class="space-y-2 text-sm text-slate-600 sm:col-span-2">
              <span class="font-medium text-slate-900">API 密钥（可选）</span>
              <input v-model="form.captcha_api_key" :disabled="!form.captcha_enabled" type="password" :class="inputClass" placeholder="留空表示不使用鉴权" />
            </label>
            <label class="space-y-2 text-sm text-slate-600">
              <span class="font-medium text-slate-900">代理（可选）</span>
              <input v-model="form.captcha_proxy" :disabled="!form.captcha_enabled" type="text" :class="inputClass" placeholder="http://127.0.0.1:7890" />
            </label>
            <label class="space-y-2 text-sm text-slate-600">
              <span class="font-medium text-slate-900">超时（秒）</span>
              <input v-model.number="form.captcha_timeout" :disabled="!form.captcha_enabled" type="number" min="1" :class="inputClass" />
            </label>
          </div>
          <div class="rounded-[18px] border border-slate-200 bg-slate-50 px-4 py-3 text-xs leading-6 text-slate-600">
            <p class="font-medium text-slate-900">接口约定</p>
            <p class="mt-1 font-mono">POST {"site": "...", "url": "...", "kind": "token"}</p>
            <p class="mt-1 font-mono">→ {"token": "..."} 或 {"content": "..."}</p>
            <p class="mt-1">密钥会以 <span class="font-mono">Authorization: Bearer</span> 发送。</p>
          </div>
          <div class="flex flex-wrap items-center gap-3 border-t border-slate-200/80 pt-4">
            <UiButton size="sm" variant="secondary" :loading="testing.captcha" :disabled="testing.captcha" @click="test('captcha')">测试服务连通</UiButton>
            <span class="text-xs text-slate-400">会发起一次真实求解请求。</span>
          </div>
          <p v-if="testResult.captcha" class="rounded-[16px] px-3 py-2 text-sm" :class="testResult.captcha.success ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'">
            {{ testResult.captcha.detail }}
          </p>
        </div>
      </UiCard>
    </section>

    <UiCard class="border-slate-200/70 bg-white/90">
      <template #header>
        <h2 class="text-lg font-semibold tracking-tight text-slate-950">配置说明</h2>
      </template>
      <div class="space-y-3 text-sm leading-6 text-slate-600">
        <p>· 配置保存在容器内 <span class="font-mono">/app/capability.json</span>，随数据卷持久化，不写入 config.toml；保存后按文件修改时间自动重载，无需重启。</p>
        <p>· 优先级：环境变量 <span class="font-mono">EK_LLM_* / EK_CAPTCHA_*</span> &gt; 本页设置 &gt; 未配置。环境变量存在时页面上方会提示。</p>
        <p>· 图片验证码 OCR 使用镜像自带的 ddddocr，<span class="font-medium text-slate-900">无需任何配置</span>。</p>
        <p>· 未配置的能力会快速返回空值，对应站点自动跳过，不会再卡在 60 秒 × 3 次的云端重试上；普通签到与 Emby 保活不依赖这些服务。</p>
        <p>· Cloudflare 反向隧道（wssocks）的服务端在作者侧，目前没有本地替代，仅在 Emby 遇到 CF 挑战时会受影响。</p>
      </div>
    </UiCard>
  </div>
</template>
