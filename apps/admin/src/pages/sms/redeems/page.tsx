import type { ActionType, ProColumns } from '@ant-design/pro-components';
import { ProTable } from '@ant-design/pro-components';
import {
  Button,
  Input,
  Select,
  Space,
  Typography,
  message,
} from 'antd';
import { useEffect, useRef, useState } from 'react';
import { AdminPage } from '../../../components/admin-page';
import { AdminStatusTag, toneFromStatus } from '../../../components/admin-feedback';
import {
  AdminDetailDrawer,
  CopyableValue,
  type DetailGroup,
} from '../../../components/admin-detail-drawer';
import { adminApi } from '../../../lib/admin-api';
import { formatDateTime } from '../../../lib/format-datetime';
import { SMS_KEY_STATUS_LABEL, labelOf } from '../../../lib/labels';
import { unknownLabel } from '../../../lib/zh-label';
import { moduleErrorLabel } from '../../../lib/zh-maps';

type SessionSummary = {
  attempt?: number;
  phone?: string | null;
  phoneMasked?: string | null;
  status?: string | null;
  activationCost?: number | string | null;
  smsCode?: string | null;
  smsCodes?: string[] | null;
  smsText?: string | null;
  nextSmsCode?: string | null;
};

type RedeemRow = {
  id: string;
  status: string;
  attemptCount: number;
  maxAttempts?: number | null;
  smsService: string;
  smsCountry: number | string;
  issuedAt?: string;
  expiresAt?: string;
  usedAt?: string;
  failedAt?: string;
  firstUsedAt?: string | null;
  batchId?: string | null;
  codeFingerprint?: string | null;
  codeFingerprintShort?: string;
  lastPhoneMasked?: string | null;
  lastSessionStatus?: string | null;
  lastSmsCode?: string | null;
  lastSmsText?: string | null;
  nextSmsCode?: string | null;
  lastErrorCode?: string | null;
  confirmedCostUsd?: number | string | null;
  usdCnyRate?: number | string | null;
  costSettlementStatus?: string | null;
  phoneHistory?: string | null;
  smsCodes?: string[] | null;
  sessions?: SessionSummary[] | null;
  smsServiceName?: string | null;
  smsCountryName?: string | null;
  /** 后端新增：卡密库条目与卡密本身。缺字段按缺失容错。 */
  inventoryItemId?: string | null;
  codeMasked?: string | null;
  code?: string | null;
  skuName?: string | null;
};

const CODE_MARKERS = ['①', '②', '③'];

const SESSION_STATUS_LABEL: Record<string, string> = {
  PENDING_NUMBER: '等待取号',
  WAITING_CODE: '等待验证码',
  SUCCESS: '已收到验证码',
  CANCELLED: '已取消',
  UNAVAILABLE: '不可用',
  ACQUIRE_UNKNOWN: '取号结果未知',
  CANCEL_PENDING: '取消待确认',
  FAILED: '失败',
  EXPIRED: '已过期',
};

function stickyHint(status?: string | null) {
  if (status === 'ACQUIRE_UNKNOWN') return '取号结果未知，可等待补偿或人工核对上游';
  if (status === 'CANCEL_PENDING') return '取消待确认，系统会重试确认取消';
  return null;
}

function settlementLabel(status?: string | null) {
  if (status === 'CONFIRMED') return '已确认成本';
  if (status === 'PENDING') return '成本待确认';
  return '无成本';
}

/** 服务 / 国家的中文展示：后端给了中文名就用，否则把代码标成「未知（代码）」。 */
function serviceCountryLabel(row: RedeemRow): string {
  const service = row.smsServiceName || (row.smsService ? unknownLabel(row.smsService) : '');
  const country =
    row.smsCountryName ||
    (row.smsCountry != null && row.smsCountry !== ''
      ? unknownLabel(String(row.smsCountry))
      : '');
  return [service, country].filter((value) => value && value !== '—').join(' · ') || '—';
}

