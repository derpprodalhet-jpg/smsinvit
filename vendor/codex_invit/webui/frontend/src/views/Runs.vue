<script setup>
import { computed, onActivated, onDeactivated, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { listRuns, runLog } from '@/api/register'
import { fmtTime, copyText } from '@/api/request'
import StatusDot from '@/components/StatusDot.vue'

// 运行记录 = 实时看板。
// 以前只显示 status=running，主人只看到"在跑"，不知道卡在哪一步；
// 现在每 2 秒刷新一次，在跑的作业直接显示：阶段 + 进度条 + 第几次 + 代理 + 已跑时长。
const rows = ref([])
const loading = ref(false)
const autoOn = ref(true)
const err = ref('')
let timer = null

const STATUS_TYPE = { done: 'primary', failed: 'danger', running: 'warning' }

// ── 详情弹窗：完整日志 + 该邮箱的卡密事件 ──
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const detailErr = ref('')

async function openDetail(row) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  detailErr.value = ''
  try {
    detail.value = await runLog(row.run_id, 1024)
  } catch (e) { detailErr.value = e.message } finally { detailLoading.value = false }
}

function copyLog() {
  if (detail.value?.text) copyText(detail.value.text)
}
const STATUS_LABEL = { done: '完成', failed: '失败', running: '运行中' }

