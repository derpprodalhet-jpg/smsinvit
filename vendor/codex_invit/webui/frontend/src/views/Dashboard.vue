<script setup>
import { computed, onActivated, onMounted, ref } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useStatsStore } from '@/stores/stats'
import { useRuntimeStore } from '@/stores/runtime'
import { getOverview } from '@/api/cdk'
import { fmtTime } from '@/api/request'
import StatusDot from '@/components/StatusDot.vue'

const router = useRouter()
const { stats } = storeToRefs(useStatsStore())
const { dataVersion } = storeToRefs(useRuntimeStore())

const ov = ref(null)
const loading = ref(false)

const cards = computed(() => [
  { label: '号池总计', value: stats.value.total, color: 'var(--brand)', icon: 'Files' },
  { label: '可用 available', value: stats.value.available, color: '#4caf50', icon: 'CircleCheck' },
  { label: '进行中 in_use', value: stats.value.in_use, color: '#ff9800', icon: 'Loading' },
  { label: '已完成 done', value: stats.value.done, color: '#2196f3', icon: 'Select' },
  { label: '失败 failed', value: stats.value.failed, color: '#e53935', icon: 'CircleClose' },
])

const cdkCards = computed(() => {
  const c = ov.value?.cdk || {}
  return [
    { label: '未使用', value: c.unused || 0, color: '#909399' },
    { label: '已兑换', value: c.bound || 0, color: '#409eff' },
    { label: '排队中', value: c.queued || 0, color: '#e6a23c' },
    { label: '执行中', value: c.running || 0, color: '#ff9800' },
    { label: '已完成', value: c.done || 0, color: '#4caf50' },
    { label: '失败', value: c.failed || 0, color: '#f56c6c' },
    { label: '可兑换邮箱', value: c.mail_pool || 0, color: 'var(--brand)' },
  ]
})

const proxyRows = computed(() => {
  const p = ov.value?.proxies || {}
  return [
    { label: '可用', value: p.ready || 0, type: 'success' },
    { label: '冻结中', value: p.frozen || 0, type: 'warning' },
    { label: '永久不可用', value: p.dead || 0, type: 'danger' },
    { label: '已禁用', value: p.disabled || 0, type: 'info' },
  ]
})

const best = computed(() => ov.value?.sms?.best || null)

async function load() {
  loading.value = true
  try {
    ov.value = await getOverview()
  } catch (e) {
    ElMessage.error('加载概览失败: ' + e.message)
  } finally {
    loading.value = false
  }
}

onMounted(load)
onActivated(load)
</script>

