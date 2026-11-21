<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listCdk, cdkStats, generateCdk, cdkExportFormats, cdkExport, cdkRecords,
  cdkReset, cdkVoid, cdkUnbind, cdkDelete, cdkUpdate, cdkMonitor, cdkSkus,
} from '@/api/cdk'
import { copyText, fmtTime } from '@/api/request'
import { useRuntimeStore } from '@/stores/runtime'
import StatusDot from '@/components/StatusDot.vue'

const { dataVersion } = storeToRefs(useRuntimeStore())

const PAGE_SIZE = 20
const rows = ref([])
const total = ref(0)
const page = ref(1)
const status = ref('')
const batch = ref('')
const search = ref('')
const batches = ref([])
const stats = ref({})
const selected = ref([])
const loading = ref(false)

const STATUS_OPTIONS = [
  { label: '全部', value: '' },
  { label: '未使用', value: 'unused' },
  { label: '已兑换', value: 'bound' },
  { label: '排队中', value: 'queued' },
  { label: '执行中', value: 'running' },
  { label: '已完成', value: 'done' },
  { label: '失败', value: 'failed' },
  { label: '已作废', value: 'void' },
]
const STATUS_LABEL = {
  unused: '未使用', bound: '已兑换', queued: '排队中', running: '执行中',
  done: '已完成', failed: '失败', void: '已作废',
}
const STATUS_TYPE = {
  unused: 'info', bound: 'primary', queued: 'warning', running: 'warning',
  done: 'success', failed: 'danger', void: 'info',
}

// ── 生成 ──
const genVisible = ref(false)
const genForm = ref({ count: 10, batch: '', prefix: 'CDK-', length: 20, note: '', skuId: '' })
const skus = ref([])
watch(genVisible, async (visible) => {
  if (!visible) return
  try {
    const result = await cdkSkus()
    skus.value = result.items || []
    if (!skus.value.some((item) => item.id === genForm.value.skuId)) genForm.value.skuId = skus.value.length === 1 ? skus.value[0].id : ''
  } catch (error) { ElMessage.error('加载邀请商品失败: ' + error.message) }
})
const genResult = ref([])
const generating = ref(false)

// ── 导出 ──
const exportFormats = ref([])
const exporting = ref(false)
const exportVisible = ref(false)
const exportText = ref('')
const exportMeta = ref({ count: 0, filename: '', label: '' })

// ── 使用记录 ──
const recVisible = ref(false)
const recData = ref(null)
const recCode = ref('')

// ── 实时执行监控（每 2 秒拉一次，作业走到哪一步都看得见）──
const mon = ref(null)
const monErr = ref('')
const monOn = ref(true)
let monTimer = null

async function loadMonitor() {
  if (!monOn.value) return
  try {
    mon.value = await cdkMonitor(40)
    monErr.value = ''
  } catch (e) { monErr.value = e.message }
  // 记录弹窗开着就一起刷新，盯着单个卡密也是实时的
  if (recVisible.value && recCode.value) {
    try { recData.value = await cdkRecords(recCode.value) } catch (_) { /* 忽略 */ }
  }
}

function relTime(ts) {
  if (!ts) return '-'
  const d = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (d < 60) return `${d} 秒前`
  if (d < 3600) return `${Math.floor(d / 60)} 分 ${d % 60} 秒前`
  return `${Math.floor(d / 3600)} 小时前`
}

function fmtDur(sec) {
  if (sec === null || sec === undefined) return '-'
  if (sec < 60) return `${sec}s`
  return `${Math.floor(sec / 60)}m${String(sec % 60).padStart(2, '0')}s`
}

const activeJobs = computed(() =>
  (mon.value?.items || []).filter((j) => ['running', 'queued'].includes(j.status)))
const recentDone = computed(() =>
  (mon.value?.items || []).filter((j) => !['running', 'queued'].includes(j.status)).slice(0, 12))
const monEvents = computed(() => (mon.value?.events || []).slice(0, 25))

async function load(resetPage = false) {
  if (resetPage) page.value = 1
  loading.value = true
  try {
    const r = await listCdk({
      status: status.value, batch: batch.value, search: search.value,
      limit: PAGE_SIZE, offset: (page.value - 1) * PAGE_SIZE,
    })
    rows.value = r.items || []
    total.value = r.total || 0
    batches.value = r.batches || []
    stats.value = r.stats || {}
  } catch (e) { ElMessage.error(e.message) } finally { loading.value = false }
}

async function loadStats() {
  try { const r = await cdkStats(); stats.value = r.stats || {} } catch (_) {}
}

