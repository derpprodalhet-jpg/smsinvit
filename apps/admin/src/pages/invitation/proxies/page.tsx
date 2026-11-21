import {
  Alert,
  Button,
  Card,
  Col,
  Input,
  Modal,
  Row,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import { useMemo, useRef, useState } from 'react';
import { AdminPage } from '../../../components/admin-page';
import { AdminDetailDrawer, type DetailGroup } from '../../../components/admin-detail-drawer';
import { InvitationStatus } from '../../../components/invitation-status';
import { resolveLabel } from '../../../lib/zh-label';
import { localizedText } from '../../../lib/zh-text';
import {
  copyInvitationText,
  invitationAction,
  invitationApi,
  invitationErrorMessage,
  invitationTime,
} from '../../../lib/invitation-api';
import { useInvitationQuery } from '../../../lib/use-invitation-query';
import { isInvitationMask } from '../settings/config-values';
import { mergeProxyTestResults, previewInvitationProxies, type ProxyTestResult } from './proxy-values';

type ProxyLock = {
  status: string;
  frozenRemaining: number;
  consecutive409: number;
  total409: number;
  frozenOnce: boolean;
  permanentDead: boolean;
  freezeCount: number;
};
const LOCK_LABELS: [string, string][] = [
  ['register', 'GPT 注册锁'],
  ['codex', 'Codex 激活锁'],
];
const LOCK_TONES: Record<string, string> = {
  ready: 'green',
  frozen: 'orange',
  dead: 'red',
  disabled: 'default',
};
type ProxyRow = {
  id: number;
  url: string;
  enabled: boolean;
  status: string;
  frozen_remaining?: number;
  /** 两个**互相独立**的 409 锁：register=GPT 注册，codex=Codex 激活。 */
  locks?: Record<string, ProxyLock>;
  /** 代理自身可用性（连不上 / CONNECT 被拒 / 超时）；与用途无关。 */
  health?: {
    status: string;
    frozenRemaining: number;
    consecutiveFailures: number;
    permanentDead: boolean;
    lastError: string;
  };
  use_count?: number;
  success_count?: number;
  fail_count?: number;
  consecutive_409?: number;
  total_409?: number;
  last_used_at?: number;
};
type PoolResponse = {
  items?: ProxyRow[];
  summary?: Record<string, number> & {
    locks?: Record<string, Record<string, number>>;
    health?: Record<string, number>;
  };
  config?: { threshold?: number; freeze_minutes?: number; attempts_limit?: number; purposes?: string[] };
};
type TestResult = ProxyTestResult;
type ProxyEvent = {
  id?: number;
  created_at?: number;
  event?: string;
  url?: string;
  detail?: string;
};
type Diagnosis = {
  checks?: (TestResult & { label?: string; proxy?: string })[];
  pool_ok?: boolean;
  base_ok?: boolean;
  verdict?: string;
};
const EVENT_LABELS: Record<string, string> = {
  add: '新增',
  use: '选用',
  success: '成功',
  fail: '失败',
  '409': '409',
  freeze: '冻结',
  permanent: '永久不可用',
  reset: '重置',
  scheme: '更换协议',
  enable: '启用',
  disable: '禁用',
  remove: '删除',
};
const LEGACY_POOL_KEY = 'dango_proxy_pool_v1';
function readLegacyPool(): string {
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(LEGACY_POOL_KEY) || '[]');
    return Array.isArray(saved) && saved.every((value) => typeof value === 'string')
      ? saved.join('\n')
      : '';
  } catch {
    return '';
  }
}

