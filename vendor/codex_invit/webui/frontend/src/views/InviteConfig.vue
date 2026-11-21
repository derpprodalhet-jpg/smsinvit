<script setup>
import { onActivated, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getInviteConfig, saveInviteConfig, runtimeCheck } from '@/api/settings'
import { getMailPool } from '@/api/cdk'
import { getOverview } from '@/api/cdk'
import FooterToolbar from '@/components/FooterToolbar.vue'

const cfg = ref({
  invite_wait_timeout: '150',
  invite_max_attempts: '3',
  invite_want_2fa: '1',
  invite_otp_timeout: '120',
  cdk_max_concurrency: '3',
  proxy_409_threshold: '2',
  proxy_freeze_minutes: '20',
  codex_binary: 'codex',
  sayhi_prompt: 'say hi',
  sayhi_model: '',
  cli_timeout: '180',
  codex_home_root: '',
})

const check = ref(null)
const checking = ref(false)
const saving = ref(false)
const pool = ref(null)
const ov = ref(null)

function toBool(v) { return v === '1' || v === true }
function fromBool(v) { return v ? '1' : '0' }

async function load() {
  try {
    const r = await getInviteConfig()
    cfg.value = { ...cfg.value, ...(r.config || {}) }
  } catch (e) { ElMessage.error('加载失败: ' + e.message) }
  try { pool.value = await getMailPool(20) } catch (_) {}
  try { ov.value = await getOverview() } catch (_) {}
}

async function save() {
  saving.value = true
  try {
    await saveInviteConfig({ ...cfg.value })
    ElMessage.success('保存成功（并发上限改动需重启用户前台生效）')
    await load()
  } catch (e) { ElMessage.error(e.message) } finally { saving.value = false }
}

async function runCheck() {
  checking.value = true
  try {
    check.value = await runtimeCheck()
    ElMessage.success('自检完成')
  } catch (e) { ElMessage.error('自检失败: ' + e.message) } finally { checking.value = false }
}

onActivated(load)
</script>

