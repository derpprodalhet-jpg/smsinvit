/**
 * 全站共用的中文枚举映射。
 *
 * 约定：
 *  - 每个映射只放「确诊过」的枚举值，来源在注释里标出（后端常量/迁移/审计调用点）。
 *  - 取用时一律走 `resolveLabel` / `resolveLabelInsensitive` / `moduleErrorLabel`，
 *    **不要**写 `map[key] || key`：那会在遇到新枚举时把英文漏到界面上。
 */
import { resolveLabel, unknownLabel } from './zh-label';

/**
 * 状态标签的语义色。定义在 lib 里，页面只把枚举交给映射取色，
 * 避免同一种状态在四个卡密列表里各写一套颜色。
 */
export type StatusTone = 'default' | 'success' | 'warning' | 'error' | 'processing';

/** 人工授权的卡密写回进度（`invitation_v2_manual_run_links.sync_status`）。 */
export const CDK_SYNC_LABEL: Record<string, string> = {
  done: '已同步',
  unbound: '未绑定卡密',
  pending: '待同步',
  failed: '同步失败',
};

/** 邀请卡密事件（`invitation_v2_cdk_events.event`，取自写入点）。 */
export const RUN_EVENT_LABEL: Record<string, string> = {
  enqueue: '已入队',
  redeem: '已分配邮箱',
  start: '开始执行',
  invite_poll: '轮询邀请信',
  invite_read_error: '读取邀请信失败',
  invite_mail: '收到邀请信',
  invite_sent: '用户确认已发邀请信',
  proxy_unavailable: '无可用代理',
  authorization_retry_stopped: '授权重试已停止',
  sms_retry_stopped: '接码重试已停止',
  done: '执行完成',
  fail: '执行失败',
  failed: '执行失败',
  error: '执行出错',
  busy: '本次未执行',
  manual_completed: '人工授权写回卡密',
  refund_retry: '退款重试',
  reset: '管理员重置',
  void: '管理员作废',
};

/** 结果待复核的原因（`invitation_v2_task_holds.reason`）。 */
export const HOLD_REASON_LABEL: Record<string, string> = {
  RESTART_UNCERTAIN: '执行进程重启，结果不明',
  ADMIN_STOPPED: '管理员手动停止',
  INTERRUPTED_WRITE: '写入中断，结果不明',
  UNKNOWN_RESULT: '结果不明',
};

/** 操作审计的动作（后端 `audit.record(...)` 的第一段）。 */
export const AUDIT_ACTION_LABEL: Record<string, string> = {
  'inventory.generate': '生成卡密',
  'inventory.batch_export': '导出批次卡密',
  'inventory.batch_reveal': '查看批次卡密明文',
  'inventory.secret_view': '查看卡密明文',
  'inventory.invalidate': '作废卡密',
  'invitation.executor.update': '修改邀请执行开关',
  'invitation.pool.update': '修改账号池补号开关',
  'invitation.cdk.generate': '生成邀请卡密',
  'invitation.permit.manual_release': '人工释放执行占位',
  'invitation.task_hold.resolve': '复核结果不明任务',
  'invitation.queue.redispatch': '重新投递滞留队列',
  'invitation.job.stop': '停止邀请作业',
  'invitation.sms_monitor.update': '修改接码价格监控开关',
  'invitation.sms_refund.update': '修改接码退款开关',
  'invitation.pool.stop': '停止本轮补号',
  'job.retry': '重试持久任务',
  'job.cancel': '取消持久任务',
  'notification.retry': '重发通知',
  'notification.cancel': '取消通知',
  'notification.channel.update': '修改通知通道',
  'notification.test.enqueue': '发送通道测试',
  'notification.secret_viewed': '查看通知正文',
  'plan.create': '新增套餐',
  'plan.update': '修改套餐',
  'plan.delete': '删除套餐',
  'plan.enable': '启停套餐',
  'setting.update': '修改配置',
  'setting.delete': '删除配置',
  'staff.create': '新增管理员',
  'staff.delete': '删除管理员',
  'staff.password.reset': '重置管理员密码',
  'staff.status.update': '修改管理员状态',
  'sms.settings_updated': '修改接码配置',
  'sms.area.add': '添加接码排除区号',
  'sms.area.delete': '删除接码排除区号',
  ADMIN_IP_BAN_UNBAN: '解除 IP 封禁',
};