export default function InvitationProxiesPage() {
  const pool = useInvitationQuery<PoolResponse>('/proxy/pool');
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState('');
  const [results, setResults] = useState<Record<string, TestResult>>({});
  const [testing, setTesting] = useState<string[]>([]);
  const testInFlight = useRef(false);
  const [diagnosis, setDiagnosis] = useState<Diagnosis | null>(null);
  const [events, setEvents] = useState<{ title: string; items: ProxyEvent[] } | null>(null);
  const [legacyPool, setLegacyPool] = useState(readLegacyPool);
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [rowDetail, setRowDetail] = useState<ProxyRow | null>(null);
  const [draftScheme, setDraftScheme] = useState<'auto' | 'http' | 'socks5h'>('auto');
  const preview = useMemo(() => previewInvitationProxies(draft, draftScheme), [draft, draftScheme]);
  const items = pool.data?.items || [];
  const masked = items.some((item) => isInvitationMask(item.url));
  const currentText = items.map((item) => item.url).join('\n');
  /**
   * 「有动作在跑」与「只读测试在跑」必须分开。
   *
   * 这里曾经把两者合成一个 `blocked = !!busy || !!testing.length` 的全局锁：点一次测试，
   * 每个代理 timeout 12 秒，期间整页按钮全灰，运营以为页面坏了。只读测试绝不能锁住
   * 写操作，也不该锁住别的只读入口。
   * 声明放在这里（而不是 return 之前）是因为 detailGroups 在渲染期就会调用
   * connectivityDetail，晚声明会踩到暂时性死区。
   */
  const actionBusy = !!busy;
  const testingNow = !!testing.length;

  async function mutate(path: string, body: unknown, success: string) {
    setBusy(path);
    try {
      const result = await invitationAction<{ added?: number; skipped?: number; parsed?: number }>(
        path,
        body,
      );
      message.success(
        result.parsed != null
          ? `${success} ${result.parsed} 条，新增 ${result.added || 0}，保留或跳过 ${result.skipped || 0}`
          : success,
      );
      await pool.reload();
      return true;
    } catch (error) {
      message.error(invitationErrorMessage(error));
      return false;
    } finally {
      setBusy('');
    }
  }
  async function importPool(replace: boolean) {
    if (!preview.urls.length || preview.invalid.length) {
      message.warning(preview.invalid.length ? '请先修正格式不正确的行' : '请先输入代理');
      return;
    }
    const execute = async () => {
      if (
        await mutate(
          `/proxy/pool/${replace ? 'replace' : 'add'}`,
          { text: preview.urls.join('\n') },
          replace ? '已覆盖保存' : '已追加合并',
        )
      )
        setDraft('');
    };
    if (replace)
      Modal.confirm({
        title: '覆盖服务端代理池？',
        content: '未出现在编辑框中的代理将删除；仍存在的代理保留统计记录。',
        onOk: execute,
      });
    else await execute();
  }
  async function importLegacyPool() {
    const legacy = previewInvitationProxies(legacyPool);
    if (legacy.invalid.length || !legacy.urls.length) {
      setDraft(legacyPool);
      message.warning('旧代理池包含格式错误，请在编辑框修正后导入');
      return;
    }
    if (await mutate('/proxy/pool/add', { text: legacy.urls.join('\n') }, '旧代理池已导入')) {
      try {
        localStorage.removeItem(LEGACY_POOL_KEY);
      } catch {
        /* A blocked browser store does not change the successful server import. */
      }
      setLegacyPool('');
    }
  }
  async function testProxies(urls: string[]) {
    if (testInFlight.current) return;
    if (!urls.length || urls.some(isInvitationMask)) {
      message.warning('当前账号无法读取代理地址，请使用具有凭证权限的账号');
      return;
    }
    testInFlight.current = true;
    setTesting(urls);
    try {
      const response = await invitationAction<{ results?: Record<string, TestResult> }>(
        '/proxy/test',
        { proxies: urls, timeout: 12 },
      );
      setResults((previous) => mergeProxyTestResults(previous, urls, response.results));
    } catch (error) {
      const errorText = invitationErrorMessage(error);
      setResults((previous) => mergeProxyTestResults(previous, urls, undefined, errorText));
      message.error(errorText);
    } finally {
      testInFlight.current = false;
      setTesting([]);
    }
  }
  async function diagnose() {
    setBusy('diagnose');
    try {
      setDiagnosis(await invitationAction<Diagnosis>('/proxy/diagnose'));
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  async function showEvents(row?: ProxyRow) {
    setBusy('events');
    try {
      const response = await invitationApi<{ items?: ProxyEvent[] }>(
        `/proxy/pool/events?limit=200${row ? `&proxy_id=${row.id}` : ''}`,
      );
      setEvents({
        title: row ? `代理 #${row.id} 事件` : '全部代理事件',
        items: response.items || [],
      });
    } catch (error) {
      message.error(invitationErrorMessage(error));
    } finally {
      setBusy('');
    }
  }
  /** 列表里的连通性摘要：只给结论，IP / 错误 / 建议都收进详情。 */
  function connectivitySummary(row: ProxyRow) {
    const result = results[row.url];
    if (testing.includes(row.url)) return <Tag color="processing">测试中</Tag>;
    if (!result) return <Typography.Text type="secondary">未测试</Typography.Text>;
    return (
      <Tag
        color={
          result.request_error ? 'orange' : result.ok ? 'green' : result.reachable ? 'orange' : 'red'
        }
      >
        {result.request_error
          ? '本次检测未完成'
          : result.ok
            ? `检测通过 · ${result.latency_ms ?? '—'} 毫秒`
            : result.reachable
              ? '代理可达，目标检测未通过'
              : '连接检测未通过'}
      </Tag>
    );
  }
  /**
   * 连通性诊断详情。
   *
   * `request_error` 是前端生成的说明，`error` / `hint` 是后端的中文分类结论，
   * 都可能是中文句子；`localizedText` 让中文原样通过、真错误码走中文映射，
   * 剩下的英文一律包成「未知（原文）」，绝不漏裸英文。
   */
  function connectivityDetail(row: ProxyRow) {
    const result = results[row.url];
    if (testing.includes(row.url)) return <Tag color="processing">测试中</Tag>;
    if (!result) return <Typography.Text type="secondary">尚未测试</Typography.Text>;
    return (
      <Space direction="vertical" size={4}>
        {connectivitySummary(row)}
        {result.request_error && (
          <Typography.Text type="warning">{localizedText(result.request_error)}</Typography.Text>
        )}
        {result.request_error && typeof result.ok === 'boolean' && (
          <Typography.Text type="secondary">以下为上次检测结果，尚未重新确认</Typography.Text>
        )}
        {result.ip && <Typography.Text>出口 IP：{result.ip}</Typography.Text>}
        {!result.ok && result.error && (
          <Typography.Text type="danger">{localizedText(result.error)}</Typography.Text>
        )}
        {result.hint && (
          <Typography.Text type="secondary">{localizedText(result.hint)}</Typography.Text>
        )}
        {row.health?.lastError && (
          <Typography.Text type="secondary">
            最近一次不可用原因：{localizedText(row.health.lastError)}
          </Typography.Text>
        )}
        {result.alt_scheme && (
          <Button
            size="small"
            disabled={actionBusy || masked}
            onClick={() => switchScheme(row, result.alt_scheme!)}
          >
            改用 {result.alt_scheme.split('://')[0]}
          </Button>
        )}
      </Space>
    );
  }
  /** 锁状态明细：代理自身可用性 + 两个用途各自独立的 409 锁。 */
  function lockDetails(row: ProxyRow) {
    return (
      <Space direction="vertical" size={4}>
        {row.health ? (
          <Space size={4} wrap>
            <Tag color={LOCK_TONES[row.health.status] || 'default'}>代理可用性</Tag>
            <InvitationStatus status={row.health.status} />
            {row.health.status === 'frozen' && (
              <Typography.Text type="secondary">
                {Math.ceil(row.health.frozenRemaining / 60)} 分钟
              </Typography.Text>
            )}
            {row.health.consecutiveFailures > 0 && (
              <Typography.Text type="secondary">
                连续不可用 {row.health.consecutiveFailures} 次
              </Typography.Text>
            )}
            {row.health.permanentDead && <Tag color="red">已永久不可用</Tag>}
          </Space>
        ) : (
          <Typography.Text type="secondary">未提供代理可用性信息</Typography.Text>
        )}
        {LOCK_LABELS.map(([purpose, label]) => {
          const lock = row.locks?.[purpose];
          if (!lock) return null;
          return (
            <Space key={purpose} size={4} wrap>
              <Tag color={LOCK_TONES[lock.status] || 'default'}>{label}</Tag>
              <InvitationStatus status={lock.status} />
              <Typography.Text type="secondary">
                {lock.frozenRemaining > 0
                  ? `剩余 ${Math.ceil(lock.frozenRemaining / 60)} 分钟`
                  : '未冻结'}
                {' · '}
                连续 409 {lock.consecutive409} 次 / 累计 {lock.total409} 次
                {lock.frozenOnce ? ' · 冻结过' : ''}
                {lock.permanentDead ? ' · 已永久不可用' : ''}
                {lock.freezeCount ? ` · 累计冻结 ${lock.freezeCount} 次` : ''}
              </Typography.Text>
            </Space>
          );
        })}
      </Space>
    );
  }
  const detailGroups: DetailGroup[] = rowDetail
    ? [
        {
          key: 'proxy',
          title: '代理',
          items: [
            { key: 'id', label: '编号', children: `#${rowDetail.id}` },
            {
              key: 'url',
              label: '地址',
              children: isInvitationMask(rowDetail.url)
                ? '已脱敏（需要凭证权限）'
                : rowDetail.url,
            },
            {
              key: 'protocol',
              label: '协议',
              children: rowDetail.url.includes('://')
                ? rowDetail.url.split('://')[0].toUpperCase()
                : '—',
            },
            { key: 'enabled', label: '启用状态', children: rowDetail.enabled ? '已启用' : '已禁用' },
            { key: 'use', label: '使用次数', children: rowDetail.use_count ?? 0 },
            {
              key: 'result',
              label: '成功 / 失败',
              children: `${rowDetail.success_count || 0} / ${rowDetail.fail_count || 0}`,
            },
            {
              key: 'last',
              label: '最后使用',
              children: invitationTime(rowDetail.last_used_at),
            },
          ],
        },
        {
          key: 'locks',
          title: '锁状态与 409 计数',
          items: [{ key: 'lock', label: '明细', children: lockDetails(rowDetail), span: 1 }],
        },
        {
          key: 'connectivity',
          title: '连通性诊断',
          items: [{ key: 'connectivity', label: '本次结果', children: connectivityDetail(rowDetail) }],
        },
      ]
    : [];

  /**
   * 按 id 原地换协议：服务端自己拼地址，所以
   *   · 不依赖前端手里的（可能已脱敏的）地址，脱敏账号也能切；
   *   · url 是唯一键，重写整池等于删旧行插新行、统计全部归零，这里保留同一行。
   */
  function switchScheme(row: ProxyRow, scheme: string) {
    const wanted = scheme.split('://')[0];
    Modal.confirm({
      title: `把代理 #${row.id} 改为 ${wanted}？`,
      content:
        '保留该代理的选用与失败统计；换协议同时解除冻结与永久不可用（换了传输方式，旧锁不再成立）。',
      onOk: () =>
        mutate('/proxy/pool/scheme', { id: row.id, scheme: wanted, clearLocks: true },
               `#${row.id} 已改用 ${wanted}，请重新测试`),
    });
  }

  async function switchSelectedSchemes(scheme: 'http' | 'socks5h') {
    const targets = items.filter((item) => selectedIds.includes(item.id));
    if (!targets.length) {
      message.warning('请先勾选要更换协议的代理');
      return;
    }
    setBusy('scheme-bulk');
    let changed = 0;
    const failures: string[] = [];
    try {
      // 逐行调用：一行失败不该让其余行悄悄没改，也要能说清是哪几行。
      for (const item of targets) {
        try {
          await invitationAction('/proxy/pool/scheme', { id: item.id, scheme, clearLocks: true });
          changed += 1;
        } catch (error) {
          failures.push(`#${item.id}：${invitationErrorMessage(error)}`);
        }
      }
      await pool.reload();
      if (failures.length) {
        message.warning(`已改 ${changed} 条，失败 ${failures.length} 条 —— ${failures.join('；')}`);
      } else {
        message.success(`已把 ${changed} 条改为 ${scheme}`);
        setSelectedIds([]);
      }
    } finally {
      setBusy('');
    }
  }
  async function copyAll() {
    try {
      await copyInvitationText(currentText);
      message.success('已复制代理池');
    } catch (error) {
      message.error(invitationErrorMessage(error));
    }
  }
  return (
    <AdminPage
      title="代理池"
      description="管理 Codex 邀请的服务端代理、连通性和生命周期。"
      extra={
        <Button onClick={() => void pool.reload()} loading={pool.loading}>
          刷新
        </Button>
      }
    >
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {pool.error && <Alert type="error" showIcon message={pool.error} />}
        <Card
          title="服务端代理池"
          extra={
            <Space wrap>
              <Space size={4}>
                <Tag>代理可用性</Tag>
                <Tag color="green">可用 {pool.data?.summary?.health?.ready || 0}</Tag>
                <Tag color="orange">冻结 {pool.data?.summary?.health?.frozen || 0}</Tag>
                <Tag color="red">不可用 {pool.data?.summary?.health?.dead || 0}</Tag>
              </Space>
              {LOCK_LABELS.map(([purpose, label]) => {
                const counts = pool.data?.summary?.locks?.[purpose];
                return (
                  <Space key={purpose} size={4}>
                    <Tag>{label}</Tag>
                    <Tag color="green">可用 {counts?.ready || 0}</Tag>
                    <Tag color="orange">冻结 {counts?.frozen || 0}</Tag>
                    <Tag color="red">不可用 {counts?.dead || 0}</Tag>
                  </Space>
                );
              })}
            </Space>
          }
        >
          <Space wrap style={{ marginBottom: 16 }}>
            <Button
              disabled={actionBusy || testingNow || !items.length || masked}
              loading={testingNow}
              onClick={() => void testProxies(items.map((item) => item.url))}
            >
              测试全部
            </Button>
            <Button
              disabled={actionBusy}
              loading={busy === 'diagnose'}
              onClick={() => void diagnose()}
            >
              代理路径诊断
            </Button>
            <Button disabled={actionBusy} onClick={() => void showEvents()}>
              事件流水
            </Button>
            <Button disabled={!items.length || masked} onClick={() => void copyAll()}>
              复制全部
            </Button>
            <Button
              disabled={actionBusy || !selectedIds.length}
              loading={busy === 'scheme-bulk'}
              onClick={() => void switchSelectedSchemes('socks5h')}
            >
              选中改为 SOCKS5
            </Button>
            <Button
              disabled={actionBusy || !selectedIds.length}
              loading={busy === 'scheme-bulk'}
              onClick={() => void switchSelectedSchemes('http')}
            >
              选中改为 HTTP(S)
            </Button>
            <Button
              danger
              disabled={actionBusy || !items.length}
              onClick={() =>
                Modal.confirm({
                  title: `清空全部 ${items.length} 个代理？`,
                  content: '清空后没有可用代理的任务将无法开始。',
                  okButtonProps: { danger: true },
                  onOk: () => mutate('/proxy/pool/replace', { text: '' }, '代理池已清空'),
                })
              }
            >
              清空代理池
            </Button>
          </Space>
          {masked && (
            <Alert
              type="info"
              showIcon
              message="代理地址已按账号权限脱敏；可管理状态，完整复制、载入编辑和连通性测试需要凭证权限。"
              style={{ marginBottom: 16 }}
            />
          )}
          <Table
            rowKey="id"
            size="small"
            dataSource={items}
            loading={pool.loading && !pool.data}
            scroll={{ x: 1450 }}
            pagination={{ pageSize: 20, showSizeChanger: true }}
            rowSelection={{
              selectedRowKeys: selectedIds,
              onChange: (keys) => setSelectedIds(keys.map(Number)),
            }}
            columns={[
              { title: '代理', dataIndex: 'url', width: 240, ellipsis: true },
              {
                title: '协议',
                width: 90,
                render: (_, row) => (row.url.includes('://') ? row.url.split('://')[0] : '—'),
              },
              { title: '使用', dataIndex: 'use_count', width: 65 },
              {
                title: '成功 / 失败',
                width: 110,
                render: (_, row) => `${row.success_count || 0} / ${row.fail_count || 0}`,
              },
              {
                // 只给结论：锁明细、409 计数、IP/错误/建议全部收进「详情」。
                title: '连通性摘要',
                width: 240,
                render: (_, row) => connectivitySummary(row),
              },
              {
                title: '操作',
                width: 240,
                fixed: 'right',
                render: (_, row) => (
                  <Space wrap size={4}>
                    <Button
                      size="small"
                      type="link"
                      disabled={actionBusy || testingNow || isInvitationMask(row.url)}
                      loading={testing.includes(row.url)}
                      onClick={() => void testProxies([row.url])}
                    >
                      测试
                    </Button>
                    <Button
                      size="small"
                      type="link"
                      disabled={actionBusy}
                      onClick={() =>
                        void mutate(
                          '/proxy/pool/enable',
                          { id: row.id, enabled: !row.enabled },
                          row.enabled ? '已禁用' : '已启用',
                        )
                      }
                    >
                      {row.enabled ? '禁用' : '启用'}
                    </Button>
                    <Button size="small" type="link" onClick={() => setRowDetail(row)}>
                      详情
                    </Button>
                    {/* 删除原来只藏在「详情」抽屉里，表格行上没有任何入口 —— 想删一条
                        就得先点进详情，操作员会以为只能「清空代理池」。这里补到行上，
                        与抽屉里的删除走同一个接口、同一套确认文案。 */}
                    <Button
                      size="small"
                      type="link"
                      danger
                      disabled={actionBusy}
                      onClick={() =>
                        Modal.confirm({
                          title: `删除代理 #${row.id}？`,
                          okButtonProps: { danger: true },
                          onOk: () => mutate('/proxy/pool/remove', { id: row.id }, '已删除代理'),
                        })
                      }
                    >
                      删除
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
          <Alert
            showIcon
            type="info"
            message={`轮换优先使用次数最少的可用代理；连续 ${pool.data?.config?.threshold || 2} 次 409 冻结 ${pool.data?.config?.freeze_minutes || 20} 分钟，解冻后再次 409 将永久不可用。`}
          />
        </Card>
        <Row gutter={[16, 16]}>
          <Col xs={24} lg={12}>
            <Card title="批量导入与编辑">
              <Typography.Paragraph type="secondary">
                每行一个代理，支持 host:port、user:pass@host:port 或
                host:port:user:pass。省略协议使用 HTTP，SOCKS5 自动使用远端 DNS。空行与 #
                注释会忽略。
              </Typography.Paragraph>
              <Select
                aria-label="导入协议"
                value={draftScheme}
                onChange={(value) => setDraftScheme(value)}
                style={{ width: 240, marginBottom: 8 }}
                options={[
                  { value: 'auto', label: '按行内协议（默认 HTTP）' },
                  { value: 'http', label: '一律按 HTTP' },
                  { value: 'socks5h', label: '一律按 SOCKS5（远端解析）' },
                ]}
              />
              <Input.TextArea
                rows={9}
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                placeholder={'socks5://127.0.0.1:7890\nhttp://user:pass@host:8080'}
              />
              <Space wrap style={{ marginTop: 16 }}>
                <Button
                  type="primary"
                  disabled={actionBusy || !preview.urls.length || !!preview.invalid.length}
                  onClick={() => void importPool(false)}
                >
                  追加合并
                </Button>
                <Button
                  disabled={actionBusy || !preview.urls.length || !!preview.invalid.length}
                  onClick={() => void importPool(true)}
                >
                  覆盖保存
                </Button>
                <Button
                  disabled={masked || !items.length}
                  onClick={() => {
                    if (draft.trim())
                      Modal.confirm({
                        title: '载入当前池并替换编辑框内容？',
                        onOk: () => setDraft(currentText),
                      });
                    else setDraft(currentText);
                  }}
                >
                  载入当前池
                </Button>
              </Space>
              {legacyPool && (
                <Space wrap style={{ marginTop: 12 }}>
                  <Button
                    disabled={actionBusy}
                    onClick={() =>
                      Modal.confirm({
                        title: '将浏览器旧代理池导入服务端？',
                        content: '追加合并成功后移除旧浏览器副本。',
                        onOk: importLegacyPool,
                      })
                    }
                  >
                    迁移浏览器旧池
                  </Button>
                  <Button
                    onClick={() => {
                      if (draft.trim())
                        Modal.confirm({
                          title: '载入旧池并替换编辑框内容？',
                          onOk: () => setDraft(legacyPool),
                        });
                      else setDraft(legacyPool);
                    }}
                  >
                    载入旧池编辑
                  </Button>
                </Space>
              )}
            </Card>
          </Col>
          <Col xs={24} lg={12}>
            <Card
              title={`归一化预览 · 有效 ${preview.urls.length} · 重复 ${preview.duplicates.length} · 错误 ${preview.invalid.length}`}
            >
              <Table
                size="small"
                rowKey="line"
                dataSource={preview.rows}
                pagination={{ pageSize: 8 }}
                scroll={{ x: 500 }}
                columns={[
                  { title: '行号', dataIndex: 'line', width: 60 },
                  { title: '规范地址', render: (_, row) => row.url || row.raw },
                  {
                    title: '校验',
                    width: 85,
                    render: (_, row) => (
                      <Tag
                        color={
                          row.status === 'invalid'
                            ? 'red'
                            : row.status === 'duplicate'
                              ? 'orange'
                              : 'green'
                        }
                      >
                        {row.status === 'invalid'
                          ? '格式错误'
                          : row.status === 'duplicate'
                            ? '重复'
                            : '有效'}
                      </Tag>
                    ),
                  },
                ]}
              />
            </Card>
          </Col>
        </Row>
      </Space>
      <AdminDetailDrawer
        title={`代理详情 · #${rowDetail?.id ?? ''}`}
        subtitle="锁明细、409 计数与连通性诊断都在这里；两把业务锁与代理可用性互相独立。"
        open={!!rowDetail}
        onClose={() => setRowDetail(null)}
        extra={
          <>
            <Button
              disabled={actionBusy || !rowDetail}
              onClick={() =>
                Modal.confirm({
                  title: `重置代理 #${rowDetail?.id}？`,
                  content: '清空成功、失败和两个用途的 409 计数，并解除冻结及永久不可用标记。',
                  onOk: () =>
                    rowDetail && mutate('/proxy/pool/reset', { id: rowDetail.id }, '已重置代理'),
                })
              }
            >
              重置
            </Button>
            <Button
              disabled={actionBusy || !rowDetail || rowDetail.health?.status === 'ready'}
              onClick={() =>
                Modal.confirm({
                  title: `重置代理 #${rowDetail?.id} 的可用性？`,
                  content: '只清「代理自身不可用」的计数与冻结/永久标记；两把业务锁不受影响。',
                  onOk: () =>
                    rowDetail &&
                    mutate(
                      '/proxy/pool/reset?purpose=health',
                      { id: rowDetail.id },
                      '已重置代理可用性',
                    ),
                })
              }
            >
              重置可用性
            </Button>
            {rowDetail &&
              LOCK_LABELS.map(([purpose, label]) => (
                <Button
                  key={purpose}
                  disabled={actionBusy || !rowDetail.locks?.[purpose]?.permanentDead}
                  onClick={() =>
                    Modal.confirm({
                      title: `只重置「${label}」这一把锁？`,
                      content: '另一个用途的 409 计数、冻结与永久标记保持不变。',
                      onOk: () =>
                        mutate(
                          `/proxy/pool/reset?purpose=${purpose}`,
                          { id: rowDetail.id },
                          `已重置${label}`,
                        ),
                    })
                  }
                >
                  重置{label}
                </Button>
              ))}
            <Button
              disabled={actionBusy || testingNow || !rowDetail || isInvitationMask(rowDetail.url)}
              loading={!!rowDetail && testing.includes(rowDetail.url)}
              onClick={() => rowDetail && void testProxies([rowDetail.url])}
            >
              测试连通性
            </Button>
            <Button disabled={actionBusy || !rowDetail} onClick={() => rowDetail && void showEvents(rowDetail)}>
              事件流水
            </Button>
            <Button
              danger
              disabled={actionBusy || !rowDetail}
              onClick={() =>
                Modal.confirm({
                  title: `删除代理 #${rowDetail?.id}？`,
                  okButtonProps: { danger: true },
                  onOk: () =>
                    rowDetail && mutate('/proxy/pool/remove', { id: rowDetail.id }, '已删除代理'),
                })
              }
            >
              删除
            </Button>
          </>
        }
        groups={detailGroups}
      />
      <Modal
        open={!!diagnosis}
        onCancel={() => setDiagnosis(null)}
        title="代理路径诊断"
        width={1050}
        footer={null}
        destroyOnHidden
      >
        <Table
          size="small"
          rowKey={(_, index) => String(index)}
          pagination={false}
          scroll={{ x: 800 }}
          dataSource={diagnosis?.checks || []}
          columns={[
            { title: '路径', dataIndex: 'label' },
            { title: '代理', dataIndex: 'proxy', ellipsis: true },
            {
              title: '结果',
              render: (_, row) => (
                <Tag color={row.ok ? 'green' : row.ok === false ? 'red' : undefined}>
                  {row.ok ? '成功' : row.ok === false ? '失败' : '未测试'}
                </Tag>
              ),
            },
            { title: '出口 IP', dataIndex: 'ip' },
            { title: '耗时（毫秒）', dataIndex: 'latency_ms' },
            {
              title: '错误与建议',
              render: (_, row) =>
                [row.error, row.hint].filter(Boolean).map((value) => localizedText(value)).join('；') ||
                '—',
            },
          ]}
        />
        {diagnosis?.verdict && (
          <Alert
            style={{ marginTop: 16 }}
            showIcon
            type={diagnosis.pool_ok ? 'success' : diagnosis.base_ok ? 'warning' : 'error'}
            message={diagnosis.verdict}
          />
        )}
      </Modal>
      <Modal
        open={!!events}
        title={events?.title}
        onCancel={() => setEvents(null)}
        width={950}
        footer={null}
        destroyOnHidden
      >
        <Table
          rowKey={(row, index) => String(row.id || index)}
          size="small"
          dataSource={events?.items || []}
          scroll={{ x: 700 }}
          pagination={{ pageSize: 15 }}
          columns={[
            { title: '时间', dataIndex: 'created_at', render: invitationTime, width: 180 },
            {
              title: '事件',
              dataIndex: 'event',
              render: (event: string) => resolveLabel(EVENT_LABELS, event),
              width: 110,
            },
            { title: '代理', dataIndex: 'url', ellipsis: true },
            { title: '详情', dataIndex: 'detail' },
          ]}
        />
      </Modal>
    </AdminPage>
  );
}
