import {
  Alert,
  Button,
  Card,
  Col,
  Collapse,
  Descriptions,
  Empty,
  Modal,
  Row,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import { useEffect, useState } from 'react';
import { AdminPage } from '../../../components/admin-page';
import { AdminDetailDrawer, type DetailGroup } from '../../../components/admin-detail-drawer';
import { adminApi } from '../../../lib/admin-api';
import {
  invitationAction,
  invitationErrorMessage,
  invitationTime,
} from '../../../lib/invitation-api';
import { useInvitationQuery } from '../../../lib/use-invitation-query';
import { Link } from '../../../lib/navigation';
import { resolveLabel, unknownLabel } from '../../../lib/zh-label';
import { localizedText } from '../../../lib/zh-text';
import { SmsSelftestDetails, type SmsSelftestResult } from '../settings/diagnostics';

type PriceRow = {
  platform: string;
  platform_label?: string;
  country: string;
  country_name?: string;
  price?: number;
  min_price?: number;
  count?: number;
  stock?: number;
  physical_count?: number;
  success_rate?: number;
  ok?: number;
  attempts?: number;
  score?: number;
  rejected_reason?: string;
  openai_safe?: boolean;
  updated_at?: number;
  reason?: string;
};
type PriceResponse = {
  best?: PriceRow | null;
  effective?: {
    source?: string;
    service?: string;
    platform_candidates?: string[];
    country_candidates?: string[];
    country_candidates_by_platform?: Record<string, string[]>;
    max_price_by_country?: Record<string, number>;
    allowed_countries?: string[];
    min_price?: number;
    max_price?: number;
    phone_exception?: string;
    provider_ids?: string;
    except_provider_ids?: string;
    openai_safe?: boolean;
    openai_safe_blocked?: PriceRow[];
  };
  platforms?: Record<
    string,
    {
      label?: string;
      configured?: boolean;
      last_poll_at?: number;
      last_ok_at?: number;
      balance?: number;
      last_error?: string;
    }
  >;
  evaluate?: {
    weight?: number;
    min_stock?: number;
    min_success_rate?: number;
    platform_mode?: string;
    physical_only?: boolean;
    physical_count_available?: boolean;
    physical_only_ignored?: boolean;
    platforms_without_physical?: string[];
    candidates?: PriceRow[];
    rejected?: PriceRow[];
  };
  rows?: PriceRow[];
  poll_seconds?: number;
  rounds?: number;
  thread_alive?: boolean;
  started_at?: number;
  generated_at?: number;
  last_error?: string;
};
const PLATFORM_LABEL: Record<string, string> = {
  auto: '自动择优',
  smsbower: 'SmsBower',
  herosms: 'HeroSMS',
  smspool: 'SMSPool',
};
const formatRate = (row: PriceRow) =>
  `${((row.success_rate || 0) * 100).toFixed(0)}%（${row.ok || 0} / ${row.attempts || 0}）`;
const platformScope = (mode?: string) =>
  mode
    ? mode
        .split(',')
        .map((platform) => resolveLabel(PLATFORM_LABEL, platform.trim()))
        .join('、')
    : '—';
/** 平台显示名：后端给了就用后端的，否则走共用中文映射，未知平台不漏裸英文。 */
const platformLabel = (row: PriceRow) =>
  row.platform_label || resolveLabel(PLATFORM_LABEL, row.platform);

export default function InvitationSmsPricesPage() {
  const prices = useInvitationQuery<PriceResponse>('/sms/prices');
  const [busy, setBusy] = useState('');
  const [selftest, setSelftest] = useState<SmsSelftestResult | null>(null);
  const [rowDetail, setRowDetail] = useState<PriceRow | null>(null);
  const [serviceNames, setServiceNames] = useState<Record<string, string>>({});
  const data = prices.data;
  const best = data?.best;
  const effective = data?.effective;
  const evaluation = data?.evaluate;
  useEffect(() => {
    // 取号服务代码（如 dr）本身是英文标识，但能查到中文服务名就显示中文名。
    void adminApi<{ code: string; name: string }[]>('/api/v1/admin/sms/catalog/services')
      .then((response) =>
        setServiceNames(
          Object.fromEntries((response.data || []).map((item) => [item.code, item.name])),
        ),
      )
      .catch(() => undefined);
  }, []);
  const serviceText = (code?: string) => {
    const value = String(code || '').trim();
    if (!value) return '—';
    const name = serviceNames[value];
    return name ? `${name}（${value}）` : unknownLabel(value);
  };
  const baseColumns = [
    {
      title: '平台',
      dataIndex: 'platform_label',
      width: 110,
      render: (_: string, row: PriceRow) => platformLabel(row),
    },
    {
      title: '国家',
      width: 210,
      render: (_: unknown, row: PriceRow) => (
        <>
          {row.country_name || '国家'}（{row.country}）
          {row.openai_safe && <Tag color="blue">项目经验名单</Tag>}
        </>
      ),
    },
    {
      title: '单价',
      dataIndex: 'price',
      width: 95,
      sorter: (a: PriceRow, b: PriceRow) => (a.price || 0) - (b.price || 0),
    },
    {
      title: '库存',
      width: 85,
      render: (_: unknown, row: PriceRow) => row.stock ?? row.count ?? '—',
      sorter: (a: PriceRow, b: PriceRow) => (a.stock ?? a.count ?? 0) - (b.stock ?? b.count ?? 0),
    },
    { title: '成功率', width: 155, render: (_: unknown, row: PriceRow) => formatRate(row) },
  ];
  /** 主表只留关键列；实体卡、得分、更新时间与筛选原因都进详情。 */
  const mainColumns = [
    baseColumns[0],
    baseColumns[1],
    baseColumns[2],
    baseColumns[3],
    baseColumns[4],
    {
      title: '状态',
      width: 130,
      render: (_: unknown, row: PriceRow) =>
        row.rejected_reason ? (
          <Tag>{localizedText(row.rejected_reason)}</Tag>
        ) : best?.platform === row.platform && best.country === row.country ? (
          <Tag color="green">当前最优</Tag>
        ) : (
          <Tag color="orange">候选</Tag>
        ),
    },
    {
      title: '操作',
      width: 90,
      render: (_: unknown, row: PriceRow) => (
        <Button type="link" size="small" onClick={() => setRowDetail(row)}>
          详情
        </Button>
      ),
    },
  ];

  async function refresh() {
    setBusy('refresh');
    try {
      const result = await invitationAction<
        PriceResponse & { refreshed?: Record<string, { ok?: boolean; error?: string }> }
      >('/sms/prices/refresh');
      const failed = Object.entries(result.refreshed || {}).filter(([, value]) => !value.ok);
      if (failed.length)
        message.warning(
          `已刷新；${failed
            .map(
              ([platform, value]) =>
                `${resolveLabel(PLATFORM_LABEL, platform)}：${localizedText(value.error, '失败')}`,
            )
            .join('；')}`,
        );
      else message.success('各平台价格已刷新');
      await prices.reload();
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  async function test() {
    setBusy('test');
    try {
      setSelftest(await invitationAction<SmsSelftestResult>('/sms/selftest'));
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  const detailGroups: DetailGroup[] = rowDetail
    ? [
        {
          key: 'price',
          title: '价格与库存',
          items: [
            { key: 'platform', label: '平台', children: platformLabel(rowDetail) },
            {
              key: 'country',
              label: '国家',
              children: `${rowDetail.country_name || '国家'}（${rowDetail.country}）`,
            },
            {
              key: 'price',
              label: '单价',
              children: `${rowDetail.price ?? '—'}${rowDetail.min_price ? `（起价 ${rowDetail.min_price}）` : ''}`,
            },
            {
              key: 'stock',
              label: '库存',
              children: `${rowDetail.stock ?? rowDetail.count ?? '—'}${rowDetail.physical_count != null ? `（实体卡 ${rowDetail.physical_count}）` : ''}`,
            },
            {
              key: 'physical',
              label: '实体卡',
              children: rowDetail.physical_count ?? '—',
            },
            { key: 'rate', label: '成功率', children: formatRate(rowDetail) },
            {
              key: 'score',
              label: '得分',
              children: rowDetail.score != null ? rowDetail.score.toFixed(4) : '—',
            },
            {
              key: 'updated',
              label: '更新时间',
              children: invitationTime(rowDetail.updated_at),
            },
            {
              key: 'safe',
              label: '项目经验名单',
              children: rowDetail.openai_safe ? '在名单内' : '不在名单内',
            },
            {
              key: 'reason',
              label: '筛选说明',
              children: localizedText(rowDetail.reason || rowDetail.rejected_reason),
            },
          ],
        },
      ]
    : [];
  return (
    <AdminPage
      title="接码价格"
      description="查看 Codex 注册接码的价格、库存、余额和实际取号参数。"
      extra={
        <Space wrap>
          <Link href="/invitation/settings">服务配置</Link>
          <Button disabled={!!busy} loading={busy === 'test'} onClick={() => void test()}>
            接口自检
          </Button>
          <Button
            disabled={!!busy && busy !== 'refresh'}
            loading={busy === 'refresh'}
            onClick={() => void refresh()}
          >
            立即刷新平台价格
          </Button>
        </Space>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {prices.error && (
          <Alert
            type="error"
            showIcon
            message={prices.error}
            action={<Button onClick={() => void prices.reload()}>重试</Button>}
          />
        )}
        {data?.last_error && (
          <Alert type="error" showIcon message={localizedText(data.last_error)} />
        )}
        {effective?.source === 'monitor' && (
          <Alert
            type="info"
            showIcon
            message={
              effective.openai_safe
                ? '当前最优国家位于项目经验名单，不能保证发码或收码成功。'
                : '当前最优国家不在项目经验名单；名单仅供参考，不能保证发码或收码成功。'
            }
            description={
              !effective.openai_safe &&
              effective.openai_safe_blocked
                ?.map(
                  (row) =>
                    `${row.country_name || row.country}：${localizedText(row.reason || row.rejected_reason, '已被筛选')}`,
                )
                .join('；')
            }
          />
        )}
        {evaluation?.physical_only_ignored && (
          <Alert
            type="warning"
            showIcon
            message="已启用仅实体卡，但平台接口没有实体卡库存字段，当前无法按实体卡过滤。"
          />
        )}
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={12}>
            <Card title="当前最优候选" loading={prices.loading && !data}>
              {best ? (
                <>
                  <Descriptions
                    column={2}
                    size="small"
                    bordered
                    items={[
                      { key: 'platform', label: '平台', children: platformLabel(best) },
                      {
                        key: 'country',
                        label: '国家',
                        children: `${best.country_name || '国家'}（${best.country}）`,
                      },
                      {
                        key: 'price',
                        label: '单价',
                        children: `${best.price ?? '—'}${best.min_price ? `（起价 ${best.min_price}）` : ''}`,
                      },
                      {
                        key: 'stock',
                        label: '库存',
                        children: `${best.stock ?? best.count ?? '—'}${best.physical_count != null ? `（实体卡 ${best.physical_count}）` : ''}`,
                      },
                      { key: 'rate', label: '成功率', children: formatRate(best) },
                      { key: 'score', label: '得分', children: best.score?.toFixed(4) ?? '—' },
                    ]}
                  />
                  <Typography.Paragraph type="secondary" style={{ marginTop: 16, marginBottom: 0 }}>
                    按所选平台自动择优，无需写入配置
                  </Typography.Paragraph>
                </>
              ) : (
                <Empty description="暂无可用最优结果，请检查平台密钥及价格、国家、库存和成功率限制。" />
              )}
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card title="平台状态与余额">
              <Space wrap style={{ marginBottom: 12 }}>
                <Tag color={data?.thread_alive ? 'green' : undefined}>
                  {data?.thread_alive ? '价格任务运行中' : '价格任务已停止'}
                </Tag>
                <Tag>已运行 {data?.rounds || 0} 轮</Tag>
                <Tag>任务间隔 {data?.poll_seconds || 5} 秒</Tag>
              </Space>
              <Table
                size="small"
                rowKey="platform"
                dataSource={Object.entries(data?.platforms || {}).map(([platform, value]) => ({
                  platform,
                  ...value,
                }))}
                pagination={false}
                scroll={{ x: 660 }}
                columns={[
                  {
                    title: '平台',
                    dataIndex: 'label',
                    render: (label: string, row: { platform: string; label?: string }) =>
                      label || resolveLabel(PLATFORM_LABEL, row.platform),
                  },
                  {
                    title: '密钥',
                    dataIndex: 'configured',
                    render: (configured: boolean) => (
                      <Tag color={configured ? 'green' : undefined}>
                        {configured ? '已配置' : '未配置'}
                      </Tag>
                    ),
                  },
                  {
                    title: '余额',
                    dataIndex: 'balance',
                    render: (balance?: number) => balance ?? '—',
                  },
                  { title: '最近轮询', dataIndex: 'last_poll_at', render: invitationTime },
                  { title: '最近成功', dataIndex: 'last_ok_at', render: invitationTime },
                  {
                    title: '错误',
                    dataIndex: 'last_error',
                    render: (error?: string) =>
                      error ? (
                        <Typography.Text type="danger">{localizedText(error)}</Typography.Text>
                      ) : (
                        '无'
                      ),
                  },
                ]}
              />
            </Card>
          </Col>
        </Row>
        <Card title="实际生效的取号参数">
          <Descriptions
            size="small"
            bordered
            column={2}
            items={[
              {
                key: 'source',
                label: '来源',
                children: effective?.source === 'monitor' ? '价格任务最优' : '手动配置',
              },
              { key: 'service', label: '接码服务', children: serviceText(effective?.service) },
              {
                key: 'platform',
                label: '平台顺序',
                children:
                  effective?.platform_candidates
                    ?.map((item) => resolveLabel(PLATFORM_LABEL, item))
                    .join(' → ') || '—',
              },
              {
                key: 'country',
                label: '国家顺序',
                children: effective?.country_candidates?.join(' → ') || '—',
              },
              {
                key: 'price',
                label: '价格区间',
                children: `${effective?.min_price || 0} ～ ${effective?.max_price || '不限'}`,
              },
              {
                key: 'prefix',
                label: '排除号码前缀',
                children: effective?.phone_exception || '无',
              },
              { key: 'provider', label: '包含供货商', children: effective?.provider_ids || '不限' },
              {
                key: 'except',
                label: '排除供货商',
                children: effective?.except_provider_ids || '无',
              },
              {
                key: 'perplatform',
                label: '各平台国家候选',
                children:
                  Object.entries(effective?.country_candidates_by_platform || {})
                    .map(
                      ([platform, countries]) =>
                        `${resolveLabel(PLATFORM_LABEL, platform)}：${countries.join(' → ')}`,
                    )
                    .join('；') || '—',
                span: 2,
              },
            ]}
          />
          <Space wrap style={{ marginTop: 16 }}>
            <Tag>权重 {evaluation?.weight ?? '—'}</Tag>
            <Tag>库存下限 {evaluation?.min_stock ?? '—'}</Tag>
            <Tag>成功率下限 {evaluation?.min_success_rate ?? '—'}</Tag>
            <Tag>平台范围 {platformScope(evaluation?.platform_mode)}</Tag>
            {evaluation?.physical_only && <Tag>仅实体卡</Tag>}
          </Space>
        </Card>
        <Card
          title={`各平台价格明细（${data?.rows?.length || 0} 行）`}
          extra={
            <Typography.Text type="secondary">
              快照 {invitationTime(data?.generated_at)}
            </Typography.Text>
          }
        >
          <Table<PriceRow>
            rowKey={(row) => `${row.platform}:${row.country}`}
            size="small"
            scroll={{ x: 1000 }}
            pagination={{ pageSize: 20, showSizeChanger: true }}
            dataSource={data?.rows || []}
            columns={mainColumns}
          />
        </Card>
        <Collapse
          items={[
            {
              key: 'evaluation',
              label: `可选候选与被排除明细（候选 ${evaluation?.candidates?.length || 0} 条 · 被排除 ${evaluation?.rejected?.length || 0} 条）`,
              children: (
                <Space direction="vertical" size={16} style={{ width: '100%' }}>
                  <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
                    候选按后端得分排序，得分越低越优。部分接口缺少实体卡字段时会保留上次价格快照；
                    主表只列关键列，实体卡、得分与筛选原因请点行内「详情」。
                  </Typography.Paragraph>
                  <Table<PriceRow>
                    rowKey={(row) => `${row.platform}:${row.country}`}
                    size="small"
                    scroll={{ x: 1100 }}
                    pagination={{ pageSize: 10 }}
                    dataSource={evaluation?.candidates || []}
                    columns={baseColumns}
                  />
                  <Table<PriceRow>
                    rowKey={(row) => `${row.platform}:${row.country}`}
                    size="small"
                    scroll={{ x: 650 }}
                    pagination={{ pageSize: 10 }}
                    dataSource={evaluation?.rejected || []}
                    columns={[
                      baseColumns[0],
                      baseColumns[1],
                      baseColumns[2],
                      {
                        title: '排除原因',
                        dataIndex: 'reason',
                        render: (reason: string, row: PriceRow) =>
                          localizedText(reason || row.rejected_reason),
                      },
                    ]}
                  />
                </Space>
              ),
            },
          ]}
        />
      </Space>
      <AdminDetailDrawer
        title={`价格详情 · ${rowDetail ? platformLabel(rowDetail) : ''}`}
        subtitle={rowDetail ? `${rowDetail.country_name || '国家'}（${rowDetail.country}）` : undefined}
        open={!!rowDetail}
        onClose={() => setRowDetail(null)}
        groups={detailGroups}
      />
      <Modal
        title="注册接码接口自检"
        open={!!selftest}
        onCancel={() => setSelftest(null)}
        footer={null}
        width={1000}
        destroyOnHidden
      >
        {selftest && <SmsSelftestDetails result={selftest} />}
      </Modal>
    </AdminPage>
  );
}
