<script setup>
import { computed, onActivated, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getSmsConfig, saveSmsConfig, testSms, getSmsAllCountries } from '@/api/settings'
import { smsSelftest } from '@/api/cdk'
import FooterToolbar from '@/components/FooterToolbar.vue'

// ── 平台 / Key ──
const enabled = ref(false)
const platformMode = ref('auto')
const apiKeySmsbower = ref('')
const apiKeyHerosms = ref('')
const apiKeySmspool = ref('')
const phCommon = ref('粘贴通用 API Key（两个平台共用）')
const phSmsbower = ref('粘贴 SmsBower API Key')
const phHerosms = ref('粘贴 HeroSMS API Key')
const phSmspool = ref('粘贴 SMSPool API Key')
const service = ref('dr')
const PLATFORM_LABEL = { auto: '自动择优', smsbower: 'SmsBower', herosms: 'HeroSMS', smspool: 'SMSPool' }

// ── 国家 ──
const country = ref('')      // 空 = 不限制国家（绝不预置 52）
const allowed = ref([])
const excluded = ref([])
const strictWhitelist = ref(false)

// ── 号码前缀 ──
const blockedPrefixes = ref('')
const prefixMaxRetry = ref('5')

// ── 价格 / 库存 ──
const minPrice = ref('')
const maxPrice = ref('')
const priceMargin = ref('0')
const autoMinStock = ref('20')
const autoMaxPrice = ref('')
const autoTopN = ref('60')
const physicalOnly = ref(false)

// ── 供货商 ──
const includeProviders = ref('')
const excludeProviders = ref('')

// ── 自动择优（5s 任务）──
const useMonitorBest = ref(true)
const priceWeight = ref(0.5)
const minSuccessRate = ref('0')
const pollSeconds = ref('5')
const autoCountry = ref(false)

// ── 复用 / 重试 ──
const reusePhone = ref(false)
const phoneSuccessMax = ref('3')
const maxPhoneAttempts = ref('')
const perPhoneTimeout = ref('80')

const allCountries = ref([])
const countriesLoading = ref(false)
const saving = ref(false)
const testing = ref(false)
const selfTesting = ref(false)

const countryOptions = computed(() =>
  allCountries.value.map((c) => ({
    value: c.id,
    label: `${c.id}·${c.name_cn}${c.price != null ? ` (${c.price}/${c.count})` : ''}`,
    safe: c.openai_sms_safe,
  })),
)

async function loadCountries(p) {
  countriesLoading.value = true
  try {
    const r = await getSmsAllCountries(p || '')
    allCountries.value = r.countries || []
  } catch (e) {
    console.error('加载国家列表失败:', e)
  } finally { countriesLoading.value = false }
}

const parseList = (s) => String(s || '').split(',').map((x) => x.trim()).filter(Boolean)

