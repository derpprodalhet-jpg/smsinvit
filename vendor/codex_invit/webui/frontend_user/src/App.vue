<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getMeta, redeem, markInviteSent, startJob, getStatus, copyText, loadMine, saveMine } from './api'

// ────────────────────────────────────────────────────────────
// 用户前台：只有两件事 —— 兑换、以及「我已发送邀请信 → 开始执行」。
// 展示的信息只有邮箱 + 一个脱敏的阶段条（后台不会下发代理/凭证/日志）。
// ────────────────────────────────────────────────────────────

const meta = ref({ pool_available: 0, ready: false, concurrency: 3, running: 0, queued: 0 })
const draft = ref('')
const mine = ref([])          // [{ code, email, status, stage, stage_label, progress, queue_position, invited }]
const redeeming = ref(false)
const starting = ref({})
const loading = ref(false)

const codesInput = computed(() =>
  draft.value.split('\n').map((s) => s.trim()).filter(Boolean),
)
const poolText = computed(() =>
  meta.value.pool_available === -1 ? '充足' : String(meta.value.pool_available ?? 0),
)
const poolEnough = computed(() =>
  meta.value.pool_available === -1 || codesInput.value.length <= meta.value.pool_available,
)

const STAGE_TEXT = (it) => {
  if (it.status === 'unused') return '未兑换'
  if (it.status === 'void') return '已作废'
  if (it.status === 'unknown') return '未知卡密'
  if (it.status === 'bound') return it.invited ? '已发送邀请信，可以开始' : '待发送邀请信'
  if (it.status === 'queued') {
    const n = it.queue_position || 0
    return n > 1 ? `正在排队（前面还有 ${n - 1} 个）` : '正在排队'
  }
  if (it.status === 'running') return it.stage_label || '正在执行'
  if (it.status === 'done') return '完成'
  if (it.status === 'failed') return '未成功，请稍后重试'
  return it.stage_label || ''
}

const barStatus = (it) => {
  if (it.status === 'done') return 'success'
  if (it.status === 'failed') return 'exception'
  return ''
}

async function loadMeta() {
  try { meta.value = await getMeta() } catch (_) { /* 静默：轮询失败不打扰用户 */ }
}

async function loadStatus() {
  const codes = mine.value.map((x) => x.code)
  if (!codes.length) return
  try {
    const r = await getStatus(codes)
    const map = new Map((r.items || []).map((x) => [x.code, x]))
    mine.value = mine.value.map((it) => {
      const s = map.get(it.code)
      if (!s) return it
      return {
        ...it,
        email: s.email || it.email,
        status: s.status,
        stage: s.stage,
        stage_label: s.stage_label,
        progress: s.progress,
        queue_position: s.queue_position,
        // 服务端不返回 invited，这个状态由本地记住（用户确认过就是确认过）
        invited: it.invited,
      }
    })
    saveMine(mine.value)
  } catch (_) { /* 静默 */ }
}

async function doRedeem() {
  const codes = codesInput.value
  if (!codes.length) { ElMessage.warning('请输入卡密（一行一个）'); return }
  if (!poolEnough.value) { ElMessage.warning(`当前可用邮箱 ${poolText.value} 个，本次需要 ${codes.length} 个`); return }
  redeeming.value = true
  try {
    const r = await redeem(codes)
    const items = r.items || []
    for (const it of items) {
      const i = mine.value.findIndex((x) => x.code === it.code)
      const row = { code: it.code, email: it.email, status: 'bound', invited: false }
      if (i >= 0) mine.value[i] = { ...mine.value[i], ...row }
      else mine.value.unshift(row)
    }
    saveMine(mine.value)
    draft.value = ''
    ElMessage.success(`兑换成功：${items.length} 个邮箱`)
    await loadMeta()
    await loadStatus()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    redeeming.value = false
  }
}

async function doInviteSent(it) {
  try {
    await markInviteSent(it.code)
    it.invited = true
    saveMine(mine.value)
    ElMessage.success('已记录：请点「开始执行」')
  } catch (e) { ElMessage.error(e.message) }
}