function selectedCodes() { return selected.value.map((r) => r.code) }

// ── 生成 ──
async function doGenerate() {
  if (!genForm.value.skuId) { ElMessage.info('请选择邀请商品'); return }
  generating.value = true
  try {
    const r = await generateCdk({ ...genForm.value })
    genResult.value = r.codes || []
    ElMessage.success(`已生成 ${r.inserted} 张（批次 ${r.batch}${r.skipped ? `，重复跳过 ${r.skipped}` : ''}）`)
    await load(true)
  } catch (e) { ElMessage.error('生成失败: ' + e.message) } finally { generating.value = false }
}

// ── 复制 ──
async function copySelected() {
  if (!selectedCodes().length) { ElMessage.info('先勾选卡密'); return }
  await copyText(selectedCodes().join('\n'))
}
async function copyAllFiltered() {
  const r = await listCdk({ status: status.value, batch: batch.value, search: search.value, limit: 100000, offset: 0 })
  if (!r.items?.length) { ElMessage.info('没有可复制的卡密'); return }
  await copyText(r.items.map((x) => x.code).join('\n'))
}
async function copyOne(code) { await copyText(code) }

// ── 导出 ──
async function loadFormats() {
  if (exportFormats.value.length) return
  try { const r = await cdkExportFormats(); exportFormats.value = r.formats || [] }
  catch (e) { ElMessage.error('加载导出格式失败: ' + e.message) }
}
function b64ToBytes(b64) {
  const bin = atob(b64 || '')
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}
function saveBlob(data, filename, mime) {
  const blob = new Blob([data], { type: mime || 'application/octet-stream' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}
async function doExport(fmt) {
  exporting.value = true
  try {
    const codes = selectedCodes()
    const payload = codes.length
      ? { format: fmt.id, codes }
      : { format: fmt.id, status: status.value, batch: batch.value, search: search.value }
    const r = await cdkExport(payload)
    if (r.mode === 'download') {
      saveBlob(b64ToBytes(r.b64), r.filename, r.mime)
      ElMessage.success(`已下载 ${r.filename}（${r.count} 行）`)
      return
    }
    exportText.value = r.text || ''
    exportMeta.value = { count: r.count, filename: r.filename, label: r.label }
    exportVisible.value = true
  } catch (e) { ElMessage.error('导出失败: ' + e.message) } finally { exporting.value = false }
}
function downloadExport() {
  saveBlob(exportText.value, exportMeta.value.filename || 'export.txt', 'text/plain;charset=utf-8')
}

// ── 记录 ──
async function showRecords(row) {
  try {
    recData.value = await cdkRecords(row.code)
    recCode.value = row.code
    recVisible.value = true
  } catch (e) { ElMessage.error('加载使用记录失败: ' + e.message) }
}

// ── 管理员操作 ──
async function confirm(msg) {
  try { await ElMessageBox.confirm(msg, '确认', { type: 'warning' }); return true } catch (_) { return false }
}
async function doReset(row) {
  if (!(await confirm(`重置卡密 ${row.code}？（清失败计数，状态回到可再次开始，邮箱绑定保留）`))) return
  try { await cdkReset(row.code); ElMessage.success('已重置'); await load() } catch (e) { ElMessage.error(e.message) }
}
async function doVoid(row) {
  if (!(await confirm(`作废卡密 ${row.code}？作废后邮箱会回到池子。`))) return
  try { await cdkVoid(row.code); ElMessage.success('已作废'); await load() } catch (e) { ElMessage.error(e.message) }
}
async function doUnbind(row) {
  if (!(await confirm(`解绑 ${row.code} 的邮箱？邮箱回池，卡密回到「未使用」。`))) return
  try { await cdkUnbind(row.code); ElMessage.success('已解绑'); await load() } catch (e) { ElMessage.error(e.message) }
}
async function doDelete() {
  const codes = selectedCodes()
  if (!codes.length) { ElMessage.info('先勾选要删除的卡密'); return }
  if (!(await confirm(`删除选中的 ${codes.length} 张卡密记录？占用的邮箱会先被放回池子。`))) return
  try { const r = await cdkDelete(codes); ElMessage.success(`已删除 ${r.deleted} 张`); selected.value = []; await load() }
  catch (e) { ElMessage.error(e.message) }
}
async function editNote(row) {
  try {
    const { value } = await ElMessageBox.prompt('备注', '修改备注', { inputValue: row.note || '' })
    await cdkUpdate({ code: row.code, note: value || '' })
    ElMessage.success('已保存')
    await load()
  } catch (_) { /* cancel */ }
}

const statCards = computed(() => {
  const s = stats.value || {}
  return [
    { label: '总数', value: s.total || 0, color: 'var(--brand)' },
    { label: '未使用', value: s.unused || 0, color: '#909399' },
    { label: '已兑换', value: s.bound || 0, color: '#409eff' },
    { label: '排队中', value: s.queued || 0, color: '#e6a23c' },
    { label: '执行中', value: s.running || 0, color: '#ff9800' },
    { label: '已完成', value: s.done || 0, color: '#4caf50' },
    { label: '失败', value: s.failed || 0, color: '#f56c6c' },
    { label: '已作废', value: s.void || 0, color: '#c0c4cc' },
    { label: '可兑换邮箱', value: s.mail_pool || 0, color: '#7c3aed' },
  ]
})

watch(page, () => load())
watch(dataVersion, () => load())
onActivated(() => { load(); loadStats(); loadFormats(); loadMonitor() })
onMounted(() => {
  loadMonitor()
  monTimer = setInterval(loadMonitor, 2000)
})
onDeactivated(() => { /* keep-alive：定时器继续跑，回来就有最新数据 */ })
onUnmounted(() => { if (monTimer) clearInterval(monTimer) })
</script>

<template>
  <div class="page">
    <!-- 实时执行监控：作业走到哪一步，这里 2 秒刷新一次 -->
    <el-card shadow="never" style="margin-bottom: 12px">
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px">
          <span class="section-title" style="margin: 0">
            实时执行监控
            <span class="hint" style="margin-left: 6px">每 2 秒刷新</span>
          </span>
          <div style="display: flex; align-items: center; gap: 10px">
            <span v-if="monErr" class="err" style="font-size: 12px">{{ monErr }}</span>
            <el-tag v-if="mon" size="small" type="warning">运行中 {{ mon.summary.running }}</el-tag>
            <el-tag v-if="mon" size="small" type="info">排队 {{ mon.summary.queued }}</el-tag>
            <el-tag v-if="mon" size="small" :type="mon.summary.proxy_available ? 'success' : 'danger'">
              可用代理 {{ mon.summary.proxy_available ?? 0 }}
            </el-tag>
            <el-tag v-if="mon" size="small">可用邮箱 {{ mon.summary.mail_pool ?? 0 }}</el-tag>
            <el-tag v-if="mon" size="small">并发 {{ mon.summary.workers }}/{{ mon.summary.max_concurrency }}</el-tag>
            <el-switch v-model="monOn" size="small" active-text="自动刷新" @change="loadMonitor" />
            <el-button size="small" text type="primary" @click="loadMonitor">立即刷新</el-button>
          </div>
        </div>
      </template>

      <el-empty v-if="!activeJobs.length" description="当前没有正在执行或排队的作业" :image-size="56" />

      <el-table v-else :data="activeJobs" size="small" stripe>
        <el-table-column prop="email" label="邮箱" min-width="200" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono" style="font-size: 12px">{{ row.email }}</span></template>
        </el-table-column>
        <el-table-column label="阶段" min-width="190">
          <template #default="{ row }">
            <el-tag :type="row.status === 'running' ? 'warning' : 'info'" size="small">{{ row.stage_label }}</el-tag>
            <span class="hint" style="margin-left: 6px; font-size: 12px">{{ fmtDur(row.elapsed) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="170">
          <template #default="{ row }">
            <el-progress :percentage="row.progress || 0" :stroke-width="10" />
          </template>
        </el-table-column>
        <el-table-column label="第几次" width="72">
          <template #default="{ row }">{{ row.attempt }}/{{ mon?.summary.max_concurrency ? 3 : 3 }}</template>
        </el-table-column>
        <el-table-column label="代理" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="mono" style="font-size: 11px">{{ row.proxy || '待分配（等邀请信阶段不用代理）' }}</span>
          </template>
        </el-table-column>
        <el-table-column label="错误" min-width="160" show-overflow-tooltip>
          <template #default="{ row }"><span class="err" style="font-size: 12px">{{ row.error || '-' }}</span></template>
        </el-table-column>
      </el-table>

      <el-collapse style="margin-top: 10px">
        <el-collapse-item :title="`实时事件流水（最近 ${monEvents.length} 条）`">
          <div v-for="e in monEvents" :key="e.id" class="mono" style="font-size: 12px; line-height: 1.9">
            <span class="hint">{{ relTime(e.created_at) }}</span>
            <el-tag size="small" style="margin: 0 6px">{{ e.event }}</el-tag>
            <span>{{ (e.detail || '').slice(0, 150) }}</span>
            <span v-if="e.code" class="hint" style="margin-left: 6px">{{ e.code.slice(-8) }}</span>
          </div>
          <el-empty v-if="!monEvents.length" description="暂无事件" :image-size="40" />
        </el-collapse-item>
      </el-collapse>
    </el-card>
    <el-row :gutter="16" style="margin-bottom: 4px">
      <el-col v-for="c in statCards" :key="c.label" :xs="8" :sm="6" :md="3" style="margin-bottom: 12px">
        <el-card class="stat-card" shadow="hover">
          <div class="stat-value" :style="{ color: c.color }">{{ c.value }}</div>
          <div class="stat-label">{{ c.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px">
          <span class="section-title" style="margin: 0">卡密管理</span>
          <div style="display: flex; gap: 8px; flex-wrap: wrap">
            <el-button type="primary" @click="genVisible = true"><el-icon><Coin /></el-icon>生成卡密</el-button>
            <el-button :disabled="!selected.length" @click="copySelected">
              <el-icon><CopyDocument /></el-icon>复制选中 ({{ selected.length }})
            </el-button>
            <el-button @click="copyAllFiltered">复制当前筛选全部</el-button>
            <el-dropdown trigger="click" @visible-change="loadFormats">
              <el-button :loading="exporting">导出<el-icon><ArrowDown /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item v-for="f in exportFormats" :key="f.id" @click="doExport(f)">
                    {{ f.label }}<span v-if="f.note" class="hint"> — {{ f.note }}</span>
                  </el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-button type="danger" plain :disabled="!selected.length" @click="doDelete">删除选中</el-button>
          </div>
        </div>
      </template>

      <el-space wrap style="margin-bottom: 12px">
        <el-button @click="load()"><el-icon><Refresh /></el-icon>刷新</el-button>
        <el-select v-model="status" style="width: 130px" @change="load(true)">
          <el-option v-for="o in STATUS_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
        </el-select>
        <el-select v-model="batch" style="width: 180px" clearable placeholder="按批次筛选" @change="load(true)">
          <el-option v-for="b in batches" :key="b" :label="b" :value="b" />
        </el-select>
        <el-input v-model="search" style="width: 240px" clearable placeholder="搜索卡密 / 邮箱 / 邀请方"
                  @keyup.enter="load(true)" />
        <el-button @click="load(true)">查询</el-button>
      </el-space>

      <el-table v-loading="loading" :data="rows" size="small" stripe
                @selection-change="(v) => selected = v">
        <el-table-column type="selection" width="42" />
        <el-table-column prop="code" label="卡密" min-width="190">
          <template #default="{ row }">
            <el-button text type="primary" class="mono" @click="copyOne(row.code)">{{ row.code }}</el-button>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="STATUS_TYPE[row.status]" size="small" effect="light">
              {{ STATUS_LABEL[row.status] || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="190" show-overflow-tooltip />
        <el-table-column label="邀请方" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.inviter_email" class="mono">{{ row.inviter_email }}</span>
            <span v-else class="hint">-</span>
          </template>
        </el-table-column>
        <el-table-column label="尝试 / 失败" width="100">
          <template #default="{ row }">{{ row.attempts }} / {{ row.fail_count }}</template>
        </el-table-column>
        <el-table-column prop="batch" label="批次" width="130" show-overflow-tooltip />
        <el-table-column label="创建时间" width="150">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="备注" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.note">{{ row.note }}</span><span v-else class="hint">-</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="290" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="showRecords(row)">使用记录</el-button>
            <el-button size="small" text @click="doReset(row)">重置</el-button>
            <el-button size="small" text @click="editNote(row)">备注</el-button>
            <el-button size="small" text type="warning" @click="doUnbind(row)">解绑</el-button>
            <el-button size="small" text type="danger" @click="doVoid(row)">作废</el-button>
          </template>
        </el-table-column>
        <template #empty><el-empty description="还没有卡密，点右上角「生成卡密」" :image-size="70" /></template>
      </el-table>

      <el-pagination
        style="margin-top: 12px; justify-content: flex-end"
        layout="total, prev, pager, next" :total="total" :page-size="PAGE_SIZE"
        :current-page="page" @current-change="(p) => (page = p)"
      />
    </el-card>

    <!-- 生成 -->
    <el-dialog v-model="genVisible" title="生成卡密" width="560px">
      <el-form label-position="top">
        <el-form-item label="邀请商品">
          <el-select v-model="genForm.skuId" placeholder="选择卡密所属商品" style="width: 100%">
            <el-option v-for="sku in skus" :key="sku.id" :label="sku.name" :value="sku.id" />
          </el-select>
        </el-form-item>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="数量（1–5000）">
              <el-input v-model.number="genForm.count" type="number" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="批次名（留空=当前时间）">
              <el-input v-model="genForm.batch" placeholder="20260101-A" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item label="前缀">
              <el-input v-model="genForm.prefix" placeholder="CDK-" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="随机部分长度（8–64）">
              <el-input v-model.number="genForm.length" type="number" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="备注">
          <el-input v-model="genForm.note" placeholder="可选" />
        </el-form-item>
      </el-form>
      <el-alert v-if="genResult.length" type="success" :closable="false" style="margin-bottom: 8px"
                :title="`本次生成 ${genResult.length} 张，可直接复制：`" />
      <el-input v-if="genResult.length" :model-value="genResult.join('\n')" type="textarea" :rows="6" readonly class="mono" />
      <template #footer>
        <el-button @click="genVisible = false">关闭</el-button>
        <el-button v-if="genResult.length" @click="copyText(genResult.join('\n'))">复制本次生成</el-button>
        <el-button type="primary" :loading="generating" @click="doGenerate">生成</el-button>
      </template>
    </el-dialog>

    <!-- 导出预览 -->
    <el-dialog v-model="exportVisible" :title="`导出预览 — ${exportMeta.label}（${exportMeta.count} 行）`" width="760px">
      <el-input v-model="exportText" type="textarea" :rows="16" readonly class="mono export-area" />
      <template #footer>
        <el-button @click="exportVisible = false">关闭</el-button>
        <el-button @click="copyText(exportText)">复制</el-button>
        <el-button type="primary" @click="downloadExport">下载 {{ exportMeta.filename }}</el-button>
      </template>
    </el-dialog>

    <!-- 使用记录 -->
    <el-dialog v-model="recVisible" :title="`使用记录 — ${recCode}`" width="880px">
      <template v-if="recData">
        <el-descriptions :column="2" border size="small" style="margin-bottom: 12px">
          <el-descriptions-item label="状态">
            <el-tag :type="STATUS_TYPE[recData.code.status]" size="small">
              {{ STATUS_LABEL[recData.code.status] || recData.code.status }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="邮箱">{{ recData.code.email || '-' }}</el-descriptions-item>
          <el-descriptions-item label="邀请方邮箱">{{ recData.code.inviter_email || '-' }}</el-descriptions-item>
          <el-descriptions-item label="邀请信主题">{{ recData.code.inviter_subject || '-' }}</el-descriptions-item>
          <el-descriptions-item label="收到邀请信">{{ fmtTime(recData.code.invite_received_at) }}</el-descriptions-item>
          <el-descriptions-item label="实际代理">
            <span class="mono">{{ recData.code.proxy_used || '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="尝试 / 失败">{{ recData.code.attempts }} / {{ recData.code.fail_count }}</el-descriptions-item>
          <el-descriptions-item label="最近错误">
            <span :class="recData.code.error ? 'err' : ''">{{ recData.code.error || '-' }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <div class="section-title" style="margin: 0 0 6px">作业历史</div>
        <el-table :data="recData.jobs" size="small" stripe max-height="200" style="margin-bottom: 12px">
          <el-table-column prop="job_id" label="job_id" width="200" show-overflow-tooltip />
          <el-table-column prop="attempt" label="第几次" width="80" />
          <el-table-column prop="status" label="状态" width="90" />
          <el-table-column prop="stage" label="阶段" width="140" />
          <el-table-column prop="progress" label="进度" width="80" />
          <el-table-column prop="proxy" label="代理" min-width="160" show-overflow-tooltip />
          <el-table-column label="开始" width="150">
            <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
          </el-table-column>
          <el-table-column prop="error" label="错误" min-width="180" show-overflow-tooltip />
          <template #empty><el-empty description="暂无作业" :image-size="50" /></template>
        </el-table>

        <div class="section-title" style="margin: 0 0 6px">事件流水</div>
        <el-table :data="recData.events" size="small" stripe max-height="240">
          <el-table-column label="时间" width="160">
            <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="event" label="事件" width="140" />
          <el-table-column prop="detail" label="详情" min-width="300" show-overflow-tooltip />
          <template #empty><el-empty description="暂无事件" :image-size="50" /></template>
        </el-table>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.stat-card { text-align: center; }
.stat-value { font-size: 22px; font-weight: 600; }
.stat-label { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 2px; }
.export-area :deep(textarea) { white-space: pre; overflow-x: auto; }
</style>