async function load() {
  try {
    const { config } = await getSmsConfig()
    await loadCountries('')
    enabled.value = config.sms_enabled === '1'
    platformMode.value = config.sms_platform_mode || 'auto'
    apiKeySmsbower.value = ''
    apiKeyHerosms.value = ''
    apiKeySmspool.value = ''
    phSmsbower.value = config.sms_api_key_smsbower === '***' ? '已设置（留空不修改）' : '粘贴 SmsBower API Key'
    phHerosms.value = config.sms_api_key_herosms === '***' ? '已设置（留空不修改）' : '粘贴 HeroSMS API Key'
    phSmspool.value = config.sms_api_key_smspool === '***' ? '已设置（留空不修改）' : '粘贴 SMSPool API Key'
    service.value = config.sms_service || 'dr'
    country.value = config.sms_country || ''   // 空就是空，别塞默认值
    allowed.value = parseList(config.sms_allowed_countries)
    excluded.value = parseList(config.sms_excluded_countries)
    strictWhitelist.value = config.sms_strict_whitelist === '1'
    blockedPrefixes.value = config.sms_blocked_prefixes || ''
    prefixMaxRetry.value = config.sms_prefix_max_retry || '5'
    minPrice.value = config.sms_min_price || ''
    maxPrice.value = config.sms_max_price || ''
    priceMargin.value = config.sms_price_margin || '0'
    autoMinStock.value = config.sms_auto_min_stock || '20'
    autoMaxPrice.value = config.sms_auto_max_price || ''
    autoTopN.value = config.sms_auto_top_n || '60'
    physicalOnly.value = config.sms_physical_only === '1'
    includeProviders.value = config.sms_include_providers || ''
    excludeProviders.value = config.sms_exclude_providers || ''
    useMonitorBest.value = config.sms_use_monitor_best === '1'
    priceWeight.value = Number(config.sms_price_weight ?? 0.5)
    minSuccessRate.value = config.sms_min_success_rate || '0'
    pollSeconds.value = config.sms_price_poll_seconds || '5'
    autoCountry.value = config.sms_auto_country === '1'
    reusePhone.value = config.sms_reuse_phone === '1'
    phoneSuccessMax.value = config.sms_phone_success_max || '3'
    maxPhoneAttempts.value = config.sms_max_phone_attempts || ''
    perPhoneTimeout.value = config.sms_per_phone_timeout || '80'
  } catch (e) { ElMessage.error(e.message) }
}

async function save() {
  saving.value = true
  try {
    await saveSmsConfig({
      sms_enabled: enabled.value ? '1' : '0',
      sms_platform_mode: platformMode.value,
      // 留空 = 传 '***'，后端按「保持原值」处理（别把已存的好 Key 覆盖掉）
      sms_api_key_smsbower: apiKeySmsbower.value.trim() || '***',
      sms_api_key_herosms: apiKeyHerosms.value.trim() || '***',
      sms_api_key_smspool: apiKeySmspool.value.trim() || '***',
      sms_service: service.value.trim() || 'dr',
      sms_country: String(country.value || '').trim(),   // ⚠️ 空 = 不限制国家，绝不能再塞成 '52'
      sms_allowed_countries: allowed.value.join(','),
      sms_excluded_countries: excluded.value.join(','),
      sms_strict_whitelist: strictWhitelist.value ? '1' : '0',
      sms_blocked_prefixes: blockedPrefixes.value.trim(),
      sms_prefix_max_retry: prefixMaxRetry.value.trim() || '5',
      sms_min_price: minPrice.value.trim(),
      sms_max_price: maxPrice.value.trim(),
      sms_price_margin: priceMargin.value.trim() || '0',
      sms_auto_min_stock: autoMinStock.value.trim() || '20',
      sms_auto_max_price: autoMaxPrice.value.trim(),
      sms_auto_top_n: autoTopN.value.trim() || '60',
      sms_physical_only: physicalOnly.value ? '1' : '0',
      sms_include_providers: includeProviders.value.trim(),
      sms_exclude_providers: excludeProviders.value.trim(),
      sms_use_monitor_best: useMonitorBest.value ? '1' : '0',
      sms_price_weight: String(priceWeight.value),
      sms_min_success_rate: minSuccessRate.value.trim() || '0',
      sms_price_poll_seconds: pollSeconds.value.trim() || '5',
      sms_auto_country: autoCountry.value ? '1' : '0',
      sms_reuse_phone: reusePhone.value ? '1' : '0',
      sms_phone_success_max: phoneSuccessMax.value.trim() || '3',
      sms_max_phone_attempts: maxPhoneAttempts.value.trim(),
      sms_per_phone_timeout: perPhoneTimeout.value.trim() || '80',
    })
    ElMessage.success('保存成功')
    setTimeout(load, 300)
  } catch (e) { ElMessage.error(e.message) } finally { saving.value = false }
}

async function test() {
  testing.value = true
  try { const r = await testSms(); ElMessage.success(r.message || '连通正常') }
  catch (e) { ElMessage.error(e.message) } finally { testing.value = false }
}

