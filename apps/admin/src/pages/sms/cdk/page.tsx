import { ModuleCdkPage, cdkError, cdkTag, cdkTime, type CdkRow, type ModuleCdkConfig } from '../../../components/module-cdk-page';
import { formatDateTime } from '../../../lib/format-datetime';
import { SMS_KEY_STATUS_LABEL, smsKeyStatusTone } from '../../../lib/labels';

type SmsKeyRow = CdkRow & {
  attemptCount?: number;
  maxAttempts?: number | null;
  smsService?: string | null;
  smsServiceName?: string | null;
  smsCountry?: number | string | null;
  smsCountryName?: string | null;
  issuedAt?: string | null;
  expiresAt?: string | null;
  usedAt?: string | null;
  skuName?: string | null;
  lastErrorCode?: string | null;
};

const config: ModuleCdkConfig<SmsKeyRow> = {
  title: '接码卡密管理',
  description: '接码业务的卡密台账：状态、服务与国家、批次、有效期与最近错误。列表只显示脱敏卡密，完整卡密需要导出权限（在详情里复制）。',
  endpoint: '/api/v1/admin/sms/redeems',
  extract: (data, meta) => ({
    items: Array.isArray(data) ? (data as SmsKeyRow[]) : [],
    total: Number((meta as { total?: number } | undefined)?.total || 0),
  }),
  statusFilter: {
    param: 'status',
    options: Object.entries(SMS_KEY_STATUS_LABEL).map(([value, label]) => ({ value, label })),
  },
  cdkParam: 'cdk',
  // 状态文案与配色都走 lib/labels.ts 的共用映射（未使用=橘黄，终态「已核销」）。
  statusLabel: SMS_KEY_STATUS_LABEL,
  statusTone: smsKeyStatusTone,
  columns: [
    {
      title: '服务 / 国家',
      width: 180,
      render: (_, row) =>
        cdkTag(
          [row.smsServiceName || row.smsService, row.smsCountryName || row.smsCountry]
            .filter((value) => value != null && String(value) !== '')
            .join(' · '),
        ),
    },
    { title: '套餐', dataIndex: 'skuName', width: 140, ellipsis: true, render: (value) => value || '—' },
    {
      title: '尝试',
      width: 90,
      render: (_, row) =>
        row.maxAttempts != null ? `${row.attemptCount || 0}/${row.maxAttempts}` : row.attemptCount || 0,
    },
    { title: '发卡', dataIndex: 'issuedAt', width: 170, render: (value) => cdkTime(value) },
    { title: '过期', dataIndex: 'expiresAt', width: 170, render: (value) => cdkTime(value) },
    { title: '最近错误', dataIndex: 'lastErrorCode', width: 200, ellipsis: true, render: (value) => cdkError(value) },
  ],
  detailGroups: (row) => [
    {
      key: 'usage',
      title: '使用',
      items: [
        { key: 'service', label: '服务', children: row.smsServiceName || row.smsService || '—' },
        { key: 'country', label: '国家', children: String(row.smsCountryName ?? row.smsCountry ?? '—') },
        { key: 'sku', label: '套餐', children: row.skuName || '—' },
        {
          key: 'attempt',
          label: '尝试次数',
          children: row.maxAttempts != null ? `${row.attemptCount || 0}/${row.maxAttempts}` : String(row.attemptCount || 0),
        },
        { key: 'issued', label: '发卡时间', children: formatDateTime(row.issuedAt) },
        { key: 'expires', label: '过期时间', children: formatDateTime(row.expiresAt) },
        { key: 'used', label: '核销时间', children: row.usedAt ? formatDateTime(row.usedAt) : '—' },
        { key: 'error', label: '最近错误', children: cdkError(row.lastErrorCode) },
      ],
    },
  ],
};

export default function SmsCdkPage() {
  return <ModuleCdkPage config={config} />;
}
