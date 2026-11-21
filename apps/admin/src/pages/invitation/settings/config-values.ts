export type InvitationConfig = Record<string, string>;
export type ConfigField = {
  key: string;
  label: string;
  type?:
    'password' | 'number' | 'boolean' | 'select' | 'platforms' | 'countries' | 'country' | 'textarea' | 'weight';
  help?: string;
  placeholder?: string;
  required?: boolean;
  min?: number;
  max?: number;
  options?: { value: string; label: string }[];
};

export function isInvitationMask(value: unknown): boolean {
  return typeof value === 'string' && /^(?:\*{3,}|\[REDACTED\]|<masked>|•+)$/i.test(value.trim());
}

export function configDraft(config: InvitationConfig, fields: ConfigField[]): InvitationConfig {
  return Object.fromEntries(
    fields.map((field) => [
      field.key,
      field.type === 'password' || isInvitationMask(config[field.key])
        ? ''
        : String(config[field.key] ?? ''),
    ]),
  );
}

/** A blank credential input always means preserve, even for legacy plaintext responses. */
export function changedConfigPayload(
  saved: InvitationConfig,
  draft: InvitationConfig,
  fields: ConfigField[],
): InvitationConfig {
  const result: InvitationConfig = {};
  for (const field of fields) {
    const value = String(draft[field.key] ?? '').trim();
    if (isInvitationMask(value) || (field.type === 'password' && !value)) continue;
    if (isInvitationMask(saved[field.key]) && !value) continue;
    if (value !== String(saved[field.key] ?? '').trim()) result[field.key] = value;
  }
  return result;
}

export const SMS_PLATFORMS = [
  { value: 'smsbower', label: 'SmsBower' },
  { value: 'herosms', label: 'HeroSMS' },
  { value: 'smspool', label: 'SMSPool' },
];

export const SMS_GROUPS: { title: string; fields: ConfigField[] }[] = [
  {
    title: '平台与密钥',
    fields: [
      {
        key: 'sms_enabled',
        label: '启用注册接码',
        type: 'boolean',
        help: '注册命中手机验证时自动租号。',
      },
      {
        key: 'sms_platform_mode',
        label: '接码平台',
        type: 'platforms',
        options: SMS_PLATFORMS,
        help: '不选时比较所有已配置密钥的平台；选一个只在该平台排序，选多个合并排序。',
      },
      ...SMS_PLATFORMS.map(({ value, label }): ConfigField => ({
        key: `sms_api_key_${value}`,
        label: `${label} API Key`,
        type: 'password',
      })),
      {
        key: 'sms_service',
        label: '服务代码',
        placeholder: 'dr',
        help: 'OpenAI 使用 dr。SMSPool 每单独立，不支持供货商过滤。',
      },
    ],
  },
  {
    title: '国家与号码前缀',
    fields: [
      {
        key: 'sms_allowed_countries',
        label: '允许的国家',
        type: 'countries',
        help: '留空允许全部国家；可选择或输入国家 ID，多个国家合并排序。',
      },
      {
        key: 'sms_excluded_countries',
        label: '排除的国家',
        type: 'countries',
        help: '排除国家优先于允许列表。',
      },
      {
        key: 'sms_strict_whitelist',
        label: '仅使用项目经验名单中的国家',
        type: 'boolean',
        help: '名单由项目维护，仅供参考，不能保证发码或收码成功。',
      },
      {
        key: 'sms_blocked_prefixes',
        label: '排除号码前缀',
        type: 'textarea',
        placeholder: '52:66,52:81 或 5266,5281',
        help: '传给平台 phoneException，并在收到号码后再次检查。',
      },
    ],
  },
  {
    title: '价格、库存与供货商',
    fields: [
      { key: 'sms_min_price', label: '最低价（空为不限）', type: 'number', min: 0 },
      { key: 'sms_max_price', label: '最高价（空为不限）', type: 'number', min: 0 },
      { key: 'sms_price_margin', label: '最优价容差', type: 'number', min: 0 },
      { key: 'sms_auto_min_stock', label: '库存下限', type: 'number', min: 0 },
      { key: 'sms_auto_max_price', label: '自动选号价格上限（0 为不限）', type: 'number', min: 0 },
      {
        key: 'sms_physical_only',
        label: '仅实体卡',
        type: 'boolean',
        help: '只按 physicalCount 计库存；接口未返回该字段时无法生效。',
      },
      {
        key: 'sms_log_otp',
        label: '打印验证码',
        type: 'boolean',
        help: '租到的真实号码与收到的验证码会写进该次运行日志（默认开）。'
          + '号码始终打印；关掉后只隐藏验证码本身。',
      },
      {
        key: 'sms_include_providers',
        label: '包含供货商 ID',
        placeholder: '1,2,3',
        help: '逗号分隔，空为不限。',
      },
      { key: 'sms_exclude_providers', label: '排除供货商 ID', placeholder: '4,5' },
    ],
  },
  {
    title: '自动择优',
    fields: [
      {
        key: 'sms_price_weight',
        label: '价格与成功率权重',
        type: 'weight',
        help: '1 只看价格，0 只看成功率。',
      },
      {
        key: 'sms_min_success_rate',
        label: '成功率下限（0 为不限）',
        type: 'number',
        min: 0,
        max: 1,
      },
      { key: 'sms_price_poll_seconds', label: '价格轮询间隔（秒）', type: 'number', min: 2 },
    ],
  },
];