<template>
  <div class="page">
    <el-row :gutter="16">
      <el-col :md="14" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header><span class="section-title" style="margin: 0">邀请作业</span></template>
          <el-form label-position="top">
            <el-row :gutter="16">
              <el-col :span="12">
                <el-form-item label="等待邀请信秒数（默认 150）">
                  <el-input v-model="cfg.invite_wait_timeout" type="number" />
                  <div class="hint">「没有取到就等 150s」就是这里；等不到本次作业失败，用户可稍后重试。</div>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="最多尝试次数（默认 3）">
                  <el-input v-model="cfg.invite_max_attempts" type="number" />
                  <div class="hint">每次尝试换一个代理 IP；3 次都失败才提示用户「稍后重试」，卡密**不作废**。</div>
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="12">
                <el-form-item label="邮箱验证码等待秒数（默认 120）">
                  <el-input v-model="cfg.invite_otp_timeout" type="number" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="全局并发上限（默认 3）">
                  <el-input v-model="cfg.cdk_max_concurrency" type="number" />
                  <div class="hint">超出排队的任务在用户前台显示「正在排队（前面还有 N 个）」；改动需重启用户前台。</div>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item>
              <el-checkbox :model-value="toBool(cfg.invite_want_2fa)"
                           @update:model-value="(v) => cfg.invite_want_2fa = fromBool(v)">
                作业注册出的新号也绑定 2FA
              </el-checkbox>
              <div class="hint">
                默认关闭：这些号只用来导出 SUB2API，凭证不下发给用户，绑 2FA 收益低且不可逆。
              </div>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never" style="margin-top: 16px">
          <template #header><span class="section-title" style="margin: 0">Codex CLI（sayhi 成功判据）</span></template>
          <el-form label-position="top">
            <el-row :gutter="16">
              <el-col :span="12">
                <el-form-item label="codex 可执行文件（默认 codex）">
                  <el-input v-model="cfg.codex_binary" placeholder="codex 或 /usr/local/bin/codex" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="CLI 超时秒数（默认 180）">
                  <el-input v-model="cfg.cli_timeout" type="number" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="16">
              <el-col :span="12">
                <el-form-item label="sayhi 提示词（默认 say hi）">
                  <el-input v-model="cfg.sayhi_prompt" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="模型（留空=CLI 默认）">
                  <el-input v-model="cfg.sayhi_model" placeholder="留空用 CLI 默认模型" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item label="私有 CODEX_HOME 根目录（留空=webui/codex_home）">
              <el-input v-model="cfg.codex_home_root" placeholder="留空使用默认目录" />
            </el-form-item>
            <el-alert
              type="info" :closable="false" show-icon
              title="成功判据只有一条：CLI 完整跑完一个回合（turn.started → 非空回复 → turn.completed，且无 turn.failed/error）。SUB2API 导出失败不算失败。"
            />
          </el-form>
        </el-card>
      </el-col>

      <el-col :md="10" style="margin-bottom: 16px">
        <el-card shadow="never">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span class="section-title" style="margin: 0">代理 409 规则</span>
              <el-button size="small" :loading="checking" @click="runCheck">运行依赖自检</el-button>
            </div>
          </template>
          <el-form label-position="top">
            <el-row :gutter="16">
              <el-col :span="12">
                <el-form-item label="连续几次 409 触发冻结（默认 2）">
                  <el-input v-model="cfg.proxy_409_threshold" type="number" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item label="冻结分钟数（默认 20）">
                  <el-input v-model="cfg.proxy_freeze_minutes" type="number" />
                </el-form-item>
              </el-col>
            </el-row>
          </el-form>
          <el-alert
            type="warning" :closable="false" show-icon
            title="连续 N 次 409 → 冻结；解冻后再 1 次 409 → 永久不可用（只能到代理池页手动重置）。"
          />

          <el-divider style="margin: 14px 0" />
          <div class="section-title" style="margin: 0 0 8px">运行依赖自检</div>
          <el-descriptions v-if="check" :column="1" border size="small">
            <el-descriptions-item label="node">
              <span class="mono">{{ check.node || '未找到' }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="codex">
              <span class="mono">{{ check.codex || ('未找到 ' + check.codex_binary_setting) }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="curl_cffi">{{ check.curl_cffi }}</el-descriptions-item>
            <el-descriptions-item label="CODEX_HOME">
              <span class="mono">{{ check.codex_home }}</span>
              <el-tag size="small" :type="check.codex_home_writable ? 'success' : 'danger'" style="margin-left: 6px">
                {{ check.codex_home_writable ? '可写' : '不可写' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="接码平台 Key">
              <el-tag v-for="(v, k) in check.sms_platforms" :key="k" size="small"
                      :type="v ? 'success' : 'info'" style="margin-right: 6px">
                {{ k }}: {{ v ? '已配置' : '未配置' }}
              </el-tag>
            </el-descriptions-item>
          </el-descriptions>
          <el-empty v-else description="点右上角「运行依赖自检」检查 node / codex / curl_cffi" :image-size="60" />
        </el-card>

        <el-card shadow="never" style="margin-top: 16px">
          <template #header>
            <div style="display: flex; align-items: center; justify-content: space-between">
              <span class="section-title" style="margin: 0">可兑换邮箱池</span>
              <el-tag size="small" type="success" effect="plain">
                可用 {{ pool?.available === -1 ? '不限量' : (pool?.available ?? '-') }}
              </el-tag>
            </div>
          </template>
          <div class="hint" style="margin-bottom: 8px">
            来源 <b>{{ pool?.mail_source || '-' }}</b> ·
            {{ pool?.pooled ? '池化来源：兑换时从号池随机取一个未使用的' : '自建来源：兑换时现场生成新地址' }}
          </div>
          <el-table :data="pool?.items || []" size="small" stripe max-height="240">
            <el-table-column prop="email" label="邮箱" min-width="200" show-overflow-tooltip />
            <el-table-column prop="kind" label="来源" width="110" />
            <el-table-column label="导入时间" width="150">
              <template #default="{ row }">{{ row.imported_at ? new Date(row.imported_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '-' }}</template>
            </el-table-column>
            <template #empty><el-empty description="号池里没有可用邮箱（去「导入邮箱」补货）" :image-size="60" /></template>
          </el-table>
          <el-divider style="margin: 12px 0" />
          <div class="hint">
            作业状态：运行 {{ ov?.cdk?.running || 0 }} · 排队 {{ ov?.cdk?.queued || 0 }} ·
            已完成 {{ ov?.cdk?.done || 0 }} · 失败 {{ ov?.cdk?.failed || 0 }}
          </div>
        </el-card>
      </el-col>
    </el-row>

    <FooterToolbar>
      <template #left>改动「全局并发上限」需重启用户前台（start_user.py）生效</template>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </FooterToolbar>
  </div>
</template>
