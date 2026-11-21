import http from './request'

// ──────────────── 接码价格（5s 任务） ────────────────
export const getSmsPrices = () => http.get('/api/sms/prices')
export const refreshSmsPrices = () => http.post('/api/sms/prices/refresh')
export const smsSelftest = () => http.post('/api/sms/selftest', {}, { timeout: 120000 })
export const applySmsBest = () => http.post('/api/sms/config/apply_best')

// ──────────────── 卡密 ────────────────
export const listCdk = (params) => http.get('/api/cdk/list', { params })
export const cdkStats = () => http.get('/api/cdk/stats')
export const generateCdk = (payload) => http.post('/api/cdk/generate', payload)
export const cdkSkus = () => http.get('/api/cdk/skus')
export const cdkExportFormats = () => http.get('/api/cdk/export/formats')
export const cdkExport = (payload) => http.post('/api/cdk/export', payload)
export const cdkRecords = (code) => http.get('/api/cdk/records', { params: { code } })
export const cdkReset = (code) => http.post('/api/cdk/reset', { code })
export const cdkVoid = (code) => http.post('/api/cdk/void', { code })
export const cdkUnbind = (code) => http.post('/api/cdk/unbind', { code })
export const cdkUpdate = (payload) => http.post('/api/cdk/update', payload)
export const cdkDelete = (codes) => http.post('/api/cdk/delete', { codes })

// ──────────────── 概览 / 邀请设置 / 依赖自检 ────────────────
export const getOverview = () => http.get('/api/overview')
export const getInviteConfig = () => http.get('/api/settings/invite')
export const saveInviteConfig = (payload) => http.post('/api/settings/invite', payload)
export const runtimeCheck = () => http.get('/api/settings/runtime_check', { timeout: 60000 })
export const getMailPool = (limit = 50) => http.get('/api/mail/pool', { params: { limit } })

// 实时执行监控：正在跑/排队的作业 + 最近事件
export const cdkMonitor = (limit = 40) => http.get('/api/cdk/monitor', { params: { limit } })
