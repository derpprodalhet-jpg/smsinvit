import { Alert, Button, Card, Space, Switch, Tabs, Tag, Typography, message } from 'antd';
import { useCallback, useEffect, useRef, useState } from 'react';
import { AdminPage } from '../../../components/admin-page';
import { adminApi } from '../../../lib/admin-api';
import { invitationErrorMessage } from '../../../lib/invitation-api';
import { Link } from '../../../lib/navigation';
import { ConfigEditor } from './config-editor';

type ExecutorState = {
  enabled: boolean;
  ready: boolean;
  state: 'disabled' | 'ready' | 'unavailable';
  message: string;
};
const EXECUTOR_PATH = '/api/v1/admin/invitation/executor';

type PoolState = {
  enabled: boolean;
  running: boolean;
  target: number;
  available: number;
  mailboxAvailable: number;
  concurrency: number;
  checkSeconds: number;
  rounds: number;
  lastError: string;
  /** 当前为什么没补号；空串表示条件齐备。 */
  blockedReason?: string;
  /** 池内已接码 / 未接码（含失败）账号数：已接码的号兑换时免 add-phone。 */
  smsBound?: number;
  smsUnbound?: number;
};
const POOL_PATH = '/api/v1/admin/invitation/pool';

/**
 * 后台常驻线程开关：接码价格监控与接码退款处理。
 *
 * 与「邀请执行 / 账号池自动补号」同一套契约：挂载只读、保存后必须权威复读一致才算成功，
 * 读不到状态时禁用一切写入，失败一律给中文错误。
 */
function SmsToggleCard({
  title,
  path,
  ariaLabel,
  description,
}: {
  title: string;
  path: string;
  ariaLabel: string;
  description: string;
}) {
  const [status, setStatus] = useState<{ enabled: boolean; running: boolean } | null>(null);
  // 读取动作 key：'load' 挂载读取、'refresh' 手动刷新、'reread' 保存或停止后的权威复读。
  // 一个 key 一个动作：保存后的复读不会点亮「刷新状态」，刷新也不会点亮开关。
  const [loading, setLoading] = useState<string | null>('load');
  // 每个按钮只在自己触发的请求进行时转圈：存的是动作 key，不是「有人在保存」。
  const [saving, setSavingAction] = useState<string | null>(null);
  const [error, setError] = useState('');
  const savingRef = useRef(false);
  const mounted = useRef(true);
  const load = useCallback(async (action: string = 'load') => {
    setLoading(action);
    setError('');
    try {
      const { data } = await adminApi<{ enabled: boolean; running: boolean }>(path);
      if (!data || typeof data.enabled !== 'boolean' || typeof data.running !== 'boolean')
        throw new Error(`${title}状态暂时无法读取，请刷新后再试。`);
      if (!mounted.current) return null;
      setStatus(data);
      return data;
    } catch (failure) {
      if (mounted.current) setError(invitationErrorMessage(failure));
      return null;
    } finally {
      if (mounted.current) setLoading(null);
    }
  }, [path, title]);
  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
    };
  }, [load]);
  async function save(enabled: boolean) {
    if (savingRef.current || loading !== null || !status || error || enabled === status.enabled)
      return;
    savingRef.current = true;
    setSavingAction('save');
    setError('');
    try {
      await adminApi(path, { method: 'PUT', body: JSON.stringify({ enabled }) });
      if (!mounted.current) return;
      const confirmed = await load('reread');
      if (!confirmed) return;
      if (confirmed.enabled !== enabled) {
        setError(`${title}设置尚未确认生效，请刷新后重试。`);
        return;
      }
      message.success(enabled ? `${title}已开启` : `${title}已关闭`);
    } catch (failure) {
      if (mounted.current) setError(`保存失败，请刷新核对当前状态：${invitationErrorMessage(failure)}`);
    } finally {
      savingRef.current = false;
      if (mounted.current) setSavingAction(null);
    }
  }
  const disabled = loading !== null || saving !== null || !status || Boolean(error);
  return (
    <Card title={title} style={{ marginBottom: 24 }}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text type="secondary">{description}</Typography.Text>
        {error ? <Alert type="error" showIcon message={error} /> : null}
        <Space wrap>
          <Switch
            aria-label={ariaLabel}
            checked={status?.enabled ?? false}
            checkedChildren="开启"
            unCheckedChildren="关闭"
            loading={saving !== null}
            disabled={disabled}
            onChange={(enabled) => void save(enabled)}
          />
          {status ? (
            <Tag color={status.running ? 'green' : status.enabled ? 'orange' : 'default'}>
              {!status.enabled ? '已关闭' : status.running ? '运行中' : '已开启，暂未运行'}
            </Tag>
          ) : (
            <Typography.Text type="secondary">正在读取状态</Typography.Text>
          )}
          <Button
            type="primary"
            loading={saving === 'save'}
            disabled={disabled}
            onClick={() => void save(!status?.enabled)}
          >
            {status?.enabled ? `关闭${title}` : `开启${title}`}
          </Button>
          <Button
            loading={loading === 'refresh'}
            disabled={saving !== null}
            onClick={() => void load('refresh')}
          >
            刷新状态
          </Button>
        </Space>
      </Space>
    </Card>
  );
}