export const EXPORT_GROUPS: { title: string; fields: ConfigField[] }[] = [
  {
    title: 'CPA 面板',
    fields: [
      { key: 'cpa_enabled', label: '启用 CPA 自动导出', type: 'boolean' },
      { key: 'cpa_url', label: 'CPA URL', placeholder: 'https://cpa.example.com' },
      { key: 'cpa_mgmt_key', label: 'CPA 管理密钥', type: 'password' },
      { key: 'cpa_timeout', label: 'CPA 超时（秒）', type: 'number', min: 5, max: 300 },
    ],
  },
  {
    title: 'SUB2API 面板',
    fields: [
      { key: 'sub2api_enabled', label: '启用 SUB2API 自动导出', type: 'boolean' },
      { key: 'sub2api_url', label: 'SUB2API URL', placeholder: 'https://sub2api.example.com' },
      { key: 'sub2api_api_key', label: 'SUB2API 管理员 API Key', type: 'password' },
      { key: 'sub2api_group_ids', label: '分组 ID', placeholder: '2 或 1,2,3' },
      { key: 'sub2api_timeout', label: 'SUB2API 超时（秒）', type: 'number', min: 5, max: 300 },
    ],
  },
];

export const POOL_GROUPS: { title: string; fields: ConfigField[] }[] = [
  {
    title: '账号池自动补号',
    fields: [
      {
        key: 'pool_min_available',
        label: '可兑换账号下限',
        type: 'number',
        min: 0,
        max: 1000,
        help: '待兑换账号低于该数量时，账号工厂自动补号；设为 0 表示不按数量补号。',
      },
      {
        key: 'pool_register_concurrency',
        label: '补号并发',
        type: 'number',
        min: 1,
        max: 10,
        help: '补号使用独立的并发配额（工位 17-32），与邀请的「全局并发上限」（工位 1-16）互不影响，两者不会互相压制。',
      },
      {
        key: 'pool_check_seconds',
        label: '巡检间隔（秒）',
        type: 'number',
        min: 10,
        max: 3600,
      },
      {
        key: 'pool_otp_timeout',
        label: '补号邮箱验证码等待（秒）',
        type: 'number',
        min: 10,
        max: 3600,
        help: '只作用于账号工厂的补号注册，与邀请的「邮箱验证码等待」互不影响。',
      },
    ],
  },
];

export const INVITE_GROUPS: { title: string; fields: ConfigField[] }[] = [
  {
    title: '邀请作业',
    fields: [
      { key: 'invite_wait_timeout', label: '等待邀请信（秒）', type: 'number', min: 10, max: 3600 },
      {
        key: 'invite_max_attempts',
        label: '最多尝试次数',
        type: 'number',
        min: 1,
        max: 20,
        help: '每次尝试切换代理，重试失败仍保留卡密。',
      },
      {
        key: 'invite_otp_timeout',
        label: '邮箱验证码等待（秒）',
        type: 'number',
        min: 10,
        max: 3600,
      },
      {
        key: 'cdk_max_concurrency',
        label: '全局并发上限',
        type: 'number',
        min: 1,
        max: 32,
        help: '改动在执行实例重启后生效。',
      },
      { key: 'invite_want_2fa', label: '为新注册账号绑定 2FA', type: 'boolean' },
    ],
  },
  {
    title: '代理 409 规则',
    fields: [
      { key: 'proxy_409_threshold', label: '连续 409 冻结阈值', type: 'number', min: 1, max: 100 },
      {
        key: 'proxy_freeze_minutes',
        label: '冻结分钟数',
        type: 'number',
        min: 1,
        max: 1440,
        help: '解冻后再次 409 将永久不可用，需要在代理池手动重置。',
      },
    ],
  },
  {
    title: 'Codex CLI 执行',
    fields: [
      {
        key: 'codex_binary',
        label: 'Codex 可执行文件',
        placeholder: 'codex',
        help: '修改执行文件、目录、提示词或模型需要凭证权限，由后端校验。',
      },
      { key: 'cli_timeout', label: 'CLI 超时（秒）', type: 'number', min: 10, max: 3600 },
      { key: 'sayhi_prompt', label: '验证提示词', type: 'textarea' },
      {
        key: 'cookie_jar_sso_max_minutes',
        label: '会话罐免登录窗口（分钟）',
        type: 'number',
        min: 0,
        max: 1440,
        help: '罐在这个时间内可直接免登录授权（实测约 25-30 分钟）。超过窗口的罐不但没用还有害（旧认证 cookie 会让重新登录后卡在授权确认页），所以超过就只回放 chatgpt.com 会话、走重新登录。',
      },
      {
        key: 'invite_direct',
        label: '邀请/授权直连（不走代理）',
        type: 'boolean',
        help: '打开后：邀请与 Codex 授权优先直连，被 Cloudflare 拦（实测会间歇 403）时自动改用池内代理重试一次。实测直连不稳定，建议保持关闭；没有代理时会明确报「代理不可用」。',
      },
      {
        key: 'sayhi_direct',
        label: 'CLI 直连（不走代理）',
        type: 'boolean',
        help: '兑现的最后一步是真跑一轮 codex CLI，默认直连（实测整轮 10s 左右，走慢代理 33s 甚至整轮跑不完）。直连遇到传输类失败（连不上/超时）会自动用本次的代理重试一次；关掉它就只走代理、失败也不回退。',
      },
      {
        key: 'sentinel_behavior_ms',
        label: 'PoW 行为模拟（毫秒）',
        type: 'number',
        min: 0,
        max: 10000,
        help: 'Sentinel 里"模拟人类操作"的补齐等待；事件本身约 2 秒，写死 4200 等于每次白等 1-2 秒。0 = 不补齐。',
      },
      { key: 'sayhi_model', label: '执行模型', help: '使用后端允许的模型标识。' },
      {
        key: 'codex_home_root',
        label: '私有 Codex 工作目录',
        help: '只允许独立临时目录的子目录，留空使用默认目录。',
      },
    ],
  },
];
