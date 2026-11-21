import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { getProxyPool, addProxyPool, replaceProxyPool } from '@/api/proxyPool'

// ────────────────────────────────────────────────────────────
// 代理池改成**服务端**存储（proxy_pool 表）。
//
// 为什么必须搬：CDK 邀请作业跑在服务端（用户前台 5094 进程），
// 浏览器 localStorage 里的代理它根本看不到。既然作业要用代理，
// 池子就得在服务端；顺带还能记「用了多少次 / 409 几次 / 冻结到什么时候」。
//
// 这里只保留归一化等纯函数给页面做本地预览用。
// ────────────────────────────────────────────────────────────

const PROXY_AUTH_RE = /^(?<user>[^:@/\s]+):(?<password>[^@/\s]+)@(?<host>[^:/\s]+):(?<port>\d+)$/i
const PROXY_LEGACY_RE = /^(?<host>[^:@/\s]+):(?<port>\d+):(?<user>[^:@/\s]+):(?<password>[^@/\s]+)$/i
const PROXY_HOSTPORT_RE = /^(?<host>[^:/\s]+):(?<port>\d+)$/i

export function normalizeProxy(p) {
  const raw = String(p ?? '').trim()
  if (!raw) return ''

  let scheme = ''
  let rest = raw
  if (raw.includes('://')) {
    const parts = raw.split('://')
    scheme = parts.shift().toLowerCase()
    rest = parts.join('://')
    if (scheme === 'socks5') scheme = 'socks5h'
  }

  let authority = null
  if (PROXY_AUTH_RE.test(rest)) {
    authority = rest
  } else {
    const m = PROXY_LEGACY_RE.exec(rest)
    if (m?.groups) {
      authority = m.groups.user + ':' + m.groups.password + '@' + m.groups.host + ':' + m.groups.port
    } else if (PROXY_HOSTPORT_RE.test(rest)) {
      authority = rest
    }
  }

  if (!authority) return ''
  return scheme ? scheme + '://' + authority : 'http://' + authority
}

export function isValidProxy(p) {
  return Boolean(normalizeProxy(p))
}

export function proxyScheme(p) {
  const normalized = normalizeProxy(p)
  const m = /^(socks5h?|socks4|https?):\/\//i.exec(normalized)
  return m ? m[1].toLowerCase() : 'http(默认)'
}

export function parseProxyLines(s) {
  return String(s || '').split('\n').map((x) => x.trim()).filter(Boolean)
}

export const STATUS_LABEL = {
  ready: '可用',
  frozen: '冻结中',
  dead: '永久不可用',
  disabled: '已禁用',
}
export const STATUS_TYPE = {
  ready: 'success',
  frozen: 'warning',
  dead: 'danger',
  disabled: 'info',
}

const LEGACY_KEY = 'dango_proxy_pool_v1'

export const useProxyStore = defineStore('proxy', () => {
  const items = ref([])          // proxy_pool 表原样
  const summary = ref({ ready: 0, frozen: 0, dead: 0, disabled: 0, total: 0, available: 0 })
  const config = ref({ threshold: 2, freeze_minutes: 20, attempts_limit: 3 })
  const loading = ref(false)

  const count = computed(() => items.value.length)
  const usableCount = computed(() => items.value.filter((x) => x.status === 'ready').length)
  const list = computed(() => items.value.map((x) => x.url))
  const text = computed(() => items.value.map((x) => x.url).join('\n'))

  async function refresh() {
    loading.value = true
    try {
      const r = await getProxyPool()
      items.value = r.items || []
      summary.value = r.summary || summary.value
      config.value = r.config || config.value
    } finally {
      loading.value = false
    }
  }

  async function setFromText(s) {
    const r = await replaceProxyPool(s)
    await refresh()
    return { parsed: r.parsed, added: r.added, skipped: r.skipped, kept: r.skipped }
  }

  async function append(s) {
    const r = await addProxyPool(s)
    await refresh()
    return { added: r.added, skipped: r.skipped }
  }

  /** 一次性把旧版本存在浏览器里的代理池导到服务端。 */
  function legacyText() {
    try {
      const saved = JSON.parse(localStorage.getItem(LEGACY_KEY) || '[]')
      if (Array.isArray(saved) && saved.length) return saved.join('\n')
    } catch (_) { /* ignore */ }
    return ''
  }

  function clearLegacy() {
    try { localStorage.removeItem(LEGACY_KEY) } catch (_) { /* ignore */ }
  }

  return {
    items, summary, config, loading, count, usableCount, list, text,
    refresh, setFromText, append, legacyText, clearLegacy,
  }
})
