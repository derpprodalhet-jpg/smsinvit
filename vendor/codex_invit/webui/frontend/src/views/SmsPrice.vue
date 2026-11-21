<script setup>
import { computed, onActivated, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getSmsPrices, refreshSmsPrices, applySmsBest, smsSelftest } from '@/api/cdk'
import { fmtTime } from '@/api/request'

const data = ref(null)
const loading = ref(false)
const refreshing = ref(false)
const testing = ref(false)
const selftest = ref(null)
const selftestVisible = ref(false)
const timer = ref(null)

const best = computed(() => data.value?.best || null)
const eff = computed(() => data.value?.effective || null)
const ev = computed(() => data.value?.evaluate || {})
const rows = computed(() => data.value?.rows || [])
const platforms = computed(() => data.value?.platforms || {})

const PLATFORM_LABEL = { smsbower: 'SmsBower', herosms: 'HeroSMS', smspool: 'SMSPool' }

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    data.value = await getSmsPrices()
  } catch (e) {
    if (!silent) ElMessage.error('加载接码价格失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

async function doRefresh() {
  refreshing.value = true
  try {
    await refreshSmsPrices()
    await load(true)
    ElMessage.success('已立即刷新各平台')
  } catch (e) {
    ElMessage.error('刷新失败: ' + e.message)
  } finally {
    refreshing.value = false
  }
}

async function applyBest() {
  try {
    const r = await applySmsBest()
    ElMessage.success(`已写入配置：国家 ${r.applied.sms_country} / 最高价 ${r.applied.sms_max_price}`)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function runSelftest() {
  testing.value = true
  try {
    selftest.value = await smsSelftest()
    selftestVisible.value = true
  } catch (e) {
    ElMessage.error('自检失败: ' + e.message)
  } finally {
    testing.value = false
  }
}

function startPolling() {
  stopPolling()
  timer.value = setInterval(() => {
    if (document.visibilityState === 'visible') load(true)
  }, Math.max(2, data.value?.poll_seconds || 5) * 1000)
}
function stopPolling() {
  if (timer.value) { clearInterval(timer.value); timer.value = null }
}

onMounted(async () => { await load(); startPolling() })
onActivated(async () => { await load(true); startPolling() })
onBeforeUnmount(stopPolling)

const ROW_TYPE = (r) => {
  if (data.value?.best && r.platform === data.value.best.platform && r.country === data.value.best.country) return 'success'
  if (r.rejected_reason) return 'info'
  return ''
}
</script>

<template>
  <div class="page" v-loading="loading">
    <!-- ⚠️ 最重要的一条：最优国家是不是 OpenAI 能发短信的国家 -->
    <el-alert
      v-if="data && data.effective && data.effective.source === 'monitor'"
      :type="data.effective.openai_safe ? 'success' : 'error'"
      :closable="false" show-icon style="margin-bottom: 12px"
      :title="data.effective.openai_safe
        ? `当前最优 ${data.best?.country_name}(${data.best?.country}) 在 OpenAI 短信白名单内，能收到验证码`
        : `当前最优 ${data.best?.country_name}(${data.best?.country}) $${data.best?.price} **不在** OpenAI 短信白名单，大概率收不到验证码 —— 会白花钱租号`"
    >
      <div v-if="!data.effective.openai_safe" style="font-size: 13px; line-height: 1.7">
        <div v-if="(data.effective.openai_safe_blocked || []).length">
          白名单国家被过滤掉了：
          <b v-for="b in data.effective.openai_safe_blocked" :key="b.platform + b.country">
            {{ b.country }}（{{ b.platform }} ${{ b.price }}，{{ b.reason }}）
          </b>
          → 到「接码配置」把<b>最高价</b>放宽到 ≥ ${{ data.effective.openai_safe_blocked[0].price }} 即可选到能收码的国家。
        </div>
        <div v-else>白名单国家当前没有报价/没货，只能选非白名单国家。</div>
      </div>
    </el-alert>
    <el-row :gutter="16">
      <el-col :md="12" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span class="section-title" style="margin: 0">当前自动最优（生效中）</span>
              <div style="display: flex; gap: 8px">
                <el-button size="small" :loading="refreshing" @click="doRefresh">
                  <el-icon><Refresh /></el-icon>立即刷新
                </el-button>
                <el-button size="small" :loading="testing" @click="runSelftest">接口自检</el-button>
              </div>
            </div>
          </template>

          <template v-if="best">
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="平台">
                <el-tag size="small" type="primary">{{ best.platform_label }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="国家">{{ best.country_name }}（{{ best.country }}）</el-descriptions-item>
              <el-descriptions-item label="单价">{{ best.price }}<span v-if="best.min_price">（起 {{ best.min_price }}）</span></el-descriptions-item>
              <el-descriptions-item label="库存">{{ best.stock }}<span v-if="best.physical_count != null">（实体 {{ best.physical_count }}）</span></el-descriptions-item>
              <el-descriptions-item label="成功率">
                {{ (best.success_rate * 100).toFixed(0) }}%
                <span class="hint">（{{ best.ok }}/{{ best.attempts }}）</span>
              </el-descriptions-item>
              <el-descriptions-item label="得分">{{ best.score.toFixed(4) }}</el-descriptions-item>
            </el-descriptions>
            <div style="margin-top: 10px">
              <el-button size="small" type="primary" plain @click="applyBest">把当前最优写入配置</el-button>
              <span class="hint" style="margin-left: 8px">
                命中 SMS 时用的就是上面这套；可在「接码配置」里关掉「用任务最优覆盖」。
              </span>
            </div>
          </template>
          <el-alert v-else type="warning" :closable="false" show-icon
            title="暂无可用最优结果：检查各平台 API Key、允许/排除国家、价格区间、库存下限与成功率下限。" />

          <el-divider style="margin: 14px 0" />
          <div class="section-title" style="margin: 0 0 8px">本次实际生效的取号参数</div>
          <el-descriptions v-if="eff" :column="1" border size="small">
            <el-descriptions-item label="来源">
              <el-tag size="small" :type="eff.source === 'monitor' ? 'success' : 'info'">
                {{ eff.source === 'monitor' ? '5s 任务最优' : '手动配置' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="平台顺序">{{ (eff.platform_candidates || []).map((p) => PLATFORM_LABEL[p] || p).join(' → ') || '-' }}</el-descriptions-item>
            <el-descriptions-item label="国家顺序">{{ (eff.country_candidates || []).join(' → ') || '-' }}</el-descriptions-item>
            <el-descriptions-item label="价格区间">{{ eff.min_price || 0 }} ~ {{ eff.max_price || '不限' }}</el-descriptions-item>
            <el-descriptions-item label="phoneException（排除前缀）">
              <span class="mono">{{ eff.phone_exception || '（无）' }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="供货商">
              包含 {{ eff.provider_ids || '不限' }} / 排除 {{ eff.except_provider_ids || '无' }}
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>

      <el-col :md="12" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">平台轮询状态</span></template>
          <el-table :data="Object.entries(platforms).map(([k, v]) => ({ key: k, ...v }))" size="small" stripe>
            <el-table-column label="平台" width="110">
              <template #default="{ row }">{{ row.label }}</template>
            </el-table-column>
            <el-table-column label="Key" width="80">
              <template #default="{ row }">
                <el-tag size="small" :type="row.configured ? 'success' : 'info'">
                  {{ row.configured ? '已配置' : '未配置' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最近轮询" width="150">
              <template #default="{ row }">{{ fmtTime(row.last_poll_at) }}</template>
            </el-table-column>
            <el-table-column label="余额" width="90">
              <template #default="{ row }">{{ row.balance ?? '-' }}</template>
            </el-table-column>
            <el-table-column label="错误" min-width="140" show-overflow-tooltip>
              <template #default="{ row }">
                <span :class="row.last_error ? 'err' : 'hint'">{{ row.last_error || '无' }}</span>
              </template>
            </el-table-column>
          </el-table>
          <div class="hint" style="margin-top: 8px">
            任务每 {{ data?.poll_seconds || 5 }}s 一轮（已跑 {{ data?.rounds || 0 }} 轮）；
            单平台失败只记错误并保留上次快照，不影响其他平台。
            <template v-if="ev.physical_count_available === false">
              <br /><b style="color: var(--el-color-warning)">注意：当前接口没返回 physicalCount，「排除虚拟号」无法生效。</b>
            </template>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never">
      <template #header>
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px">
          <span class="section-title" style="margin: 0">各平台价格明细（{{ rows.length }} 行）</span>
          <div class="hint">
            权重 {{ ev.weight }} · 库存下限 {{ ev.min_stock }} · 成功率下限 {{ ev.min_success_rate }}
            · 平台模式 {{ PLATFORM_LABEL[ev.platform_mode] || ev.platform_mode }}
            <template v-if="ev.physical_only"> · <b>仅实体卡</b></template>
          </div>
        </div>
      </template>
      <el-table :data="rows" size="small" stripe max-height="520" :row-class-name="({ row }) => ROW_TYPE(row) === 'success' ? 'best-row' : ''">
        <el-table-column label="平台" width="100">
          <template #default="{ row }">{{ row.platform_label }}</template>
        </el-table-column>
        <el-table-column label="国家" min-width="140">
          <template #default="{ row }">
            {{ row.country_name || ('国家' + row.country) }}（{{ row.country }}）
            <el-tag v-if="row.openai_safe" size="small" type="success" style="margin-left: 4px">安全</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="price" label="单价" width="90" sortable />
        <el-table-column prop="count" label="库存" width="90" sortable />
        <el-table-column label="实体卡" width="90">
          <template #default="{ row }">{{ row.physical_count ?? '-' }}</template>
        </el-table-column>
        <el-table-column label="成功率" width="110">
          <template #default="{ row }">
            {{ ((row.success_rate || 0) * 100).toFixed(0) }}%
            <span class="hint">（{{ row.ok }}/{{ row.attempts }}）</span>
          </template>
        </el-table-column>
        <el-table-column label="得分" width="90">
          <template #default="{ row }">{{ row.score != null ? row.score.toFixed(4) : '-' }}</template>
        </el-table-column>
        <el-table-column label="状态" min-width="150">
          <template #default="{ row }">
            <el-tag v-if="row.rejected_reason" size="small" type="info" effect="plain">{{ row.rejected_reason }}</el-tag>
            <el-tag v-else-if="best && row.platform === best.platform && row.country === best.country"
                    size="small" type="success">当前最优</el-tag>
            <el-tag v-else size="small" type="warning" effect="plain">候选</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="更新时间" width="150">
          <template #default="{ row }">{{ fmtTime(row.updated_at) }}</template>
        </el-table-column>
        <template #empty><el-empty description="暂无价格数据：配好平台 API Key 后等一轮（5s）" :image-size="70" /></template>
      </el-table>
    </el-card>

    <el-dialog v-model="selftestVisible" title="接码接口自检（只读探测，不买号）" width="820px">
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 12px"
        title="用真实 Key 调查询类接口，打印平台返回的原始字段名 —— 用来确认 physicalCount（排除虚拟号）等字段是否真的存在。" />
      <template v-for="(v, k) in (selftest?.platforms || {})" :key="k">
        <div class="section-title" style="margin: 12px 0 6px">
          {{ PLATFORM_LABEL[k] || k }}
          <el-tag size="small" :type="v.configured ? 'success' : 'info'" style="margin-left: 6px">
            {{ v.configured ? '已配置' : '未配置' }}
          </el-tag>
          <el-tag v-if="v.physical_count_available != null" size="small"
                  :type="v.physical_count_available ? 'success' : 'warning'" style="margin-left: 6px">
            physicalCount {{ v.physical_count_available ? '可用' : '不可用' }}
          </el-tag>
        </div>
        <el-alert v-if="v.error" type="error" :closable="false" :title="v.error" style="margin-bottom: 8px" />
        <el-table :data="v.probes || []" size="small" stripe>
          <el-table-column prop="action" label="action" width="220" />
          <el-table-column prop="status" label="HTTP" width="70" />
          <el-table-column prop="elapsed_ms" label="耗时ms" width="80" />
          <el-table-column label="返回字段" min-width="260">
            <template #default="{ row }">
              <span class="mono" style="font-size: 12px">{{ (row.fields || []).slice(0, 12).join(', ') || row.text || row.error || '-' }}</span>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
:deep(.best-row) { background: var(--el-color-success-light-9); }
</style>