export function InvitationSmsMonitorControl() {
  return (
    <SmsToggleCard
      title="接码价格监控"
      path="/api/v1/admin/invitation/sms-monitor"
      ariaLabel="接码价格监控"
      description="开启后后台持续轮询各接码平台的价格与库存，供接码价格页与自动择优使用；关闭只是停止轮询，不影响已开始的注册。"
    />
  );
}

export function InvitationSmsRefundControl() {
  return (
    <SmsToggleCard
      title="接码退款处理"
      path="/api/v1/admin/invitation/sms-refund"
      ariaLabel="接码退款处理"
      description="开启后后台自动处理需要退款的接码订单；关闭只是停止新轮次，正在处理的订单会走完当前一次确认。"
    />
  );
}

export function InvitationExecutorControl() {
  const [status, setStatus] = useState<ExecutorState | null>(null);
  // 读取动作 key：'load' 挂载读取、'refresh' 手动刷新、'reread' 保存或停止后的权威复读。
  // 一个 key 一个动作：保存后的复读不会点亮「刷新状态」，刷新也不会点亮开关。
  const [loading, setLoading] = useState<string | null>('load');
  // 每个按钮只在自己触发的请求进行时转圈：存的是动作 key，不是「有人在保存」。
  const [saving, setSavingAction] = useState<string | null>(null);
  const [error, setError] = useState('');
  const mounted = useRef(true);
  const savingRef = useRef(false);
  const readController = useRef<AbortController | null>(null);
  const writeController = useRef<AbortController | null>(null);
  const generation = useRef(0);

  const load = useCallback(async (action: string = 'load') => {
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    const current = ++generation.current;
    setLoading(action);
    setError('');
    try {
      const { data } = await adminApi<ExecutorState>(EXECUTOR_PATH, { signal: controller.signal });
      if (!mounted.current || controller.signal.aborted || current !== generation.current)
        return null;
      if (
        !data ||
        typeof data.enabled !== 'boolean' ||
        typeof data.ready !== 'boolean' ||
        !['disabled', 'ready', 'unavailable'].includes(data.state) ||
        typeof data.message !== 'string'
      )
        throw new Error('邀请执行状态暂时无法读取，请刷新后再试。');
      setStatus(data);
      return data;
    } catch (failure) {
      if (mounted.current && !controller.signal.aborted && current === generation.current)
        setError(invitationErrorMessage(failure));
      return null;
    } finally {
      if (mounted.current && current === generation.current) setLoading(null);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
      generation.current++;
      readController.current?.abort();
      writeController.current?.abort();
    };
  }, [load]);

  async function save(enabled: boolean) {
    if (savingRef.current || loading !== null || !status || error || enabled === status.enabled)
      return;
    savingRef.current = true;
    setSavingAction('save');
    setError('');
    readController.current?.abort();
    const controller = new AbortController();
    writeController.current = controller;
    try {
      await adminApi<ExecutorState>(EXECUTOR_PATH, {
        method: 'PUT',
        body: JSON.stringify({ enabled }),
        signal: controller.signal,
      });
      if (!mounted.current || controller.signal.aborted) return;
      const confirmed = await load('reread');
      if (!confirmed) return;
      if (confirmed.enabled !== enabled) {
        setError('邀请执行设置尚未确认生效，请刷新后重试。');
        return;
      }
      message.success(enabled ? '邀请执行已开启' : '邀请执行已关闭');
    } catch (failure) {
      if (mounted.current && !controller.signal.aborted)
        setError(`保存失败，请刷新核对当前状态：${invitationErrorMessage(failure)}`);
    } finally {
      savingRef.current = false;
      if (mounted.current) setSavingAction(null);
    }
  }

  const disabled = loading !== null || saving !== null || !status || Boolean(error);
  return (
    <Card title="邀请执行" style={{ marginBottom: 24 }}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text>
          开启后，用户可手动开始邀请任务；关闭后停止接收新任务，已排队和运行中的任务继续处理。保存立即生效。
        </Typography.Text>
        {error ? <Alert type="error" showIcon message={error} /> : null}
        <Space wrap>
          <Switch
            aria-label="邀请执行"
            checked={status?.enabled ?? false}
            checkedChildren="开启"
            unCheckedChildren="关闭"
            loading={saving !== null}
            disabled={disabled}
            onChange={(enabled) => void save(enabled)}
          />
          {status ? (
            <Tag color={status.ready ? 'green' : status.enabled ? 'orange' : 'default'}>
              {status.state === 'disabled'
                ? '已关闭'
                : status.state === 'ready'
                  ? '可接受新任务'
                  : '已开启，暂不可开始'}
            </Tag>
          ) : (
            <Typography.Text type="secondary">正在读取状态</Typography.Text>
          )}
          <Button
            type="primary"
            loading={saving === 'save'}
            disabled={disabled}
            onClick={() => void save(!status?.enabled)}
          >
            {status?.enabled ? '关闭邀请执行' : '开启邀请执行'}
          </Button>
          <Button
            loading={loading === 'refresh'}
            disabled={saving !== null}
            onClick={() => void load('refresh')}
          >
            刷新状态
          </Button>
        </Space>
        {status?.message ? (
          <Typography.Text type="secondary">{status.message}</Typography.Text>
        ) : null}
        <Link href="/invitation/proxies">配置代理池</Link>
      </Space>
    </Card>
  );
}