async function doStart(it) {
  if (!it.invited) { ElMessage.warning('请先确认「我已发送邀请信」'); return }
  starting.value = { ...starting.value, [it.code]: true }
  try {
    const r = await startJob(it.code)
    if (!r.ok) {
      ElMessage.warning(r.message || '当前繁忙，请稍后重试')
    } else {
      it.status = r.item?.status || 'queued'
      it.progress = r.item?.progress || 0
      it.stage_label = r.item?.stage_label || '正在排队'
      it.queue_position = r.item?.queue_position || 0
      saveMine(mine.value)
      ElMessage.success('已开始执行')
    }
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    const s = { ...starting.value }
    delete s[it.code]
    starting.value = s
  }
}

function forget(it) {
  mine.value = mine.value.filter((x) => x.code !== it.code)
  saveMine(mine.value)
}

let timer = null
onMounted(async () => {
  mine.value = loadMine()
  await loadMeta()
  await loadStatus()
  timer = setInterval(() => {
    if (document.visibilityState === 'visible') {
      loadMeta()
      loadStatus()
    }
  }, 2000)
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div class="wrap">
    <div class="brand"><span class="dot">C</span> codex_invit</div>
    <div class="sub">兑换卡密 → 把邮箱发给邀请方 → 确认已发送 → 开始执行</div>

    <!-- ① 兑换 -->
    <div class="card">
      <h2>兑换</h2>
      <div class="tip">一个卡密兑换一个邮箱。可一次输入多条（每行一个）。</div>

      <div class="pool">
        <span class="num">{{ poolText }}</span>
        <span class="lbl">当前可用邮箱{{ meta.pool_available === -1 ? '' : ' 个' }}</span>
        <span v-if="!meta.ready" class="busy">· 当前繁忙，开始执行可能需稍后重试</span>
      </div>

      <el-input
        v-model="draft"
        type="textarea"
        :rows="4"
        class="mono"
        placeholder="CDK-XXXXXXXX&#10;CDK-YYYYYYYY"
      />
      <div style="display: flex; align-items: center; gap: 12px; margin-top: 12px">
        <el-button
          type="primary" size="large" :loading="redeeming"
          :disabled="!codesInput.length || !poolEnough"
          @click="doRedeem"
        >
          兑换{{ codesInput.length ? `（${codesInput.length} 条）` : '' }}
        </el-button>
        <span v-if="!poolEnough" class="busy">
          可用邮箱 {{ poolText }} 个，不够本次兑换
        </span>
        <span v-else class="stage">已输入 {{ codesInput.length }} 条</span>
      </div>
    </div>

    <!-- ② 我的邮箱 / 执行 -->
    <div class="card">
      <h2>我的邮箱</h2>
      <div class="tip">
        把邮箱复制给对方发邀请信；确认已发送后再点「开始执行」。
      </div>

      <el-empty v-if="!mine.length" description="还没有兑换记录" :image-size="70" />

      <div v-for="it in mine" :key="it.code" class="mailrow">
        <div class="grow">
          <div class="mail mono">
            {{ it.email || '（待分配）' }}
            <el-button v-if="it.email" size="small" text type="primary" @click="copyText(it.email)">复制</el-button>
          </div>
          <div class="stage">
            卡密 <span class="mono">{{ it.code }}</span> · {{ STAGE_TEXT(it) }}
          </div>
          <el-progress
            v-if="['queued','running','done','failed'].includes(it.status)"
            :percentage="it.status === 'done' ? 100 : (it.progress || 0)"
            :status="barStatus(it)"
            :stroke-width="10"
            style="margin-top: 8px"
          />
        </div>

        <div style="display: flex; gap: 8px; align-items: center; flex-wrap: wrap">
          <el-button
            v-if="['bound'].includes(it.status)"
            :type="it.invited ? 'success' : 'default'"
            :plain="!it.invited"
            @click="doInviteSent(it)"
            :disabled="it.invited"
          >
            {{ it.invited ? '已发送 ✓' : '我已发送邀请信' }}
          </el-button>
          <el-button
            v-if="['bound','failed'].includes(it.status)"
            type="primary"
            :loading="!!starting[it.code]"
            :disabled="!it.invited || !meta.ready"
            @click="doStart(it)"
          >
            开始执行
          </el-button>
          <el-button v-if="['done','failed','void'].includes(it.status)" text @click="forget(it)">移除</el-button>
        </div>
      </div>
    </div>

    <div class="foot">
      进度只反映执行阶段；遇到「未成功，请稍后重试」时直接再点一次「开始执行」即可，卡密不会作废。
    </div>
  </div>
</template>