<template>
  <div class="page" v-loading="loading">
    <el-row :gutter="16">
      <el-col v-for="c in cards" :key="c.label" :xs="12" :sm="8" :md="4" style="margin-bottom: 16px">
        <el-card class="stat-card" shadow="hover">
          <div style="display: flex; align-items: center; justify-content: space-between">
            <div>
              <div class="stat-value" :style="{ color: c.color }">{{ c.value }}</div>
              <div class="stat-label">{{ c.label }}</div>
            </div>
            <el-icon :size="30" :style="{ color: c.color, opacity: 0.5 }"><component :is="c.icon" /></el-icon>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :md="14" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span class="section-title" style="margin: 0">卡密 / 邀请概览</span>
              <el-button size="small" text type="primary" @click="router.push('/cdk')">前往卡密管理</el-button>
            </div>
          </template>
          <el-row :gutter="12">
            <el-col v-for="c in cdkCards" :key="c.label" :xs="12" :sm="6" style="margin-bottom: 12px">
              <div class="mini-card">
                <div class="mini-value" :style="{ color: c.color }">{{ c.value }}</div>
                <div class="mini-label">{{ c.label }}</div>
              </div>
            </el-col>
          </el-row>
          <div class="hint" style="margin-top: 4px">
            「可兑换邮箱」= 号池里 available 且没被任何卡密占用的邮箱；自建来源（CF 临时邮箱）不限量。
          </div>
        </el-card>
      </el-col>

      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span class="section-title" style="margin: 0">代理池健康度</span>
              <el-button size="small" text type="primary" @click="router.push('/proxy')">前往代理池</el-button>
            </div>
          </template>
          <el-space wrap>
            <el-tag v-for="p in proxyRows" :key="p.label" :type="p.type" effect="plain">
              {{ p.label }} <b>{{ p.value }}</b>
            </el-tag>
          </el-space>
          <div class="hint" style="margin-top: 8px">
            规则：连续 2 次 409 → 冻结 20 分钟；解冻后再 1 次 409 → 永久不可用（需手动重置）。
          </div>
          <el-divider style="margin: 12px 0" />
          <div class="section-title" style="margin: 0 0 8px">最优接码（5s 任务）</div>
          <template v-if="best">
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="平台">{{ best.platform_label }}</el-descriptions-item>
              <el-descriptions-item label="国家">{{ best.country_name }}（{{ best.country }}）</el-descriptions-item>
              <el-descriptions-item label="单价">{{ best.price }}</el-descriptions-item>
              <el-descriptions-item label="库存">{{ best.stock }}</el-descriptions-item>
              <el-descriptions-item label="成功率">{{ (best.success_rate * 100).toFixed(0) }}%</el-descriptions-item>
              <el-descriptions-item label="得分">{{ best.score.toFixed(3) }}</el-descriptions-item>
            </el-descriptions>
            <div class="hint" style="margin-top: 6px">
              命中 SMS 时**就用这个平台/国家/价格**取号（可在「接码配置」里关掉自动择优）。
            </div>
          </template>
          <el-empty v-else description="暂无可用最优结果（检查 API Key / 国家 / 价格 / 库存过滤）" :image-size="60" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16">
      <el-col :md="12" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">最近运行</span></template>
          <el-table :data="ov?.runs || []" size="small" stripe max-height="260">
            <el-table-column prop="run_id" label="run_id" width="130">
              <template #default="{ row }"><span class="mono">{{ row.run_id }}</span></template>
            </el-table-column>
            <el-table-column prop="kind" label="类型" width="100" />
            <el-table-column prop="email" label="邮箱" min-width="160" show-overflow-tooltip />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <StatusDot
                  :type="{ done: 'primary', failed: 'danger', running: 'warning' }[row.status] || 'info'"
                  :text="row.status"
                />
              </template>
            </el-table-column>
            <el-table-column label="开始" width="150">
              <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
            </el-table-column>
            <template #empty><el-empty description="暂无运行记录" :image-size="60" /></template>
          </el-table>
        </el-card>
      </el-col>
      <el-col :md="12" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">快捷操作</span></template>
          <el-space wrap>
            <el-button @click="router.push('/import')"><el-icon><Upload /></el-icon>导入邮箱</el-button>
            <el-button @click="router.push('/register')"><el-icon><VideoPlay /></el-icon>单次注册</el-button>
            <el-button @click="router.push('/pool')"><el-icon><Files /></el-icon>邮箱列表</el-button>
            <el-button @click="router.push('/cdk')"><el-icon><Ticket /></el-icon>卡密管理</el-button>
            <el-button @click="router.push('/settings/sms-price')"><el-icon><TrendCharts /></el-icon>接码价格</el-button>
          </el-space>
          <el-divider style="margin: 12px 0" />
          <div class="hint">
            邮箱来源：<b>{{ ov?.mail_source || '-' }}</b> ·
            可兑换邮箱：<b>{{ ov?.pool_available ?? '-' }}</b> ·
            价格任务：每 {{ ov?.sms?.poll_seconds || 5 }}s 一轮（已跑 {{ ov?.sms?.rounds || 0 }} 轮）
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<style scoped>
.mini-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
  text-align: center;
}
.mini-value { font-size: 22px; font-weight: 600; }
.mini-label { font-size: 12px; color: var(--el-text-color-secondary); margin-top: 2px; }
</style>
