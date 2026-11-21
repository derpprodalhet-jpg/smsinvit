<script setup>
import { computed, onActivated, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useProxyStore, proxyScheme, STATUS_LABEL, STATUS_TYPE } from '@/stores/proxy'
import { testProxies } from '@/api/proxyPool'
import { copyText, fmtTime } from '@/api/request'
import { resetProxy, removeProxy, setProxyEnabled, listProxyEvents, diagnoseProxy } from '@/api/proxyPool'

const store = useProxyStore()

const draft = ref('')
const testResults = ref({}) // url -> { status, latency_ms, ip, error, hint, alt_scheme }
const testingAll = ref(false)
const diagVisible = ref(false)
const diagRunning = ref(false)
const diag = ref(null)
const eventsVisible = ref(false)
const events = ref([])
const eventsTitle = ref('')

const rows = computed(() =>
  store.items.map((p, i) => ({
    index: i + 1,
    ...p,
    result: testResults.value[p.url] || null,
  })),
)

const legacyText = computed(() => store.legacyText())

async function refresh() {
  try { await store.refresh() } catch (e) { ElMessage.error('加载代理池失败: ' + e.message) }
}

async function runTest(targets) {
  if (!targets.length) return
  for (const p of targets) testResults.value[p] = { status: 'testing' }
  try {
    const { results } = await testProxies(targets)
    for (const [proxy, res] of Object.entries(results)) {
      testResults.value[proxy] = { status: res.ok ? 'ok' : 'fail', ...res }
    }
  } catch (e) {
    for (const p of targets) testResults.value[p] = { status: 'fail', error: e.message }
    ElMessage.error('测试失败: ' + e.message)
  }
}
const testOne = (proxy) => runTest([proxy])
async function testAll() {
  if (!store.count) return
  testingAll.value = true
  try { await runTest(store.items.map((x) => x.url)) } finally { testingAll.value = false }
}

async function save() {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  try {
    const r = await store.setFromText(draft.value)
    draft.value = ''
    ElMessage.success(`已覆盖保存 ${r.parsed} 条（新增 ${r.added}，保留 ${r.skipped}）`)
  } catch (e) { ElMessage.error('保存失败: ' + e.message) }
}
async function append() {
  if (!draft.value.trim()) { ElMessage.warning('请先粘贴代理'); return }
  try {
    const r = await store.append(draft.value)
    draft.value = ''
    ElMessage.success(`已追加 ${r.added} 条（跳过重复 ${r.skipped}）`)
  } catch (e) { ElMessage.error('追加失败: ' + e.message) }
}
function editInDraft() {
  draft.value = store.text
  ElMessage.info('已把当前代理池载入编辑框，改完点「覆盖保存」')
}
function loadLegacy() {
  const t = legacyText.value
  if (!t) { ElMessage.info('浏览器里没有旧版代理池'); return }
  draft.value = t
  ElMessage.info('已载入浏览器旧代理池，点「追加合并」导入服务端')
}
async function importLegacy() {
  const t = legacyText.value
  if (!t) { ElMessage.info('浏览器里没有旧版代理池'); return }
  try {
    const r = await store.append(t)
    store.clearLegacy()
    ElMessage.success(`已从浏览器导入 ${r.added} 条`)
  } catch (e) { ElMessage.error('导入失败: ' + e.message) }
}

async function switchScheme(row) {
  const alt = row.result?.alt_scheme
  if (!alt) return
  const urls = store.items.map((x) => (x.url === row.url ? alt : x.url))
  try {
    await store.setFromText(urls.join('\n'))
    ElMessage.success(`已改成 ${alt.split('://')[0]}:// 保存，请再测一次`)
  } catch (e) { ElMessage.error('保存失败: ' + e.message) }
}

async function toggle(row) {
  try {
    await setProxyEnabled({ id: row.id, enabled: !row.enabled })
    await refresh()
  } catch (e) { ElMessage.error(e.message) }
}
async function doReset(row) {
  try {
    await resetProxy({ id: row.id })
    ElMessage.success('已重置（清空计数与冻结/永久标记）')
    await refresh()
  } catch (e) { ElMessage.error(e.message) }
}
async function doRemove(row) {
  try {
    await ElMessageBox.confirm(`删除代理 ${row.url}？`, '确认', { type: 'warning' })
  } catch (_) { return }
  try { await removeProxy({ id: row.id }); ElMessage.success('已删除'); await refresh() }
  catch (e) { ElMessage.error(e.message) }
}
async function clearAll() {
  if (!store.count) return
  try { await ElMessageBox.confirm(`确定清空全部 ${store.count} 个代理？`, '确认', { type: 'warning' }) }
  catch (_) { return }
  try { await store.setFromText(''); ElMessage.success('已清空') } catch (e) { ElMessage.error(e.message) }
}
async function runDiagnose() {
  diagRunning.value = true
  try {
    diag.value = await diagnoseProxy()
    diagVisible.value = true
  } catch (e) { ElMessage.error('诊断失败: ' + e.message) } finally { diagRunning.value = false }
}