async function selftest() {
  selfTesting.value = true
  try {
    const r = await smsSelftest()
    const parts = Object.entries(r.platforms || {}).map(([k, v]) =>
      `${PLATFORM_LABEL[k] || k}: ${v.configured ? (v.physical_count_available ? 'physicalCount 可用' : 'physicalCount 不可用') : '未配置'}`)
    ElMessage({ message: parts.join(' · ') || '自检完成', duration: 8000 })
  } catch (e) { ElMessage.error('自检失败: ' + e.message) } finally { selfTesting.value = false }
}

onActivated(() => load())
</script>

<template>
  <div class="page">
    <el-card shadow="never" style="max-width: 960px">
      <template #header><span class="section-title" style="margin: 0">SMS 接码配置</span></template>

      <el-form label-position="top">
        <el-form-item>
          <el-checkbox v-model="enabled">
            <b>启用 SMS 接码</b>（命中 add-phone 时自动租号）
          </el-checkbox>
        </el-form-item>

        <el-divider content-position="left">平台与密钥（各平台独立设置 Key）</el-divider>
        <el-form-item label="平台模式">
          <el-radio-group v-model="platformMode">
            <el-radio value="auto">自动择优（用 5s 价格任务的最优平台）</el-radio>
            <el-radio value="smsbower">只用 SmsBower</el-radio>
            <el-radio value="herosms">只用 HeroSMS</el-radio>
            <el-radio value="smspool">只用 SMSPool</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="SmsBower API Key">
              <el-input v-model="apiKeySmsbower" type="password" show-password :placeholder="phSmsbower" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="HeroSMS API Key">
              <el-input v-model="apiKeyHerosms" type="password" show-password :placeholder="phHerosms" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="SMSPool API Key">
              <el-input v-model="apiKeySmspool" type="password" show-password :placeholder="phSmspool" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="Service 代码（OpenAI = dr）">
          <el-input v-model="service" placeholder="dr" style="max-width: 240px" />
          <div class="hint" style="width: 100%; margin-top: 4px">
            SMSPool 沿用相同的服务代码与国家 ID；每单独立，不支持下方的号码复用与供货商包含/排除设置。
          </div>
        </el-form-item>

        <el-divider content-position="left">国家（白名单 / 黑名单）</el-divider>
        <el-form-item label="默认国家（可留空；留空 = 不限制国家，按价格任务/全量库存挑）">
          <el-select v-model="country" filterable clearable :loading="countriesLoading"
                     placeholder="留空 = 不限制国家（点右侧 × 可清空）" style="width: 100%">
            <el-option v-for="o in countryOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
          <el-button size="small" text type="primary" style="margin-top: 4px"
                     @click="country = ''">清空（不限制国家）</el-button>
        </el-form-item>
        <el-form-item label="允许使用的国家（多选；空=全部可用）">
          <el-select v-model="allowed" multiple filterable clearable collapse-tags collapse-tags-tooltip
                     :loading="countriesLoading" placeholder="搜索国家名称或 ID…" style="width: 100%">
            <el-option v-for="o in countryOptions" :key="o.value" :label="o.label" :value="o.value">
              <span>{{ o.label }}</span>
              <el-tag v-if="o.safe" size="small" type="success" style="margin-left: 6px">安全</el-tag>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="排除国家（多选；优先级高于白名单）">
          <el-select v-model="excluded" multiple filterable clearable collapse-tags collapse-tags-tooltip
                     :loading="countriesLoading" placeholder="这些国家一律不选" style="width: 100%">
            <el-option v-for="o in countryOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item>
          <el-checkbox v-model="strictWhitelist">
            严格白名单：只从 OpenAI SMS 白名单国家（当前只有 52 泰国）里选
          </el-checkbox>
        </el-form-item>

        <el-divider content-position="left">号码前缀排除（官方 phoneException）</el-divider>
        <el-form-item label="排除的号码前缀">
          <el-input v-model="blockedPrefixes" type="textarea" :rows="2" class="mono"
                    placeholder="两种写法都行：52:66,52:81（国家:前缀） 或 5266,5281（国家码+3~6位掩码）" />
          <div class="hint" style="margin-top: 4px">
            会作为 <span class="mono">phoneException</span> 传给平台；取到号之后系统还会**本地再核一遍**，
            命中就取消退款重租（双保险）。
          </div>
        </el-form-item>
        <el-form-item label="前缀命中后最多重租几次（默认 5）">
          <el-input v-model="prefixMaxRetry" type="number" style="max-width: 200px" />
        </el-form-item>

        <el-divider content-position="left">价格 / 库存 / 虚拟号</el-divider>
        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="最低价 minPrice（空=不限）">
              <el-input v-model="minPrice" placeholder="0.05" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="最高价 maxPrice（空=不限）">
              <el-input v-model="maxPrice" placeholder="0.5" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="最优价容差（自动择优时加价）">
              <el-input v-model="priceMargin" placeholder="0" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="库存下限（默认 20）">
              <el-input v-model="autoMinStock" type="number" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="自动选号价格上限（0=不限）">
              <el-input v-model="autoMaxPrice" placeholder="0" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="候选国家数量（默认 60）">
              <el-input v-model="autoTopN" type="number" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item>
          <el-checkbox v-model="physicalOnly">
            <b>排除虚拟号</b>（只按实体卡 physicalCount 计库存；接口没这个字段时该项无法生效）
          </el-checkbox>
        </el-form-item>

        <el-divider content-position="left">供货商过滤（platform / provider）</el-divider>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="只从这些供货商买 providerIds（逗号分隔，空=不限）">
              <el-input v-model="includeProviders" placeholder="1,2,3" />
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item label="排除供货商 exceptProviderIds">
              <el-input v-model="excludeProviders" placeholder="4,5" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-divider content-position="left">自动择优（5s 价格任务）</el-divider>
        <el-form-item>
          <el-checkbox v-model="useMonitorBest">
            <b>用任务算出的最优结果覆盖国家与价格</b>（关掉则只用上面的手动配置）
          </el-checkbox>
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item :label="`价格 / 成功率权重：${priceWeight.toFixed(2)}（1=只看价格，0=只看成功率）`">
              <el-slider v-model="priceWeight" :min="0" :max="1" :step="0.05" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="成功率下限（0=不限）">
              <el-input v-model="minSuccessRate" placeholder="0" />
            </el-form-item>
          </el-col>
          <el-col :span="6">
            <el-form-item label="轮询间隔秒（默认 5）">
              <el-input v-model="pollSeconds" type="number" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item>
          <el-checkbox v-model="autoCountry">
            启用自动选号（没有最优结果时按价格+库存现场挑一个国家）
          </el-checkbox>
        </el-form-item>

        <el-divider content-position="left">号码复用与失败重试</el-divider>
        <el-form-item>
          <el-checkbox v-model="reusePhone"><b>启用号码复用</b>（同一号码成功后可再用，省钱）</el-checkbox>
        </el-form-item>
        <el-row :gutter="16">
          <el-col :span="8">
            <el-form-item label="同号成功复用上限">
              <el-input v-model="phoneSuccessMax" type="number" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="最多换号次数（空=平台默认）">
              <el-input v-model="maxPhoneAttempts" type="number" placeholder="留空使用平台默认" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="单号等待秒数（默认 80）">
              <el-input v-model="perPhoneTimeout" type="number" />
            </el-form-item>
          </el-col>
        </el-row>
      </el-form>
    </el-card>

    <FooterToolbar>
      <template #left>
        平台：{{ PLATFORM_LABEL[platformMode] }}
        · 允许 {{ allowed.length }} / 排除 {{ excluded.length }} 个国家
      </template>
      <el-button :loading="selfTesting" @click="selftest">接口自检</el-button>
      <el-button :loading="testing" @click="test">测试余额</el-button>
      <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
    </FooterToolbar>
  </div>
</template>
