import { defineStore } from 'pinia'
import { reactive, watch } from 'vue'

const KEY = 'codex_invit_form_v1'

// 跨页面共享 + localStorage 持久化的表单字段
const defaults = {
  proxy: '',
  otpTimeout: 10,
  // 单次注册后是否自动绑 2FA。CDK 邀请作业有自己的开关（「邀请与执行」页），
  // 这里只管单次注册。
  want2fa: true,
}

// el-select 的 clearable 清空时把值写成 undefined（不是 ''），
// 而 proxy 在多处是 `form.value.proxy.trim()` 直接调 —— 统一兜底成字符串。
export function proxyText(form) {
  return String(form?.proxy ?? '').trim()
}

export const useFormStore = defineStore('form', () => {
  let saved = {}
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') } catch (_) { saved = {} }
  const form = reactive({ ...defaults, ...saved })

  watch(() => form.proxy, (v) => {
    if (v === undefined || v === null) form.proxy = ''
  })

  watch(form, (v) => {
    try { localStorage.setItem(KEY, JSON.stringify(v)) } catch (_) {}
  }, { deep: true })

  return { form }
})