/** 审计资源类型。 */
export const AUDIT_RESOURCE_LABEL: Record<string, string> = {
  admin_user: '管理员',
  inventory: '卡密',
  inventory_item: '卡密',
  inventory_batch: '卡密批次',
  sku: '套餐',
  plan: '套餐',
  setting: '配置',
  settings: '配置',
  job: '持久任务',
  notification: '通知',
  sms_redeem_key: '接码卡密',
  sms_excluded_area: '接码排除区号',
  invitation_executor: '邀请执行器',
  invitation_account_pool: '邀请账号池',
  invitation_cdk_job: '邀请作业',
  invitation_run: '邀请运行',
  invitation_task_hold: '结果待复核',
  invitation_execution_permit: '执行占位',
  session: '会话',
  admin_ip_ban: 'IP 封禁',
};

/**
 * 原业务表（迁移前历史遗留数据）里可能出现的字段名。
 *
 * 「迁移前历史」页面已按运营要求下线，但操作审计仍在用它翻译记账明细里的
 * 原始字段名，所以映射与 `legacyFieldLabel` 继续保留。
 */
export const LEGACY_FIELD_LABEL: Record<string, string> = {
  id: '记录 ID',
  run_id: '任务',
  job_id: '作业',
  email: '邮箱',
  code: '卡密',
  batch: '批次',
  note: '备注',
  status: '状态',
  stage: '阶段',
  kind: '类型',
  progress: '进度',
  attempt: '尝试次数',
  attempts: '尝试次数',
  fail_count: '失败次数',
  error: '错误',
  reason: '原因',
  source: '来源',
  proxy: '代理',
  proxy_used: '实际代理',
  phone: '号码',
  sms_code: '验证码',
  password: '密码',
  totp_secret: '2FA 密钥',
  access_token: '访问令牌',
  session_token: '会话令牌',
  refresh_token: '刷新令牌',
  cookie_jar: '会话罐',
  result_json: '结果',
  created_at: '创建时间',
  updated_at: '更新时间',
  started_at: '开始时间',
  finished_at: '结束时间',
  verified_at: '核验时间',
  invite_sent_at: '发出邀请信',
  invite_processed_at: '处理邀请信',
  redeemed_at: '领取时间',
  expires_at: '过期时间',
  imported_at: '导入时间',
  phone_bound_at: '接码时间',
  cdk_bound_at: '绑定卡密时间',
  cli_completed_at: 'CLI 完成时间',
  account_id: '账号 ID',
  inviter_email: '邀请方邮箱',
  inviter_subject: '邀请信主题',
  invite_link: '邀请链接',
  last_error: '最近错误',
  last_error_code: '错误码',
  plus_check: 'Plus 检测',
  service: '服务',
  country: '国家',
  owner_pid: '进程号',
  owner_token: '占位令牌',
  instance_id: '实例',
  slot_id: '槽位',
};

/** 管理员角色。 */
export const STAFF_ROLE_LABEL: Record<string, string> = {
  SUPER_ADMIN: '超级管理员',
  ADMIN: '管理员',
};

/** 批次来源（`inventory_import_batches.source` 的 CHECK 值）。 */
export const BATCH_SOURCE_LABEL: Record<string, string> = {
  PASTE: '粘贴导入',
  GENERATE: '后台生成',
};

/**
 * 库存记录状态（`inventory_items.status`，四个业务共用同一张库存表）。
 *
 * 取值来源：建表默认值 `ISSUED`；业务核销后置 `CONSUMED`；
 * `invitation/router.py` 作废后置 `INVALID`。历史数据里还可能出现过期/停用，
 * 所以一并给出中文，任何没见过的取值都会落到「未知（原值）」。
 */
export const INVENTORY_STATUS_LABEL: Record<string, string> = {
  ISSUED: '已发行',
  CONSUMED: '已核销',
  INVALID: '已作废',
  EXPIRED: '已过期',
  DISABLED: '已停用',
  UNUSED: '未使用',
};

/** 库存状态配色：未使用/已发行一律橘黄（四个模块一致），终态绿，作废红。 */
export const INVENTORY_STATUS_TONE: Record<string, StatusTone> = {
  ISSUED: 'warning',
  UNUSED: 'warning',
  CONSUMED: 'success',
  INVALID: 'error',
  EXPIRED: 'warning',
  DISABLED: 'default',
};

export function inventoryStatusLabel(status?: string | null): string {
  return resolveLabel(INVENTORY_STATUS_LABEL, status);
}

export function inventoryStatusTone(status?: string | null): StatusTone {
  return INVENTORY_STATUS_TONE[String(status ?? '').trim()] || 'default';
}

/** 邮箱来源（`mail_providers` 的 kind）。全称用 Tooltip 显示，列表只放短名。 */
export const MAIL_SOURCE_LABEL: Record<string, string> = {
  cf_temp: 'CF临时',
  outlook: 'Outlook',
  icloud_relay: 'iCloud',
  imap: 'IMAP',
};

