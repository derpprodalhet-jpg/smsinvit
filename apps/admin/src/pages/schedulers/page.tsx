import { Button, Card, Input, Modal, Space, Switch, Table, Typography, message } from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { AdminPage } from '../../components/admin-page';
import { AdminStatusTag, toneFromStatus } from '../../components/admin-feedback';
import { AdminDetailDrawer, CopyableValue } from '../../components/admin-detail-drawer';
import { adminApi } from '../../lib/admin-api';
import { formatDateTime } from '../../lib/format-datetime';
import { moduleErrorLabel, schedulerGroupLabel, schedulerStatusLabel } from '../../lib/zh-maps';
import { FieldTimeOutlined, PlayCircleOutlined, ReloadOutlined } from '@ant-design/icons';

type ScheduledTask = {
  id: string;
  name: string;
  group: string;
  description?: string;
  enabled: boolean;
  intervalMs: number;
  cron?: string;
  lastRunAt?: string | null;
  nextDueAt?: string | null;
  runCount: number;
  lastError?: string | null;
  status: string;
};

type SchedulerStatus = {
  tasks: ScheduledTask[];
};

/** 间隔一律用中文单位，避免「ms / s / min」混排。 */
function formatMs(ms: number) {
  if (!ms || ms < 0) return '—';
  if (ms < 1000) return `${ms} 毫秒`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms % 1000 === 0 ? 0 : 1)} 秒`;
  return `${(ms / 60_000).toFixed(ms % 60_000 === 0 ? 0 : 1)} 分钟`;
}

export default function SchedulersPage() {
  const [data, setData] = useState<SchedulerStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ScheduledTask | null>(null);
  const [cronEdit, setCronEdit] = useState<{ id: string; name: string; cron: string } | null>(null);
  const [cronDraft, setCronDraft] = useState('');
  const [cronSaving, setCronSaving] = useState(false);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const result = await adminApi<SchedulerStatus>('/api/v1/admin/schedulers');
      setData(result.data);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '加载失败');
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  function openCronEdit(row: ScheduledTask) {
    setCronEdit({ id: row.id, name: row.name, cron: row.cron || '' });
    setCronDraft(row.cron || '');
  }

  async function toggleEnabled(row: ScheduledTask, enabled: boolean) {
    setBusyId(row.id);
    try {
      await adminApi(`/api/v1/admin/schedulers/${encodeURIComponent(row.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled }),
      });
      message.success(enabled ? '已开启' : '已关闭');
      await load(true);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '更新失败');
    } finally {
      setBusyId(null);
    }
  }

  async function runOnce(row: ScheduledTask) {
    setBusyId(row.id);
    try {
      const result = await adminApi<{ id: string; ran: boolean; error?: string }>(
        `/api/v1/admin/schedulers/${encodeURIComponent(row.id)}/run`,
        { method: 'POST', body: '{}' },
      );
      if (result.data?.error)
        message.warning(`已执行，但有错误：${moduleErrorLabel(result.data.error)}`);
      else message.success('已立即执行一次');
      await load(true);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '执行失败');
    } finally {
      setBusyId(null);
    }
  }

  async function saveCron() {
    if (!cronEdit) return;
    setCronSaving(true);
    try {
      await adminApi(`/api/v1/admin/schedulers/${encodeURIComponent(cronEdit.id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ cron: cronDraft.trim() }),
      });
      message.success(cronDraft.trim() ? 'Cron 已保存' : '已清除 Cron，改回间隔调度');
      setCronEdit(null);
      await load(true);
    } catch (error) {
      message.error(error instanceof Error ? error.message : '保存失败');
    } finally {
      setCronSaving(false);
    }
  }

  return (
    <AdminPage
      title="定时任务"
      description="可开关、设置 Cron（留空则用运行时间隔）、立即执行。Cron 表达式、累计次数与最近错误见「详情」。"
      extra={
        <Button icon={<ReloadOutlined />} onClick={() => void load()} loading={loading}>
          刷新
        </Button>
      }
    >
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        <Card
          title={
            <Space>
              <FieldTimeOutlined />
              调度循环
            </Space>
          }
          bordered={false}
        >
          <Table<ScheduledTask>
            rowKey="id"
            loading={loading && !data}
            dataSource={data?.tasks || []}
            pagination={false}
            scroll={{ x: 1080 }}
            columns={[
              {
                title: '任务',
                dataIndex: 'name',
                render: (_, row) => (
                  <div>
                    <Typography.Text strong>{row.name}</Typography.Text>
                    <div>
                      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                        {row.description || row.id}
                      </Typography.Text>
                    </div>
                  </div>
                ),
              },
              {
                title: '分组',
                dataIndex: 'group',
                width: 120,
                render: (group: string) => schedulerGroupLabel(group),
              },
              {
                title: '状态',
                dataIndex: 'status',
                width: 120,
                render: (status: string) => (
                  <AdminStatusTag tone={toneFromStatus(status)}>
                    {schedulerStatusLabel(status)}
                  </AdminStatusTag>
                ),
              },
              {
                title: '开关',
                dataIndex: 'enabled',
                width: 80,
                render: (_, row) => (
                  <Switch
                    checked={row.enabled}
                    loading={busyId === row.id}
                    onChange={(checked) => void toggleEnabled(row, checked)}
                  />
                ),
              },
              {
                title: '间隔',
                dataIndex: 'intervalMs',
                width: 100,
                render: (ms: number) => formatMs(ms),
              },
              {
                title: '上次执行',
                dataIndex: 'lastRunAt',
                width: 170,
                render: (value?: string | null) => formatDateTime(value),
              },
              {
                title: '下次执行',
                dataIndex: 'nextDueAt',
                width: 170,
                render: (value?: string | null) => formatDateTime(value),
              },
              {
                title: '操作',
                width: 200,
                fixed: 'right',
                render: (_, row) => (
                  <Space wrap>
                    <Button
                      size="small"
                      icon={<PlayCircleOutlined />}
                      loading={busyId === row.id}
                      onClick={() => void runOnce(row)}
                    >
                      立即执行
                    </Button>
                    <Button type="link" size="small" onClick={() => setDetail(row)}>
                      详情
                    </Button>
                  </Space>
                ),
              },
            ]}
          />
        </Card>
      </Space>

      <AdminDetailDrawer
        title="定时任务详情"
        subtitle={detail ? `任务 ID：${detail.id}` : undefined}
        open={!!detail}
        onClose={() => setDetail(null)}
        extra={
          detail ? (
            <Button onClick={() => openCronEdit(detail)}>编辑 Cron</Button>
          ) : undefined
        }
        groups={
          detail
            ? [
                {
                  key: 'task',
                  title: '任务',
                  items: [
                    { key: 'name', label: '任务名称', children: detail.name },
                    { key: 'id', label: '任务 ID', children: <CopyableValue value={detail.id} /> },
                    { key: 'group', label: '分组', children: schedulerGroupLabel(detail.group) },
                    {
                      key: 'status',
                      label: '状态',
                      children: (
                        <AdminStatusTag tone={toneFromStatus(detail.status)}>
                          {schedulerStatusLabel(detail.status)}
                        </AdminStatusTag>
                      ),
                    },
                    {
                      key: 'description',
                      label: '说明',
                      children: detail.description || '—',
                    },
                  ],
                },
                {
                  key: 'schedule',
                  title: '调度',
                  items: [
                    {
                      key: 'cron',
                      label: 'Cron',
                      children: detail.cron ? (
                        <Space size={4}>
                          <Typography.Text code>{detail.cron}</Typography.Text>
                          <Button type="link" size="small" onClick={() => openCronEdit(detail)}>
                            编辑
                          </Button>
                        </Space>
                      ) : (
                        '未设置（用间隔调度）'
                      ),
                    },
                    { key: 'interval', label: '间隔', children: formatMs(detail.intervalMs) },
                    {
                      key: 'lastRunAt',
                      label: '上次执行',
                      children: formatDateTime(detail.lastRunAt),
                    },
                    {
                      key: 'nextDueAt',
                      label: '下次执行',
                      children: formatDateTime(detail.nextDueAt),
                    },
                    { key: 'runCount', label: '累计执行次数', children: detail.runCount ?? 0 },
                  ],
                },
                {
                  key: 'error',
                  title: '最近错误',
                  items: [
                    {
                      key: 'lastError',
                      label: '最近错误',
                      children: detail.lastError ? (
                        <Space direction="vertical" size={2}>
                          <Typography.Text type="danger">
                            {moduleErrorLabel(detail.lastError)}
                          </Typography.Text>
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                            技术详情：{detail.lastError}
                          </Typography.Text>
                        </Space>
                      ) : (
                        '—'
                      ),
                    },
                  ],
                },
              ]
            : []
        }
      />

      <Modal
        title={`编辑 Cron · ${cronEdit?.name || ''}`}
        open={!!cronEdit}
        onCancel={() => setCronEdit(null)}
        onOk={() => void saveCron()}
        confirmLoading={cronSaving}
        destroyOnHidden
      >
        <Typography.Paragraph type="secondary" style={{ marginBottom: 12 }}>
          Spring 6 段表达式（秒 分 时 日 月
          周）。留空则按「运行时配置」中的间隔毫秒调度。时区取业务时区。
        </Typography.Paragraph>
        <Input
          value={cronDraft}
          placeholder="例如 0 */5 * * * *（每 5 分钟）"
          onChange={(e) => setCronDraft(e.target.value)}
        />
      </Modal>
    </AdminPage>
  );
}
