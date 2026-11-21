import { Alert, Button, Card, Col, Descriptions, Empty, Row, Space, Statistic, Table, Tag, Typography } from 'antd';
import { AdminPage } from '../../components/admin-page';
import { InvitationRunPanel } from '../../components/invitation-run-panel';
import { InvitationStatus } from '../../components/invitation-status';
import { adminApi } from '../../lib/admin-api';
import { invitationErrorMessage, invitationTime } from '../../lib/invitation-api';
import { cdkStatusLabel, runKindLabel, runKindTone } from '../../lib/invitation';
import { resolveLabel } from '../../lib/zh-label';
import { Link, useRouter } from '../../lib/navigation';
import { useInvitationRuntime } from '../../lib/invitation-runtime';
import { useInvitationQuery } from '../../lib/use-invitation-query';
import { useCallback, useEffect, useState } from 'react';

type Overview = {
  stats?: Record<string, number>;
  cdk?: Record<string, number>;
  proxies?: Record<string, number>;
  sms?: { best?: any; poll_seconds?: number; rounds?: number; last_error?: string };
  runs?: any[];
  mail_source?: string;
  pool_available?: number;
  /** 注册 / 接受邀请 / 完成 CLI 三段各自的账号数。 */
  authorization?: {
    tracked?: number;
    inviteAccepted?: number;
    cliCompleted?: number;
    awaitingAuthorization?: number;
  };
};
const MAIL_STATS = [
  ['total', '邮箱总计', '#287c80'],
  ['available', '邮箱', '#389e0d'],
  ['in_use', '使用中', '#d48806'],
  ['registered', 'GPT', '#13c2c2'],
  // `done` 既有"单次注册成功"也有"Codex 授权完成"：写成"已授权"就是把注册成功
  // 读成了邀请完成。真正的授权进度看下面授权阶段的三个数字。
  ['done', '单次注册完成', '#1677ff'],
  ['failed', '失败', '#cf1322'],
];
/** 卡密统计卡片：文案与卡密列表同源（终态「已核销」，不再出现「已完成」）。 */
const CDK_STATS: [string, string][] = [
  ['unused', cdkStatusLabel('unused')],
  ['bound', cdkStatusLabel('bound')],
  ['queued', cdkStatusLabel('queued')],
  ['running', cdkStatusLabel('running')],
  ['done', cdkStatusLabel('done')],
  ['failed', cdkStatusLabel('failed')],
  ['mail_pool', '可兑换邮箱'],
];
const SHORTCUTS = [
  ['/invitation', '邀请概览'],
  ['/invitation/mail-accounts', '邮箱管理'],
  ['/invitation/register', '单次注册'],
  ['/invitation/registered', '注册结果'],
  ['/invitation/runs', '运行记录'],
  ['/invitation/cdk', '卡密管理'],
  ['/invitation/proxies', '代理池'],
  ['/invitation/sms-prices', '接码价格'],
  ['/invitation/settings', '服务配置'],
];
/** 接码平台键 → 展示名；未知平台走 unknownLabel，绝不漏出裸英文键。 */
const SMS_PLATFORM_LABEL: Record<string, string> = {
  auto: '自动择优',
  smsbower: 'SmsBower',
  herosms: 'HeroSMS',
  smspool: 'SMSPool',
};
const POOL_PATH = '/api/v1/admin/invitation/pool';
type PoolSnapshot = {
  enabled: boolean;
  running: boolean;
  target: number;
  available: number;
  mailboxAvailable: number;
  concurrency: number;
  checkSeconds: number;
  rounds: number;
  lastError: string;
  /** 为什么没在补号；空串表示条件齐备。 */
  blockedReason?: string;
  /** 补号来源，以及它是不是池化来源（非池化来源是注册时现造地址）。 */
  mailSource?: string;
  pooled?: boolean;
  /** 注册阶段顺手验过手机 / 没验过的账号数。 */
  smsBound?: number;
  smsUnbound?: number;
};