async function showEvents(row) {
  try {
    const r = await listProxyEvents(row?.id ?? null, 200)
    events.value = r.items || []
    eventsTitle.value = row ? row.url : '全部代理事件'
    eventsVisible.value = true
  } catch (e) { ElMessage.error(e.message) }
}

const EV_LABEL = {
  add: '新增', use: '选用', success: '成功', fail: '失败', '409': '409',
  freeze: '冻结', permanent: '永久不可用', reset: '重置',
  enable: '启用', disable: '禁用', remove: '删除',
}

onMounted(refresh)
onActivated(refresh)
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">批量导入</span></template>
          <p class="hint">
            每行一个：<span class="mono">[协议://][user:pass@]host:port</span><br />
            不写协议默认按 <b>HTTP 代理</b>；SOCKS5 必须写 <span class="mono">socks5://</span>（会归一化成 socks5h 走远端 DNS）。<br />
            池子存在<b>服务端</b>：CDK 邀请作业跑在服务端，只有服务端池子才用得上。
          </p>
          <el-input
            v-model="draft" type="textarea" :rows="12" class="mono"
            placeholder="socks5://127.0.0.1:7890&#10;socks5://user:pass@1.2.3.4:1080&#10;http://5.6.7.8:8080"
          />
          <div style="margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap">
            <el-button type="primary" @click="save">覆盖保存</el-button>
            <el-button @click="append">追加合并</el-button>
            <el-button @click="editInDraft">载入当前池</el-button>
            <el-button v-if="legacyText" @click="importLegacy">从浏览器旧池导入</el-button>
            <el-button v-if="legacyText" text @click="loadLegacy">仅载入编辑框</el-button>
          </div>
          <el-alert
            type="info" :closable="false" show-icon style="margin-top: 12px"
            title="轮换规则：每次开始新任务从「可用且使用次数最少」的代理开始，不把一个 IP 怼死。"
          />
        </el-card>
      </el-col>

      <el-col :md="14" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px">
              <span class="section-title" style="margin: 0">
                服务端代理池（共 {{ store.count }}，可用 {{ store.usableCount }}）
              </span>
              <div style="display: flex; gap: 8px; flex-wrap: wrap">
                <el-tag size="small" type="success" effect="plain">可用 {{ store.summary.ready }}</el-tag>
                <el-tag size="small" type="warning" effect="plain">冻结 {{ store.summary.frozen }}</el-tag>
                <el-tag size="small" type="danger" effect="plain">永久不可用 {{ store.summary.dead }}</el-tag>
                <el-button size="small" :loading="testingAll" :disabled="!store.count" @click="testAll">测试全部</el-button>
                <el-button size="small" :loading="diagRunning" @click="runDiagnose">一键诊断</el-button>
                <el-button size="small" :disabled="!store.count" @click="showEvents(null)">事件流水</el-button>
                <el-button size="small" :disabled="!store.count" @click="copyText(store.text)">复制全部</el-button>
                <el-button size="small" type="danger" plain :disabled="!store.count" @click="clearAll">清空</el-button>
              </div>
            </div>
          </template>

          <el-table v-loading="store.loading" :data="rows" size="small" stripe max-height="460">
            <el-table-column prop="index" label="#" width="46" />
            <el-table-column prop="url" label="代理地址" min-width="190" show-overflow-tooltip>
              <template #default="{ row }"><span class="mono">{{ row.url }}</span></template>
            </el-table-column>
            <el-table-column label="状态" width="120">
              <template #default="{ row }">
                <el-tag :type="STATUS_TYPE[row.status]" size="small" effect="light">
                  {{ STATUS_LABEL[row.status] }}<template v-if="row.status === 'frozen'">（{{ Math.ceil(row.frozen_remaining / 60) }}分）</template>
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="协议" width="90">
              <template #default="{ row }"><span class="mono" style="font-size: 12px">{{ proxyScheme(row.url) }}</span></template>
            </el-table-column>
            <el-table-column label="使用" width="60">
              <template #default="{ row }">{{ row.use_count }}</template>
            </el-table-column>
            <el-table-column label="成功/失败" width="90">
              <template #default="{ row }">
                <span style="color: #4caf50">{{ row.success_count }}</span> /
                <span style="color: #f56c6c">{{ row.fail_count }}</span>
              </template>
            </el-table-column>
            <el-table-column label="409（连/计）" width="100">
              <template #default="{ row }">{{ row.consecutive_409 }} / {{ row.total_409 }}</template>
            </el-table-column>
            <el-table-column label="最后使用" width="150">
              <template #default="{ row }">{{ fmtTime(row.last_used_at) }}</template>
            </el-table-column>
            <el-table-column label="连通性" min-width="130">
              <template #default="{ row }">
                <template v-if="!row.result"><span class="hint">未测</span></template>
                <el-tag v-else-if="row.result.status === 'testing'" type="warning" size="small">测试中…</el-tag>
                <template v-else-if="row.result.status === 'ok'">
                  <el-tag type="success" size="small">{{ row.result.latency_ms }}ms</el-tag>
                  <span v-if="row.result.ip" class="hint mono" style="margin-left: 4px">{{ row.result.ip }}</span>
                  <div v-if="row.result.hint" class="hint" style="margin-top: 2px; color: var(--el-color-primary)">
                    {{ row.result.hint }}
                  </div>
                </template>
                <template v-else>
                  <el-tooltip :content="row.result.error || '连接失败'" placement="top">
                    <el-tag type="danger" size="small">失败</el-tag>
                  </el-tooltip>
                  <div v-if="row.result.hint" class="hint" style="margin-top: 2px; color: var(--el-color-danger)">
                    {{ row.result.hint }}
                  </div>
                  <el-button v-if="row.result.alt_scheme" size="small" text type="primary"
                             style="padding: 0" @click="switchScheme(row)">
                    改用 {{ row.result.alt_scheme.split('://')[0] }}:// 保存
                  </el-button>
                </template>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="230" fixed="right">
              <template #default="{ row }">
                <el-button size="small" text type="primary" @click="testOne(row.url)">测试</el-button>
                <el-button size="small" text @click="toggle(row)">{{ row.enabled ? '禁用' : '启用' }}</el-button>
                <el-button size="small" text type="warning" @click="doReset(row)">重置</el-button>
                <el-button size="small" text @click="showEvents(row)">流水</el-button>
                <el-button size="small" text type="danger" @click="doRemove(row)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty><el-empty description="暂无代理，请在左侧批量导入" :image-size="70" /></template>
          </el-table>

          <el-alert
            type="warning" :closable="false" show-icon style="margin-top: 12px"
            title="用户前台不会展示任何代理信息；池里没有可用代理时，用户点「开始执行」会被拒绝并提示稍后重试。"
          />
        </el-card>
      </el-col>
    </el-row>

    <el-dialog v-model="diagVisible" title="代理路径诊断（直连 / 系统代理 / 池内代理）" width="820px">
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px"
        title="注意：这里的网络请求发生在服务进程里。服务要在你自己的终端里启动，结果才代表你的网络。" />
      <el-table :data="diag?.checks || []" size="small" stripe>
        <el-table-column label="" width="46">
          <template #default="{ row }">
            <span v-if="row.ok === true">✅</span><span v-else-if="row.ok === false">❌</span><span v-else>－</span>
          </template>
        </el-table-column>
        <el-table-column prop="label" label="路径" min-width="180" />
        <el-table-column prop="proxy" label="代理" min-width="200" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono" style="font-size: 12px">{{ row.proxy }}</span></template>
        </el-table-column>
        <el-table-column prop="ip" label="出口 IP" width="140">
          <template #default="{ row }"><span class="mono" style="font-size: 12px">{{ row.ip || '-' }}</span></template>
        </el-table-column>
        <el-table-column prop="latency_ms" label="耗时" width="80" />
        <el-table-column label="错误 / 死因" min-width="240">
          <template #default="{ row }">
            <div v-if="row.error" class="mono" style="font-size: 11px">{{ row.error }}</div>
            <div v-if="row.hint" style="color: var(--el-color-danger); font-size: 12px">{{ row.hint }}</div>
            <span v-if="!row.error && !row.hint" class="hint">—</span>
          </template>
        </el-table-column>
      </el-table>
      <el-alert v-if="diag" :type="diag.pool_ok ? 'success' : (diag.base_ok ? 'warning' : 'error')"
                :closable="false" show-icon style="margin-top: 12px" :title="diag.verdict" />
    </el-dialog>

    <el-dialog v-model="eventsVisible" :title="`事件流水 — ${eventsTitle}`" width="720px">
      <el-table :data="events" size="small" stripe max-height="420">
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="事件" width="110">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ EV_LABEL[row.event] || row.event }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="url" label="代理" min-width="180" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono">{{ row.url }}</span></template>
        </el-table-column>
        <el-table-column prop="detail" label="详情" min-width="180" show-overflow-tooltip />
        <template #empty><el-empty description="暂无事件" :image-size="60" /></template>
      </el-table>
    </el-dialog>
  </div>
</template>
