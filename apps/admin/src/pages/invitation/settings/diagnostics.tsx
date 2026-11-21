import { Alert, Descriptions, Space, Table, Tag, Typography } from 'antd';
import { resolveLabel, unknownLabel } from '../../../lib/zh-label';
import { moduleErrorLabel } from '../../../lib/zh-maps';

export type SmsBalanceResult = {
  ok: true;
  all_ok: boolean;
  platforms: Record<
    'smsbower' | 'herosms' | 'smspool',
    {
      label: string;
      configured: boolean;
      status: 'success' | 'failed' | 'unconfigured';
      ok: boolean;
      balance: number | null;
      elapsed_ms: number;
      error: string;
    }
  >;
};

const BALANCE_PLATFORMS = ['smsbower', 'herosms', 'smspool'] as const;
const BALANCE_STATUS = {
  success: { label: '查询成功', color: 'green' },
  failed: { label: '查询失败', color: 'red' },
  unconfigured: { label: '未配置密钥', color: undefined },
};

/** 自检探针的查询操作（`sms_prices.selftest` 里写死的 action 名）。 */
const PROBE_ACTION_LABEL: Record<string, string> = {
  getBalance: '查询余额',
  getPrices: '查询价目',
  getPricesV2: '查询价目（V2）',
  getPricesV3: '查询价目（V3）',
  getTopCountriesByServiceRank: '查询热门国家排行',
};

/** 平台展示名（品牌名，保留原文）。 */
const PLATFORM_TITLE: Record<string, string> = {
  smsbower: 'SmsBower',
  herosms: 'HeroSMS',
  smspool: 'SMSPool',
};

export function SmsBalanceDetails({ result }: { result: SmsBalanceResult }) {
  const rows = BALANCE_PLATFORMS.map((platform) => ({ platform, ...result.platforms[platform] }));
  return (
    <Space direction="vertical" size="middle" style={{ width: '100%' }}>
      <Alert
        showIcon
        type={result.all_ok ? 'success' : 'warning'}
        message={result.all_ok ? '三个平台余额查询成功' : '余额查询已结束，部分平台失败或未配置'}
        description="使用已保存的配置同时查询三个平台。余额查询不能确认 OpenAI 发码与收码是否成功。"
      />
      <Table
        rowKey="platform"
        size="small"
        pagination={false}
        dataSource={rows}
        scroll={{ x: 600 }}
        columns={[
          { title: '平台', dataIndex: 'label' },
          {
            title: '查询结果',
            dataIndex: 'status',
            render: (status: keyof typeof BALANCE_STATUS) => (
              <Tag color={BALANCE_STATUS[status]?.color}>
                {BALANCE_STATUS[status]?.label ?? unknownLabel(status)}
              </Tag>
            ),
          },
          {
            title: '余额',
            dataIndex: 'balance',
            render: (balance: number | null) => balance ?? '—',
          },
          {
            title: '耗时（毫秒）',
            render: (_, row) => (row.configured ? row.elapsed_ms : '—'),
          },
          {
            title: '详情',
            dataIndex: 'error',
            render: (error: string) => (
              <Typography.Text style={{ overflowWrap: 'anywhere' }}>
                {error ? moduleErrorLabel(error) : '—'}
              </Typography.Text>
            ),
          },
        ]}
      />
    </Space>
  );
}

export type SmsSelftestResult = {
  platforms?: Record<
    string,
    {
      configured?: boolean;
      physical_count_available?: boolean;
      error?: string;
      probes?: {
        action?: string;
        status?: number;
        elapsed_ms?: number;
        fields?: string[];
        text?: string;
        error?: string;
      }[];
    }
  >;
};

export function SmsSelftestDetails({ result }: { result: SmsSelftestResult }) {
  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <Alert showIcon type="info" message="接口自检仅查询平台响应，不购买号码。" />
      {Object.entries(result.platforms || {}).map(([platform, info]) => (
        <div key={platform}>
          <Typography.Title level={5}>
            {/* zh-label-allow-raw: 平台键（smsbower 等）是技术标识，不是英文文案；缺映射时显示键本身便于排查。 */}
            {PLATFORM_TITLE[platform] || platform}{' '}
            <Tag color={info.configured ? 'green' : undefined}>
              {info.configured ? '已配置' : '未配置'}
            </Tag>
            {info.physical_count_available != null && (
              <Tag color={info.physical_count_available ? 'green' : 'orange'}>
                实体卡字段{info.physical_count_available ? '可用' : '不可用'}
              </Tag>
            )}
          </Typography.Title>
          {info.error && (
            <Alert type="error" message={moduleErrorLabel(info.error)} style={{ marginBottom: 12 }} />
          )}
          <Table
            size="small"
            pagination={false}
            rowKey={(_, index) => String(index)}
            dataSource={info.probes || []}
            scroll={{ x: 650 }}
            columns={[
              { title: '查询操作', dataIndex: 'action', render: (action: string) => resolveLabel(PROBE_ACTION_LABEL, action) },
              { title: 'HTTP 状态', dataIndex: 'status' },
              { title: '耗时（毫秒）', dataIndex: 'elapsed_ms' },
              {
                title: '返回字段 / 错误',
                render: (_, probe) =>
                  (probe.fields || []).join(', ') || moduleErrorLabel(probe.error) || probe.text || '—',
              },
            ]}
          />
        </div>
      ))}
    </Space>
  );
}

export type RuntimeResult = {
  node?: string;
  codex?: string;
  codex_binary_setting?: string;
  curl_cffi?: string;
  codex_home?: string;
  codex_home_writable?: boolean;
  sms_platforms?: Record<string, boolean>;
};

export function RuntimeDetails({ result }: { result: RuntimeResult }) {
  return (
    <Descriptions
      column={1}
      bordered
      size="small"
      items={[
        { key: 'node', label: 'Node', children: result.node || '未找到' },
        {
          key: 'codex',
          label: 'Codex',
          children: result.codex || `未找到 ${result.codex_binary_setting || ''}`,
        },
        { key: 'curl', label: 'curl_cffi', children: result.curl_cffi || '未找到' },
        {
          key: 'home',
          label: 'Codex 工作目录',
          children: (
            <>
              {result.codex_home || '—'}{' '}
              <Tag color={result.codex_home_writable ? 'green' : 'red'}>
                {result.codex_home_writable ? '可写' : '不可写'}
              </Tag>
            </>
          ),
        },
        {
          key: 'sms',
          label: '接码平台密钥',
          children: (
            <Space wrap>
              {Object.entries(result.sms_platforms || {}).map(([key, configured]) => (
                <Tag key={key} color={configured ? 'green' : undefined}>
                  {key}：{configured ? '已配置' : '未配置'}
                </Tag>
              ))}
            </Space>
          ),
        },
      ]}
    />
  );
}