export function mailSourceLabel(kind?: string | null): string {
  return resolveLabel(MAIL_SOURCE_LABEL, kind);
}

/** 通知类型（`notification_outbox.kind`）。 */
export const NOTIFICATION_KIND_LABEL: Record<string, string> = {
  EMAIL: '邮件',
  TELEGRAM: 'Telegram',
  ORDER_DELIVERED: '订单发货通知',
  PASSWORD_RESET: '重置密码',
  EMAIL_REGISTER_CODE: '注册验证码',
  EMAIL_RESET_CODE: '重置验证码',
  LOW_STOCK: '库存不足提醒',
  TASK_FAILED: '任务失败提醒',
};

/** 通知渠道。 */
export const NOTIFICATION_CHANNEL_LABEL: Record<string, string> = {
  EMAIL: '邮件',
  TELEGRAM: 'Telegram',
  SMS: '短信',
  WEBHOOK: 'Webhook',
};

/** 通知发送状态。`SKIPPED`/`UNCERTAIN` 是 outbox 的既有状态。 */
export const NOTIFICATION_STATUS_LABEL: Record<string, string> = {
  PENDING: '待发送',
  SENDING: '发送中',
  SENT: '已发送',
  FAILED: '发送失败',
  CANCELLED: '已取消',
  SKIPPED: '已跳过',
  UNCERTAIN: '结果不明',
  EXPIRED: '已过期',
};

/**
 * 持久任务类型（`durable_jobs.kind`）。
 * 直充收尾任务已随直充业务下线；后端新增任务类型后在这里补中文。
 */
export const JOB_KIND_LABEL: Record<string, string> = {};

/** 持久任务状态。 */
export const JOB_STATUS_LABEL: Record<string, string> = {
  PENDING: '待执行',
  RUNNING: '执行中',
  FAILED: '已失败',
  SUCCEEDED: '已完成',
  CANCELLED: '已取消',
};

/** 定时任务分组与状态。分组取值只可能来自已注册任务（worker/notification）。 */
export const SCHEDULER_GROUP_LABEL: Record<string, string> = {
  worker: '后台任务',
  notification: '通知',
  default: '默认',
};

export const SCHEDULER_STATUS_LABEL: Record<string, string> = {
  RUNNING: '正在执行',
  IDLE: '待首次执行',
  DISABLED: '已关闭',
  ERROR: '最近出错',
  STALE: '可能停滞',
};