/** 会话态：只用中文映射，未命中走「未知（原始值）」，绝不回退成英文状态码。 */
function sessionStatusLabel(status?: string | null): string {
  if (!status) return '—';
  return labelOf(SESSION_STATUS_LABEL, status);
}

/** 卡密列：明文可复制 > 脱敏值 > 短指纹。 */
function KeyCell({ row }: { row: RedeemRow }) {
  if (row.code) {
    return (
      <Typography.Text code copyable style={{ fontSize: 12 }}>
        {row.code}
      </Typography.Text>
    );
  }
  const text =
    row.codeMasked ||
    (row.codeFingerprint ? `${row.codeFingerprint.slice(0, 12)}…` : '') ||
    row.codeFingerprintShort ||
    '';
  return text ? <span className="font-mono text-xs">{text}</span> : <>—</>;
}

export default function SmsRedeemsPage() {
  const actionRef = useRef<ActionType>();
  const [status, setStatus] = useState<string>('');
  const [sessionStatus, setSessionStatus] = useState<string>('');
  const [cdk, setCdk] = useState<string>('');
  const [detail, setDetail] = useState<RedeemRow | null>(null);

  useEffect(() => {
    const fromUrl = new URLSearchParams(window.location.search).get('sessionStatus');
    if (fromUrl) {
      setSessionStatus(fromUrl);
      // Reload after state flush so the first filtered request uses the URL status.
      queueMicrotask(() => actionRef.current?.reload());
    }
  }, []);

  const columns: ProColumns<RedeemRow>[] = [
    {
      title: '卡密',
      dataIndex: 'codeMasked',
      search: false,
      width: 180,
      render: (_, row) => <KeyCell row={row} />,
    },
    {
      title: '卡密状态',
      dataIndex: 'status',
      search: false,
      width: 110,
      render: (_, row) => (
        <AdminStatusTag tone={toneFromStatus(row.status)}>
          {labelOf(SMS_KEY_STATUS_LABEL, row.status)}
        </AdminStatusTag>
      ),
    },
    {
      title: '会话态',
      dataIndex: 'lastSessionStatus',
      search: false,
      width: 140,
      render: (_, row) =>
        row.lastSessionStatus ? (
          <AdminStatusTag tone={toneFromStatus(row.lastSessionStatus)}>
            {sessionStatusLabel(row.lastSessionStatus)}
          </AdminStatusTag>
        ) : (
          '—'
        ),
    },
    {
      title: '验证码',
      dataIndex: 'lastSmsCode',
      search: false,
      width: 120,
      render: (_, row) =>
        row.lastSmsCode ? (
          <Typography.Text code copyable>
            {row.lastSmsCode}
          </Typography.Text>
        ) : (
          '—'
        ),
    },
    {
      title: '最近号码',
      dataIndex: 'lastPhoneMasked',
      search: false,
      width: 130,
      render: (_, row) => row.lastPhoneMasked || '—',
    },
    {
      title: '服务 / 国家',
      search: false,
      width: 180,
      ellipsis: true,
      render: (_, row) => serviceCountryLabel(row),
    },
    {
      title: '异常原因',
      search: false,
      width: 200,
      ellipsis: true,
      render: (_, row) =>
        stickyHint(row.lastSessionStatus) ||
        (row.lastErrorCode ? moduleErrorLabel(row.lastErrorCode) : '—'),
    },
    {
      title: '次数',
      dataIndex: 'attemptCount',
      search: false,
      width: 90,
      render: (_, row) =>
        row.maxAttempts != null ? `${row.attemptCount}/${row.maxAttempts}` : row.attemptCount,
    },
    {
      title: '成本',
      search: false,
      width: 160,
      render: (_, row) => {
        if (row.confirmedCostUsd != null && String(row.confirmedCostUsd) !== '') {
          return `$${Number(row.confirmedCostUsd).toFixed(4)} · ${settlementLabel(row.costSettlementStatus)}`;
        }
        return settlementLabel(row.costSettlementStatus);
      },
    },
    {
      title: '首次使用',
      dataIndex: 'firstUsedAt',
      search: false,
      width: 170,
      render: (_, row) => formatDateTime(row.firstUsedAt),
    },
    {
      title: '操作',
      valueType: 'option',
      search: false,
      width: 80,
      fixed: 'right',
      render: (_, row) => [
        <Button key="detail" type="link" size="small" onClick={() => setDetail(row)}>
          详情
        </Button>,
      ],
    },
  ];

  const detailGroups: DetailGroup[] = detail
    ? [
        {
          key: 'key',
          title: '卡密',
          items: [
            {
              key: 'code',
              label: '卡密',
              children: detail.code ? (
                <CopyableValue value={detail.code} />
              ) : detail.codeMasked ? (
                <span className="font-mono text-xs">{detail.codeMasked}</span>
              ) : (
                '—'
              ),
            },
            {
              key: 'fingerprint',
              label: '完整指纹',
              children: <CopyableValue value={detail.codeFingerprint || detail.codeFingerprintShort} />,
            },
            {
              key: 'inventoryItemId',
              label: '卡密库条目 ID',
              children: <CopyableValue value={detail.inventoryItemId} />,
            },
            {
              key: 'sku',
              label: '套餐',
              children: <CopyableValue value={detail.skuName} />,
            },
            {
              key: 'batch',
              label: '批次',
              children: <CopyableValue value={detail.batchId} />,
            },
          ],
        },
        {
          key: 'status',
          title: '状态',
          items: [
            {
              key: 'status',
              label: '卡密状态',
              children: (
                <AdminStatusTag tone={toneFromStatus(detail.status)}>
                  {labelOf(SMS_KEY_STATUS_LABEL, detail.status)}
                </AdminStatusTag>
              ),
            },
            {
              key: 'sessionStatus',
              label: '会话态',
              children: detail.lastSessionStatus ? (
                <AdminStatusTag tone={toneFromStatus(detail.lastSessionStatus)}>
                  {sessionStatusLabel(detail.lastSessionStatus)}
                </AdminStatusTag>
              ) : (
                '—'
              ),
            },
            {
              key: 'errorCode',
              label: '错误码',
              children: detail.lastErrorCode ? (
                <Space direction="vertical" size={0}>
                  <Typography.Text type="danger">
                    {moduleErrorLabel(detail.lastErrorCode)}
                  </Typography.Text>
                  <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                    技术详情：{detail.lastErrorCode}
                  </Typography.Text>
                </Space>
              ) : (
                '—'
              ),
            },
            {
              key: 'service',
              label: '服务',
              children: detail.smsServiceName || (detail.smsService ? unknownLabel(detail.smsService) : '—'),
            },
            {
              key: 'country',
              label: '国家',
              children:
                detail.smsCountryName ||
                (detail.smsCountry != null && detail.smsCountry !== ''
                  ? unknownLabel(String(detail.smsCountry))
                  : '—'),
            },
          ],
        },
        {
          key: 'codes',
          title: '验证码与号码',
          items: [
            {
              key: 'smsCodes',
              label: '验证码',
              children:
                (detail.smsCodes && detail.smsCodes.length > 0) || detail.lastSmsCode ? (
                  <Space direction="vertical" size={2}>
                    {(detail.smsCodes && detail.smsCodes.length > 0
                      ? detail.smsCodes
                      : [detail.lastSmsCode!]
                    ).map((code, i) => (
                      <Typography.Text key={`${code}-${i}`} code copyable>
                        {CODE_MARKERS[i] || `${i + 1}.`} {code}
                      </Typography.Text>
                    ))}
                  </Space>
                ) : (
                  '—'
                ),
            },
            {
              key: 'nextSmsCode',
              label: '下一封码',
              children: detail.nextSmsCode ? (
                <Typography.Text code copyable>
                  {detail.nextSmsCode}
                </Typography.Text>
              ) : (
                '—'
              ),
            },
            {
              key: 'smsText',
              label: '短信原文',
              children: detail.lastSmsText || '—',
            },
            {
              key: 'phoneHistory',
              label: '号码记录',
              children: detail.phoneHistory ? (
                <Typography.Text copyable style={{ wordBreak: 'break-all' }}>
                  {detail.phoneHistory}
                </Typography.Text>
              ) : (
                detail.lastPhoneMasked || '—'
              ),
            },
            {
              key: 'sessions',
              label: '会话明细',
              children:
                detail.sessions && detail.sessions.length > 0 ? (
                  <Space direction="vertical" size={8} style={{ width: '100%' }}>
                    {detail.sessions.map((s) => (
                      <div key={`attempt-${s.attempt}`} style={{ fontSize: 12, lineHeight: 1.6 }}>
                        <div>
                          第 {s.attempt} 次 · {sessionStatusLabel(s.status)}
                        </div>
                        <div>
                          号码：
                          {s.phone ? (
                            <Typography.Text copyable>{s.phone}</Typography.Text>
                          ) : (
                            s.phoneMasked || '—'
                          )}
                        </div>
                        <div>
                          验证码：
                          {s.smsCodes && s.smsCodes.length > 0
                            ? s.smsCodes.map((c, i) => (
                                <Typography.Text
                                  key={`${c}-${i}`}
                                  code
                                  copyable
                                  style={{ marginRight: 6 }}
                                >
                                  {CODE_MARKERS[i] || `${i + 1}.`} {c}
                                </Typography.Text>
                              ))
                            : s.smsCode || '—'}
                        </div>
                        {s.activationCost != null && String(s.activationCost) !== '' ? (
                          <div>成本：${Number(s.activationCost).toFixed(4)} USD</div>
                        ) : null}
                        {s.smsText ? <div>原文：{s.smsText}</div> : null}
                        {stickyHint(s.status) ? (
                          <div style={{ color: '#ad6800' }}>{stickyHint(s.status)}</div>
                        ) : null}
                      </div>
                    ))}
                  </Space>
                ) : detail.lastSessionStatus ? (
                  sessionStatusLabel(detail.lastSessionStatus)
                ) : (
                  '—'
                ),
            },
          ],
        },
        {
          key: 'usage',
          title: '使用与成本',
          items: [
            {
              key: 'attempts',
              label: '尝试次数',
              children:
                detail.maxAttempts != null
                  ? `${detail.attemptCount}/${detail.maxAttempts}`
                  : detail.attemptCount,
            },
            {
              key: 'settlement',
              label: '成本结算',
              children: (
                <>
                  {settlementLabel(detail.costSettlementStatus)}
                  {detail.confirmedCostUsd != null && String(detail.confirmedCostUsd) !== ''
                    ? ` · $${Number(detail.confirmedCostUsd).toFixed(4)} USD`
                    : ''}
                  {detail.usdCnyRate != null && String(detail.usdCnyRate) !== ''
                    ? ` · 汇率 ${Number(detail.usdCnyRate).toFixed(4)}`
                    : ''}
                </>
              ),
            },
            { key: 'firstUsedAt', label: '首次使用', children: formatDateTime(detail.firstUsedAt) },
            { key: 'usedAt', label: '核销时间', children: formatDateTime(detail.usedAt) },
            { key: 'issuedAt', label: '发卡', children: formatDateTime(detail.issuedAt) },
            { key: 'expiresAt', label: '过期', children: formatDateTime(detail.expiresAt) },
            { key: 'failedAt', label: '失败时间', children: formatDateTime(detail.failedAt) },
          ],
        },
      ]
    : [];

  return (
    <AdminPage
      title="卡密使用记录"
      description="接码兑换状态、验证码与脱敏号码；服务 / 国家等次要信息见详情。"
    >
      <Space style={{ marginBottom: 16 }} wrap>
        <Input
          allowClear
          placeholder="按完整卡密查询"
          style={{ width: 260 }}
          value={cdk}
          onChange={(e) => setCdk(e.target.value)}
          onPressEnter={() => actionRef.current?.reload()}
        />
        <Select
          allowClear
          placeholder="按卡密状态筛选"
          style={{ width: 180 }}
          value={status || undefined}
          onChange={(v) => {
            setStatus(v || '');
            actionRef.current?.reload();
          }}
          options={[
            { value: 'UNUSED', label: labelOf(SMS_KEY_STATUS_LABEL, 'UNUSED') },
            { value: 'LOCKED', label: labelOf(SMS_KEY_STATUS_LABEL, 'LOCKED') },
            { value: 'USED', label: labelOf(SMS_KEY_STATUS_LABEL, 'USED') },
            { value: 'FAILED', label: labelOf(SMS_KEY_STATUS_LABEL, 'FAILED') },
            { value: 'EXPIRED', label: labelOf(SMS_KEY_STATUS_LABEL, 'EXPIRED') },
            { value: 'DISABLED', label: labelOf(SMS_KEY_STATUS_LABEL, 'DISABLED') },
          ]}
        />
        <Select
          allowClear
          placeholder="按会话态筛选"
          style={{ width: 200 }}
          value={sessionStatus || undefined}
          onChange={(v) => {
            setSessionStatus(v || '');
            actionRef.current?.reload();
          }}
          options={[
            { value: 'ACQUIRE_UNKNOWN', label: SESSION_STATUS_LABEL.ACQUIRE_UNKNOWN },
            { value: 'CANCEL_PENDING', label: SESSION_STATUS_LABEL.CANCEL_PENDING },
            { value: 'WAITING_CODE', label: SESSION_STATUS_LABEL.WAITING_CODE },
            { value: 'SUCCESS', label: SESSION_STATUS_LABEL.SUCCESS },
            { value: 'FAILED', label: SESSION_STATUS_LABEL.FAILED },
          ]}
        />
        <Button onClick={() => actionRef.current?.reload()}>查询</Button>
      </Space>
      <ProTable<RedeemRow>
        rowKey="id"
        actionRef={actionRef}
        columns={columns}
        search={false}
        cardBordered
        scroll={{ x: 1180 }}
        rowClassName={(row) =>
          row.lastSessionStatus === 'ACQUIRE_UNKNOWN' || row.lastSessionStatus === 'CANCEL_PENDING'
            ? 'lc-sms-sticky'
            : ''
        }
        pagination={{ defaultPageSize: 20, showSizeChanger: true }}
        request={async (params) => {
          const page = params.current || 1;
          const pageSize = params.pageSize || 20;
          const qs = new URLSearchParams({ page: String(page), pageSize: String(pageSize) });
          if (status) qs.set('status', status);
          if (sessionStatus) qs.set('sessionStatus', sessionStatus);
          if (cdk.trim()) qs.set('cdk', cdk.trim());
          try {
            const res = await adminApi<RedeemRow[]>(`/api/v1/admin/sms/redeems?${qs}`);
            return {
              data: res.data || [],
              success: true,
              total: res.meta?.total ?? res.data?.length ?? 0,
            };
          } catch (err) {
            message.error(err instanceof Error ? err.message : '加载失败');
            return { data: [], success: false, total: 0 };
          }
        }}
      />

      <style>{`
        .lc-sms-sticky td {
          background: #fffbe6 !important;
        }
      `}</style>

      <AdminDetailDrawer
        title="卡密详情"
        subtitle={detail ? `卡密库条目：${detail.inventoryItemId || '—'}` : undefined}
        width={620}
        open={!!detail}
        onClose={() => setDetail(null)}
        groups={detailGroups}
      />
    </AdminPage>
  );
}