async function load(quiet = false) {
  if (!quiet) loading.value = true
  try {
    const { items } = await listRuns(50)
    rows.value = items || []
    err.value = ''
  } catch (e) {
    err.value = e.message
    if (!quiet) ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function fmtDur(sec) {
  if (sec === null || sec === undefined) return '-'
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m${String(sec % 60).padStart(2, '0')}s`
  return `${Math.floor(sec / 3600)}h${String(Math.floor((sec % 3600) / 60)).padStart(2, '0')}m`
}

const runningCount = computed(() => rows.value.filter((r) => r.status === 'running').length)

onMounted(() => {
  load()
  timer = setInterval(() => { if (autoOn.value) load(true) }, 2000)
})
onActivated(() => load(true))
onDeactivated(() => { /* keep-alive：定时器继续，回来就是最新 */ })
onUnmounted(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="page">
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px">
          <span class="section-title" style="margin: 0">
            运行记录
            <el-tag v-if="runningCount" type="warning" size="small" style="margin-left: 8px">
              运行中 {{ runningCount }}
            </el-tag>
            <span class="hint" style="margin-left: 6px">每 2 秒刷新</span>
          </span>
          <div style="display: flex; align-items: center; gap: 8px">
            <span v-if="err" class="err" style="font-size: 12px">{{ err }}</span>
            <el-switch v-model="autoOn" size="small" active-text="自动刷新" />
            <el-button size="small" @click="load()"><el-icon><Refresh /></el-icon>刷新</el-button>
          </div>
        </div>
      </template>

      <el-skeleton v-if="loading && !rows.length" :rows="6" animated style="padding: 8px 0" />
      <el-table v-else v-loading="loading" :data="rows" size="small" stripe>
        <el-table-column prop="run_id" label="run_id" width="150">
          <template #default="{ row }"><span class="mono" style="font-size: 12px">{{ row.run_id }}</span></template>
        </el-table-column>
        <el-table-column prop="email" label="邮箱" min-width="190" show-overflow-tooltip>
          <template #default="{ row }"><span class="mono" style="font-size: 12px">{{ row.email }}</span></template>
        </el-table-column>
        <el-table-column label="类型" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.kind === 'codex_auth' ? 'success' : 'info'">
              {{ row.kind === 'codex_auth' ? '邀请授权' : '注册' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="88">
          <template #default="{ row }">
            <StatusDot :type="STATUS_TYPE[row.status] || 'info'"
                       :text="STATUS_LABEL[row.status] || row.status" />
          </template>
        </el-table-column>
        <el-table-column label="当前阶段 / 进度" min-width="240">
          <template #default="{ row }">
            <template v-if="row.status === 'running'">
              <el-tag type="warning" size="small">{{ row.stage_label || '执行中' }}</el-tag>
              <span v-if="row.attempt" class="hint" style="margin-left: 6px; font-size: 12px">
                第 {{ row.attempt }} 次
              </span>
              <el-progress :percentage="row.progress || 0" :stroke-width="8" style="margin-top: 4px" />
              <div v-if="row.proxy" class="mono" style="font-size: 11px; color: var(--el-text-color-secondary)">
                代理 {{ row.proxy }}
              </div>
            </template>
            <span v-else class="hint">—</span>
          </template>
        </el-table-column>
        <el-table-column label="已跑" width="80">
          <template #default="{ row }">{{ fmtDur(row.elapsed) }}</template>
        </el-table-column>
        <el-table-column label="开始时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button size="small" text type="primary" @click="openDetail(row)">详情</el-button>
          </template>
        </el-table-column>
        <el-table-column prop="error" label="错误 / 结果" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="row.error ? 'err' : ''" style="font-size: 12px">{{ row.error || '-' }}</span>
          </template>
        </el-table-column>
        <template #empty>
          <el-empty description="暂无运行记录" :image-size="70" />
        </template>
      </el-table>
    </el-card>

    <!-- 详情：完整日志（不再只有一句 error） -->
    <el-dialog v-model="detailVisible" :title="`运行详情 — ${detail?.run?.run_id || ''}`" width="1000px" top="5vh">
      <el-alert v-if="detailErr" type="error" :closable="false" :title="detailErr" style="margin-bottom: 10px" />
      <el-skeleton v-if="detailLoading" :rows="8" animated />
      <template v-else-if="detail">
        <el-descriptions :column="3" border size="small" style="margin-bottom: 12px">
          <el-descriptions-item label="邮箱">{{ detail.run.email }}</el-descriptions-item>
          <el-descriptions-item label="类型">{{ detail.run.kind === 'codex_auth' ? '邀请授权' : '注册' }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <StatusDot :type="STATUS_TYPE[detail.run.status] || 'info'" :text="STATUS_LABEL[detail.run.status] || detail.run.status" />
          </el-descriptions-item>
          <el-descriptions-item label="开始">{{ fmtTime(detail.run.started_at) }}</el-descriptions-item>
          <el-descriptions-item label="结束">{{ detail.run.finished_at ? fmtTime(detail.run.finished_at) : '-' }}</el-descriptions-item>
          <el-descriptions-item label="耗时">{{ fmtDur(detail.run.finished_at ? Math.round(detail.run.finished_at - detail.run.started_at) : Math.round(Date.now() / 1000 - detail.run.started_at)) }}</el-descriptions-item>
          <el-descriptions-item label="错误" :span="3">
            <span :class="detail.run.error ? 'err' : ''">{{ detail.run.error || '-' }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="日志文件" :span="3">
            <span class="mono" style="font-size: 12px">{{ detail.log_path }}</span>
            <el-tag size="small" style="margin-left: 6px">{{ Math.round((detail.size || 0) / 1024) }} KB</el-tag>
            <el-tag v-if="detail.truncated" size="small" type="warning" style="margin-left: 4px">已截断（只显示尾部）</el-tag>
          </el-descriptions-item>
        </el-descriptions>

        <el-collapse v-if="(detail.events || []).length" style="margin-bottom: 10px">
          <el-collapse-item :title="`卡密事件流水（${detail.events.length} 条）`">
            <div v-for="e in detail.events" :key="e.id" class="mono" style="font-size: 12px; line-height: 1.8">
              <span class="hint">{{ fmtTime(e.created_at) }}</span>
              <el-tag size="small" style="margin: 0 6px">{{ e.event }}</el-tag>
              <span>{{ e.detail }}</span>
            </div>
          </el-collapse-item>
        </el-collapse>

        <div class="section-title" style="margin: 0 0 6px">完整日志</div>
        <pre class="mono log-area">{{ detail.text || '（日志文件为空）' }}</pre>
      </template>
      <template #footer>
        <el-button @click="detailVisible = false">关闭</el-button>
        <el-button :disabled="!detail?.text" @click="copyLog">复制全部日志</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.log-area {
  max-height: 52vh;
  overflow: auto;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
