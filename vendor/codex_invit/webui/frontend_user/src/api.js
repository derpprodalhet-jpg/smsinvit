import axios from 'axios'
import { ElMessage } from 'element-plus'

// 同源（用户前台自己伺服页面和接口），所以 baseURL 留空。
const http = axios.create({
  baseURL: '',
  headers: { 'Content-Type': 'application/json' },
  timeout: 60000,
})

http.interceptors.response.use(
  (resp) => resp.data,
  (error) => {
    const data = error?.response?.data
    const detail = data?.detail || data?.message || error?.response?.statusText || error?.message || '请求失败'
    const err = new Error(detail)
    err.status = error?.response?.status
    err.data = data
    return Promise.reject(err)
  },
)

export default http

export const getMeta = () => http.get('/api/u/meta')
export const redeem = (codes) => http.post('/api/u/redeem', { codes })
export const markInviteSent = (code) => http.post('/api/u/invite_sent', { code })
export const startJob = (code) => http.post('/api/u/start', { code })
export const getStatus = (codes) =>
  http.get('/api/u/status', { params: { codes: codes.join(',') } })

export async function copyText(text) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
    } else {
      const ta = document.createElement('textarea')
      ta.value = text
      ta.style.cssText = 'position:fixed;left:-9999px'
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    ElMessage.success('已复制到剪贴板')
    return true
  } catch (e) {
    ElMessage.error('复制失败: ' + e.message)
    return false
  }
}

// ── 本地记住兑换过的卡密（刷新页面后还在） ──
const KEY = 'codex_invit_user_codes_v1'

export function loadMine() {
  try {
    const arr = JSON.parse(localStorage.getItem(KEY) || '[]')
    return Array.isArray(arr) ? arr.filter((x) => x && x.code) : []
  } catch (_) {
    return []
  }
}

export function saveMine(list) {
  try {
    localStorage.setItem(KEY, JSON.stringify(list.slice(0, 200)))
  } catch (_) {
    /* ignore */
  }
}

export function upsertMine(item) {
  const list = loadMine()
  const i = list.findIndex((x) => x.code === item.code)
  if (i >= 0) list[i] = { ...list[i], ...item }
  else list.unshift({ ...item })
  saveMine(list)
  return list
}