export function InvitationPoolControl() {
  const [status, setStatus] = useState<PoolState | null>(null);
  // 读取动作 key：'load' 挂载读取、'refresh' 手动刷新、'reread' 保存或停止后的权威复读。
  // 一个 key 一个动作：保存后的复读不会点亮「刷新状态」，刷新也不会点亮开关。
  const [loading, setLoading] = useState<string | null>('load');
  // 每个按钮只在自己触发的请求进行时转圈：存的是动作 key，不是「有人在保存」。
  const [saving, setSavingAction] = useState<string | null>(null);
  const [error, setError] = useState('');
  const mounted = useRef(true);
  const savingRef = useRef(false);
  const readController = useRef<AbortController | null>(null);
  const writeController = useRef<AbortController | null>(null);
  const generation = useRef(0);

  const load = useCallback(async (action: string = 'load') => {
    readController.current?.abort();
    const controller = new AbortController();
    readController.current = controller;
    const current = ++generation.current;
    setLoading(action);
    setError('');
    try {
      const { data } = await adminApi<PoolState>(POOL_PATH, { signal: controller.signal });
      if (!mounted.current || controller.signal.aborted || current !== generation.current)
        return null;
      if (
        !data ||
        typeof data.enabled !== 'boolean' ||
        typeof data.running !== 'boolean' ||
        typeof data.lastError !== 'string' ||
        (data.blockedReason !== undefined && typeof data.blockedReason !== 'string') ||
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
      setStatus(data);
      return data;
    } catch (failure) {
      if (mounted.current && !controller.signal.aborted && current === generation.current)
        setError(invitationErrorMessage(failure));
      return null;
    } finally {
      if (mounted.current && current === generation.current) setLoading(null);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
      generation.current++;
      readController.current?.abort();
      writeController.current?.abort();
    };
  }, [load]);

  async function save(enabled: boolean) {
    if (savingRef.current || loading !== null || !status || error || enabled === status.enabled)
      return;
    savingRef.current = true;
    setSavingAction('save');
    setError('');
    readController.current?.abort();
    const controller = new AbortController();
    writeController.current = controller;
    try {
      await adminApi<PoolState>(POOL_PATH, {
        method: 'PUT',
        body: JSON.stringify({ enabled }),
        signal: controller.signal,
      });
      if (!mounted.current || controller.signal.aborted) return;
      const confirmed = await load('reread');
      if (!confirmed) return;
      if (confirmed.enabled !== enabled) {
        setError('账号池设置尚未确认生效，请刷新后重试。');
        return;
      }
      message.success(enabled ? '账号池自动补号已开启' : '已停止接收新的补号；正在执行的轮次会被要求结束');
    } catch (failure) {
      if (mounted.current && !controller.signal.aborted)
        setError(`保存失败，请刷新核对当前状态：${invitationErrorMessage(failure)}`);
    } finally {
      savingRef.current = false;
      if (mounted.current) setSavingAction(null);
    }
  }

  /**
   * 手动结束本轮补号：只要求结束正在跑的轮次，不改开关。
   * 与关闭开关是两件事——关开关注重「不再接收」，这里注重「现在就停」。
   */
  async function stopRound() {
    if (saving !== null) return;
    setSavingAction('stop-round');
    setError('');
    try {
      await adminApi<{ stopped?: number }>(`${POOL_PATH}/stop`, { method: 'POST' });
      if (!mounted.current) return;
      message.success('已要求结束本轮补号；正在执行的轮次会尽快停止');
      await load('reread');
    } catch (failure) {
      if (mounted.current) setError(`停止本轮补号失败：${invitationErrorMessage(failure)}`);
    } finally {
      if (mounted.current) setSavingAction(null);
    }
  }

  const disabled = loading !== null || saving !== null || !status || Boolean(error);
  return (
    <Card title="账号池自动补号" style={{ marginBottom: 24 }}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Typography.Text>
          与邀请执行各自独立：这里只决定账号工厂是否自动补号，不影响邀请执行的开关与并发；反向也一样。保存立即生效。
        </Typography.Text>
        <Typography.Text type="secondary">
          开启后会使用代理池自动注册账号，可能消耗邮箱库存与接码余额。补号注册完成后停在绑定 2FA，不做 Codex
          授权，账号进入待兑换账号池等待 CDK 兑换。
        </Typography.Text>
        {error ? <Alert type="error" showIcon message={error} /> : null}
        <Space wrap>
          <Switch
            aria-label="账号池自动补号"
            checked={status?.enabled ?? false}
            checkedChildren="开启"
            unCheckedChildren="关闭"
            loading={saving !== null}
            disabled={disabled}
            onChange={(enabled) => void save(enabled)}
          />
          {status ? (
            <Tag color={status.running ? 'green' : status.enabled ? 'orange' : 'default'}>
              {!status.enabled ? '已关闭' : status.running ? '运行中' : '已开启，暂未运行'}
            </Tag>
          ) : (
            <Typography.Text type="secondary">正在读取状态</Typography.Text>
          )}
          <Button
            type="primary"
            loading={saving === 'save'}
            disabled={disabled}
            onClick={() => void save(!status?.enabled)}
          >
            {status?.enabled ? '关闭账号池自动补号' : '开启账号池自动补号'}
          </Button>
          <Button
            loading={loading === 'refresh'}
            disabled={saving !== null}
            onClick={() => void load('refresh')}
          >
            刷新状态
          </Button>
          <Button
            danger
            loading={saving === 'stop-round'}
            disabled={saving !== null}
            onClick={() => void stopRound()}
          >
            立即停止本轮补号
          </Button>
        </Space>
        {status ? (
          <Space wrap>
            <Tag>可兑换账号 {status.available}</Tag>
            <Tag>补号目标 {status.target}</Tag>
            <Tag>可注册邮箱 {status.mailboxAvailable}</Tag>
            <Tag>补号并发 {status.concurrency}</Tag>
            <Tag>巡检间隔 {status.checkSeconds} 秒</Tag>
            <Tag>已完成补号轮次 {status.rounds}</Tag>
            <Tag color="green">已接码 {status.smsBound ?? 0}</Tag>
            <Tag color="orange">未接码 {status.smsUnbound ?? 0}</Tag>
          </Space>
        ) : null}
        {status?.lastError ? (
          <Typography.Text type="danger">最近错误：{status.lastError}</Typography.Text>
        ) : null}
        {/* 开了开关却没补号时，必须在这里说清原因，而不是只让人去翻容器日志。 */}
        {status?.enabled && status?.blockedReason ? (
          <Typography.Text type="warning">当前未补号：{status.blockedReason}</Typography.Text>
        ) : null}
      </Space>
    </Card>
  );
}

export default function InvitationSettingsPage() {
  return (
    <AdminPage
      title="服务配置"
      description="统一管理 Codex 邀请的邮箱、注册接码、自动导出与执行设置。"
    >
      <InvitationExecutorControl />
      <InvitationPoolControl />
      <Tabs
        items={[
          { key: 'mail', label: '邮箱', children: <ConfigEditor section="mail" /> },
          { key: 'sms', label: '注册接码', children: <ConfigEditor section="sms" /> },
          { key: 'export', label: '自动导出', children: <ConfigEditor section="export" /> },
          { key: 'invite', label: '邀请与执行', children: <ConfigEditor section="invite" /> },
          { key: 'pool', label: '账号池', children: <ConfigEditor section="pool" /> },
        ]}
      />
      {/* 常驻线程开关放在页签之后：与「邀请执行 / 账号池自动补号」同一套交互。 */}
      <InvitationSmsMonitorControl />
      <InvitationSmsRefundControl />
    </AdminPage>
  );
}
