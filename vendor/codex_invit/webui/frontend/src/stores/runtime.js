import { defineStore } from 'pinia'
import { ref } from 'vue'
import { createSSE } from '@/api/request'
import { useStatsStore } from './stats'

let _logId = 0
const MAX_LOGS = 2000

function classify(line) {
  const l = (line || '').toLowerCase()
  if (l.includes('error') || l.includes('失败') || l.includes('拒绝')) return 'err'
  if (l.includes('warning') || l.includes('warn')) return 'warn'
  if (l.includes('成功') || l.includes('完成') || l.includes('命中') || l.includes('ok')) return 'ok'
  return ''
}

// 运行时状态：全局实时日志 + 单个 run 的 SSE + 告警横幅。
// 放在 store 里是为了「切换菜单页面时日志不中断」。
//
// codex_invit：去掉了 auto-loop 那条全局流（批量至2FA 菜单已删除）。
// 运行日志统一走 `/api/runs/{id}/stream`：单次注册、CDK 邀请作业、手动 Codex 授权
// 都用它。
export const useRuntimeStore = defineStore('runtime', () => {
  const logs = ref([])            // { id, text, kind }
  const banner = ref('')          // 严重错误横幅
  const lastRunResult = ref(null)
  const dataVersion = ref(0)      // 递增：通知号池/结果/记录表刷新
  const runningSingle = ref(false)

  let currentEs = null

  function addLog(text, kind) {
    logs.value.push({ id: ++_logId, text, kind: kind ?? classify(text) })
    if (logs.value.length > MAX_LOGS) logs.value.splice(0, logs.value.length - MAX_LOGS)
  }
  function clearLogs() { logs.value = [] }
  function bumpData() { dataVersion.value++ }
  function dismissBanner() { banner.value = '' }
  function stopStream() {
    if (currentEs) { try { currentEs.close() } catch (_) {} }
    currentEs = null
    runningSingle.value = false
  }

  // ─── 单个 run 的 SSE（单次注册 / CDK 作业 / 手动 Codex 授权共用）───
  function streamRun(runId) {
    if (currentEs) { try { currentEs.close() } catch (_) {} }
    runningSingle.value = true
    const es = createSSE(`/api/runs/${runId}/stream`, {
      log: (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.line) addLog(d.line)
        } catch (_) {}
      },
      status: (e) => {
        try {
          const d = JSON.parse(e.data)
          if (d.kind === 'done') {
            lastRunResult.value = {
              email: d.email,
              password: d.password || '',
              access_token_len: d.access_token_len,
              partial: d.partial,
              // ⚠️ 2FA secret 只在这里下发一次，前端必须透传（原项目已知缺陷）
              totp_secret: d.totp_secret || '',
              attempts: d.attempts,
              message: d.message || '',
            }
            addLog(
              `完成: ${d.email}`
              + (d.password ? ' / ' + d.password : '')
              + (d.message ? ` — ${d.message}` : ''),
              'ok',
            )
          } else if (d.kind === 'error') {
            lastRunResult.value = { email: d.email, error: d.message }
            addLog('错误: ' + d.message, 'err')
          } else if (d.kind === 'phase') {
            addLog(`phase=${d.phase}${d.email ? ' email=' + d.email : ''}${d.message ? ' ' + d.message : ''}`, 'evt')
          }
        } catch (_) {}
      },
      end: () => {
        try { es.close() } catch (_) {}
        currentEs = null
        runningSingle.value = false
        useStatsStore().refresh()
        bumpData()
      },
    }, () => {
      try { es.close() } catch (_) {}
      currentEs = null
      runningSingle.value = false
    })
    currentEs = es
  }

  return {
    logs, banner, lastRunResult, dataVersion, runningSingle,
    addLog, clearLogs, bumpData, dismissBanner, streamRun, stopStream,
  }
})