/** 只读展示账号工厂状态：与邀请执行的并发数字分开，避免误读成同一套配额。 */
function InvitationPoolSnapshotCard() {
  const [snapshot, setSnapshot] = useState<PoolSnapshot>();
  const [error, setError] = useState('');
  const [loadedAt, setLoadedAt] = useState<number>();
  // 这个卡片只有「读取账号池」一个动作：挂载、5 秒轮询和点刷新都走 load()。
  const [loading, setLoading] = useState<string | null>(null);
  const load = useCallback(async () => {
    setLoading('load');
    try {
      const { data } = await adminApi<PoolSnapshot>(POOL_PATH);
      if (
        !data ||
        typeof data.enabled !== 'boolean' ||
        typeof data.lastError !== 'string' ||
        [
          data.target,
          data.available,
          data.mailboxAvailable,
          data.concurrency,
          data.checkSeconds,
          data.rounds,
        ].some((value) => typeof value !== 'number')
      )
        throw new Error('账号池状态暂时无法读取，请刷新后再试。');
      setSnapshot(data);
      setError('');
      setLoadedAt(Date.now());
    } catch (failure) {
      setError(invitationErrorMessage(failure));
    } finally {
      setLoading(null);
    }
  }, []);
  useEffect(() => {
    void load();
    // 与其它概览数字一样每 5 秒刷新，后台标签页不轮询。
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 5000);
    return () => clearInterval(timer);
  }, [load]);
  return (
    <Card
      title="账号池"
      extra={<Link href="/invitation/settings">服务配置</Link>}
      style={{ height: '100%' }}
    >
      <Space wrap>
        <Tag color={snapshot?.enabled ? (snapshot.running ? 'green' : 'orange') : 'default'}>
          开关 {snapshot ? (snapshot.enabled ? '已开启' : '已关闭') : '—'}
        </Tag>
        {/* 光有开关说明不了"为什么没补号"：这里把实际运行状态单独标出来。 */}
        <Tag color={snapshot?.running ? 'green' : 'default'}>
          {snapshot ? (snapshot.running ? '巡检线程运行中' : '巡检线程未运行') : '—'}
        </Tag>
        <Tag color="cyan">待兑换 {snapshot?.available ?? '—'}</Tag>
        <Tag>补号目标 {snapshot?.target ?? '—'}</Tag>
        <Tag>补号并发 {snapshot?.concurrency ?? '—'}</Tag>
        <Tag>巡检间隔 {snapshot ? `${snapshot.checkSeconds} 秒` : '—'}</Tag>
        <Tag>可注册邮箱 {snapshot?.mailboxAvailable ?? '—'}</Tag>
        <Tag>
          补号来源 {snapshot?.mailSource ?? '—'}
          {snapshot ? (snapshot.pooled ? '（池化）' : '（现造地址）') : ''}
        </Tag>
        <Tag color="green">已接码 {snapshot?.smsBound ?? '—'}</Tag>
        <Tag color="orange">未接码 {snapshot?.smsUnbound ?? '—'}</Tag>
        <Tag>补号轮次 {snapshot?.rounds ?? '—'}</Tag>
        <Button
          size="small"
          loading={loading === 'load' && !snapshot}
          onClick={() => void load()}
        >
          刷新
        </Button>
      </Space>
      {snapshot?.enabled && snapshot?.blockedReason ? (
        <Alert
          style={{ marginTop: 12 }}
          type="info"
          showIcon
          message={`当前未补号：${snapshot.blockedReason}`}
        />
      ) : null}
      {(error || snapshot?.lastError) && (
        <Alert
          style={{ marginTop: 12 }}
          type="warning"
          showIcon
          message={error || `账号池最近错误：${snapshot?.lastError}`}
          description={
            loadedAt
              ? `以下是 ${invitationTime(loadedAt / 1000)} 读取到的状态，可能已经过期。`
              : undefined
          }
        />
      )}
      <Typography.Paragraph type="secondary" style={{ margin: '12px 0 0' }}>
        账号池只负责预先注册：注册后停在绑定 2FA、不做 Codex
        授权，账号进入待兑换账号池。它的补号并发与巡检独立于邀请执行的并发上限，两者互不影响。
      </Typography.Paragraph>
    </Card>
  );
}
export default function InvitationPage() {
  const overview = useInvitationQuery<Overview>('/overview');
  const { watchRun } = useInvitationRuntime();
  const router = useRouter();
  const data = overview.data;
  const best = data?.sms?.best;
  // 页面级「刷新」只在自己触发的那次读取期间转圈：卡片骨架屏仍看 overview.loading，
  // 5 秒轮询与账号池卡片的刷新都不会点亮它，它也不会点亮别处。
  const [refreshing, setRefreshing] = useState<string | null>(null);
  const reloadOverview = useCallback(async () => {
    setRefreshing('reload-overview');
    try {
      await overview.reload();
    } finally {
      setRefreshing(null);
    }
  }, [overview.reload]);
  return (
    <AdminPage
      title="邀请概览"
      description="查看邮箱、卡密、代理和最近运行；数据每 5 秒刷新。"
      extra={
        <Button loading={refreshing === 'reload-overview'} onClick={() => void reloadOverview()}>
          刷新
        </Button>
      }
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {overview.error && <Alert type="error" showIcon message={overview.error} />}
        <Row gutter={[16, 16]}>
          {[
            ['已注册账号', data?.authorization?.tracked],
            ['已接受邀请', data?.authorization?.inviteAccepted],
            ['已完成 CLI', data?.authorization?.cliCompleted],
            ['待跑 CLI', data?.authorization?.awaitingAuthorization],
          ].map(([label, value]) => (
            <Col xs={12} sm={8} lg={4} key={String(label)}>
              <Card loading={overview.loading && !data} size="small">
                <Statistic title={label as string} value={Number(value ?? 0)} />
              </Card>
            </Col>
          ))}
          {MAIL_STATS.map(([key, label, color]) => (
            <Col xs={12} sm={8} lg={4} key={key}>
              <Card loading={overview.loading && !data}>
                <Statistic title={label} value={data?.stats?.[key] ?? 0} valueStyle={{ color }} />
              </Card>
            </Col>
          ))}
        </Row>
        <Row gutter={[16, 16]}>
          <Col xs={24} xl={14}>
            <Card
              title="卡密 / 邀请概览"
              extra={<Link href="/invitation/cdk">卡密管理</Link>}
              style={{ height: '100%' }}
            >
              <Row gutter={[16, 20]}>
                {CDK_STATS.map(([key, label]) => (
                  <Col xs={12} sm={6} key={key}>
                    <Statistic
                      title={label}
                      value={
                        key === 'mail_pool' && data?.cdk?.[key] === -1
                          ? '不限量'
                          : (data?.cdk?.[key] ?? 0)
                      }
                    />
                  </Col>
                ))}
              </Row>
              <Typography.Paragraph type="secondary" style={{ margin: '20px 0 0' }}>
                可兑换账号是已完成注册并绑定 2FA、且尚未被卡密占用的账号，不是刚导入的原始邮箱；账号由账号工厂预先补号，用户兑换 CDK 后立即绑定。
              </Typography.Paragraph>
            </Card>
          </Col>
          <Col xs={24} xl={10}>
            <Card
              title="代理池健康度"
              extra={<Link href="/invitation/proxies">代理池</Link>}
              style={{ height: '100%' }}
            >
              <Space wrap>
                {[
                  ['ready', '可用', 'green'],
                  ['frozen', '冻结中', 'orange'],
                  ['dead', '永久不可用', 'red'],
                  ['disabled', '已禁用', 'default'],
                ].map(([key, label, color]) => (
                  <Tag key={key} color={color}>
                    {label} {data?.proxies?.[key] ?? 0}
                  </Tag>
                ))}
              </Space>
              <Typography.Paragraph type="secondary" style={{ margin: '16px 0 0' }}>
                连续 409
                的冻结阈值、冻结时长及重试次数可在服务配置中调整；永久不可用的代理需要手动重置。
              </Typography.Paragraph>
            </Card>
          </Col>
          <Col xs={24} xl={12}>
            <Card title="最优接码" extra={<Link href="/invitation/sms-prices">接码价格</Link>}>
              {data?.sms?.last_error && (
                <Alert type="warning" message={data.sms.last_error} style={{ marginBottom: 12 }} />
              )}
              {best ? (
                <Descriptions
                  size="small"
                  column={2}
                  items={[
                    {
                      key: 'platform',
                      label: '平台',
                      children:
                        best.platform_label ||
                        resolveLabel(SMS_PLATFORM_LABEL, best.platform),
                    },
                    {
                      key: 'country',
                      label: '国家',
                      children: `${best.country_name || '—'} (${best.country || '—'})`,
                    },
                    { key: 'price', label: '单价', children: best.price },
                    { key: 'stock', label: '库存', children: best.stock },
                    {
                      key: 'success',
                      label: '成功率',
                      children: `${Math.round(Number(best.success_rate || 0) * 100)}%`,
                    },
                    { key: 'score', label: '得分', children: Number(best.score || 0).toFixed(3) },
                  ]}
                />
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="暂无可用最优结果，请检查密钥及国家、价格、库存过滤。"
                />
              )}
              <Typography.Text type="secondary">
                价格任务每 {data?.sms?.poll_seconds || 5} 秒刷新，已运行 {data?.sms?.rounds ?? 0}{' '}
                轮。
              </Typography.Text>
            </Card>
          </Col>
          <Col xs={24} xl={12}>
            <Card title="快捷入口">
              <Space wrap>
                {SHORTCUTS.map(([path, label]) => (
                  <Button key={path} onClick={() => router.push(path)}>
                    {label}
                  </Button>
                ))}
              </Space>
              <Typography.Paragraph type="secondary" style={{ margin: '16px 0 0' }}>
                当前邮箱来源：{data?.mail_source || '—'} · 可用邮箱：
                {data?.pool_available === -1 ? '不限量' : (data?.pool_available ?? '—')}
                。导入邮箱请进入邮箱管理。
              </Typography.Paragraph>
            </Card>
          </Col>
          <Col xs={24}>
            <InvitationPoolSnapshotCard />
          </Col>
        </Row>
        <Card title="最近运行" extra={<Link href="/invitation/runs">全部运行记录</Link>}>
          <Table
            rowKey="run_id"
            size="small"
            dataSource={data?.runs || []}
            pagination={false}
            scroll={{ x: 800 }}
            columns={[
              { title: '任务', dataIndex: 'run_id', ellipsis: true, width: 160 },
              { title: '邮箱', dataIndex: 'email', ellipsis: true },
              {
                title: '类型',
                dataIndex: 'kind',
                render: (kind) => <Tag color={runKindTone(kind)}>{runKindLabel(kind)}</Tag>,
              },
              {
                title: '状态',
                dataIndex: 'status',
                render: (status) => <InvitationStatus status={status} />,
              },
              { title: '开始时间', dataIndex: 'started_at', render: invitationTime },
              {
                title: '操作',
                render: (_, row) => (
                  <Button
                    type="link"
                    onClick={() => {
                      watchRun(row.run_id);
                      router.push('/invitation/runs');
                    }}
                  >
                    查看任务
                  </Button>
                ),
              },
            ]}
          />
        </Card>
        <InvitationRunPanel />
      </Space>
    </AdminPage>
  );
}