/** 通用模块错误码 → 中文。覆盖接码 / 邀请的对外错误码。 */
export const MODULE_ERROR_LABEL: Record<string, string> = {
  // 通知（Telegram）。库里只存错误码，所以「怎么修」必须写在这里 ——
  // 运营看到的不是「未知（TELEGRAM_REJECTED）」，而是下一步该做什么。
  TELEGRAM_CHAT_NOT_FOUND: '机器人访问不到这个会话：把机器人拉进群/频道，并在群里发一条消息后再复制 Chat ID',
  TELEGRAM_CHAT_EMPTY: 'Chat ID 为空，请填写运营群/频道的 Chat ID',
  TELEGRAM_BOT_BLOCKED: '机器人被该会话拉黑，请先解除拉黑',
  TELEGRAM_BOT_NOT_MEMBER: '机器人不在该群/频道里，请把它拉进去',
  TELEGRAM_NO_RIGHTS: '机器人没有发言权限，请在群里给它发送消息的权限',
  TELEGRAM_TOKEN_INVALID: 'Bot Token 无效，请重新复制粘贴',
  TELEGRAM_TOPIC_CLOSED: '目标话题已关闭，无法发送',
  TELEGRAM_MESSAGE_TOO_LONG: '消息过长被 Telegram 拒收',
  TELEGRAM_REJECTED: 'Telegram 拒绝发送，请检查通道配置',
  TELEGRAM_CONFIG_INVALID: 'Telegram 配置格式无效（Bot Token 或 Chat ID）',
  TELEGRAM_DISABLED: 'Telegram 通道未配置',
  TELEGRAM_RESULT_UNCERTAIN: '发送结果不明确，需要人工核查是否已发出',

  // 接码
  SMS_UNAVAILABLE: '接码服务暂时不可用',
  SMS_PROVIDER_ERROR: '接码服务暂时异常，请稍后再试',
  SMS_CDK_NOT_FOUND: '卡密无效或不存在',
  SMS_CDK_USED: '该卡密已使用成功',
  SMS_CDK_FAILED: '该卡密已使用失败',
  SMS_CDK_EXPIRED: '该卡密已过期',
  SMS_CDK_DISABLED: '该卡密已被停用或作废',
  SMS_CDK_AMBIGUOUS: '该卡密存在重复记录，请核查',
  SMS_SESSION_NOT_FOUND: '接码会话不存在',
  SMS_SESSION_STATE: '当前会话状态不可操作',
  SMS_ALREADY_LISTENING: '该会话已在等待验证码',
  SMS_CANCEL_TOO_EARLY: '号码获取后需等待约 2 分钟才能换号',
  SMS_MAX_CODES: '本会话已达到验证码数量上限',
  SMS_NEXT_CLOSED: '该会话已不再接收下一封验证码',
  SMS_NUMBER_EXPIRED: '号码已过期',
  SMS_NUMBER_INVALID: '号码不可用',
  SMS_RESULT_PENDING: '结果尚未确定',
  SMS_SKU_MISCONFIGURED: '接码套餐缺少服务 / 国家 / 价格带配置',
  SMS_WEBHOOK_FORBIDDEN: '接码回调校验失败',
  SMS_WRONG_EXCEPTION_PHONE: '该号码不在项目经验名单内',
  // 邀请
  INVITATION_RUNNING: '有执行中的任务，不能修改',  INVITATION_INVALID_STATE: '当前卡密状态不能执行该操作',
  INVITATION_BINDING_CHANGED: '邮箱与卡密绑定已改变',
  INVITATION_RUN_REQUIRED: '需要运行记录',
  INVITATION_RUN_UNBOUND: '未保存卡密关联',
  INVITATION_RUN_ALREADY_BOUND: '该运行已经启动',
  INVITATION_RUN_MISMATCH: '未找到该邮箱的运行记录',
  INVITATION_COMPLETION_UNVERIFIED: '缺少与本次运行一致的完成记录',
  INVITATION_EXECUTOR_UNAVAILABLE: '邀请执行器未开启',
  INVITATION_EXECUTOR_BUSY: '邀请执行器忙，请稍后重试',
  INVITATION_TASK_HOLD: '任务结果待复核',
  INVITATION_OUTBOUND_FORBIDDEN: '出站主机未获部署授权',
  // 管理员操作留下的标记与租约类原因
  ADMIN_CANCELLED: '管理员取消',
  ADMIN_STOPPED: '管理员停止',
  ADMIN_RELEASED: '管理员人工释放',
  LEASE_EXPIRED: '执行超时，已释放',
  INTERRUPTED_WRITE: '写入中断，结果不明',
};

/** 错误码 → 中文：先查模块表，再退回错误码表，最后是「未知（码）」。 */
export function moduleErrorLabel(
  code?: string | null,
  extra: Record<string, string> = {},
): string {
  const value = String(code ?? '').trim();
  if (!value) return '—';
  if (extra[value]) return extra[value];
  if (MODULE_ERROR_LABEL[value]) return MODULE_ERROR_LABEL[value];
  return unknownLabel(value);
}

export function cdkSyncLabel(status?: string | null): string {
  return resolveLabel(CDK_SYNC_LABEL, status);
}

export function runEventLabel(event?: string | null): string {
  return resolveLabel(RUN_EVENT_LABEL, event);
}

export function holdReasonLabel(reason?: string | null): string {
  return resolveLabel(HOLD_REASON_LABEL, reason);
}

export function auditActionLabel(action?: string | null): string {
  return resolveLabel(AUDIT_ACTION_LABEL, action);
}

export function auditResourceLabel(resource?: string | null): string {
  return resolveLabel(AUDIT_RESOURCE_LABEL, resource);
}

export function legacyFieldLabel(field?: string | null): string {
  return resolveLabel(LEGACY_FIELD_LABEL, field);
}

export function staffRoleLabel(role?: string | null): string {
  return resolveLabel(STAFF_ROLE_LABEL, role);
}

export function notificationKindLabel(kind?: string | null): string {
  return resolveLabel(NOTIFICATION_KIND_LABEL, kind);
}

export function notificationChannelLabel(channel?: string | null): string {
  return resolveLabel(NOTIFICATION_CHANNEL_LABEL, channel);
}

export function notificationStatusLabel(status?: string | null): string {
  return resolveLabel(NOTIFICATION_STATUS_LABEL, status);
}

export function jobKindLabel(kind?: string | null): string {
  return resolveLabel(JOB_KIND_LABEL, kind);
}

export function jobStatusLabel(status?: string | null): string {
  return resolveLabel(JOB_STATUS_LABEL, status);
}

export function schedulerGroupLabel(group?: string | null): string {
  return resolveLabel(SCHEDULER_GROUP_LABEL, group);
}

export function schedulerStatusLabel(status?: string | null): string {
  return resolveLabel(SCHEDULER_STATUS_LABEL, status);
}
