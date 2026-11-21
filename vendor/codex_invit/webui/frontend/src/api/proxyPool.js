import http from './request'

// ──────────────── 服务端代理池 ────────────────
export const getProxyPool = () => http.get('/api/proxy/pool')
export const addProxyPool = (text) => http.post('/api/proxy/pool/add', { text })
export const replaceProxyPool = (text) => http.post('/api/proxy/pool/replace', { text })
export const setProxyEnabled = (payload) => http.post('/api/proxy/pool/enable', payload)
export const resetProxy = (payload) => http.post('/api/proxy/pool/reset', payload)
export const removeProxy = (payload) => http.post('/api/proxy/pool/remove', payload)
export const listProxyEvents = (proxyId = null, limit = 200) =>
  http.get('/api/proxy/pool/events', { params: { proxy_id: proxyId, limit } })

// 代理连通性测试（后端并发测试，可能耗时，单独放宽超时到 3 分钟）
export const testProxies = (proxies, timeout = 8) =>
  http.post('/api/proxy/test', { proxies, timeout }, { timeout: 180000 })

// 一键诊断：直连 / 系统代理 / 池内代理 三条路径对比
export const diagnoseProxy = () => http.post('/api/proxy/diagnose', {}, { timeout: 180000 })
