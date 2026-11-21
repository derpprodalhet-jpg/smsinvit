import { ReloadOutlined, StopOutlined } from '@ant-design/icons';
import { Alert, Button, Popconfirm, Space, Table, Tag, Typography, message } from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { useCallback, useEffect, useState } from 'react';
import { AdminPage } from '../../../components/admin-page';
import { adminApi } from '../../../lib/admin-api';
import { formatDateTime } from '../../../lib/format-datetime';

type IpBanRow = {
  ipHash: string;
  ipAddress: string;
  failureCount: number;
  lastFailureAt: string | null;
  lastIdentifier: string | null;
  banned: boolean;
  secondsRemaining: number;
  bannedUntil: string | null;
  banReason: string | null;
};

type IpBanPayload = {
  items: IpBanRow[];
  failureLimit: number;
  banHours: number;
  windowHours: number;
};

/** 剩余时长按 bannedUntil 现算，避免用响应里的秒数在页面上「冻住」。 */
function remainingText(row: IpBanRow) {
  if (!row.banned || !row.bannedUntil) return '—';
  const ms = new Date(row.bannedUntil).getTime() - Date.now();
  if (!Number.isFinite(ms) || ms <= 0) return '即将解除';
  const minutes = Math.ceil(ms / 60000);
  if (minutes < 60) return `约 ${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  return `约 ${hours} 小时 ${minutes % 60} 分钟`;
}

export default function AdminIpBansPage() {
  const [rows, setRows] = useState<IpBanRow[]>([]);
  const [rule, setRule] = useState({ failureLimit: 5, banHours: 24 });
  const [loading, setLoading] = useState(false);
  const [unbanning, setUnbanning] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await adminApi<IpBanPayload>('/api/v1/admin/security/ip-bans?limit=200');
      setRows(res.data?.items || []);
      setRule({
        failureLimit: res.data?.failureLimit ?? 5,
        banHours: res.data?.banHours ?? 24,
      });
    } catch (err) {
      message.error(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function unban(row: IpBanRow) {
    setUnbanning(row.ipHash);
    try {
      await adminApi('/api/v1/admin/security/ip-bans/unban', {
        method: 'POST',
        body: JSON.stringify({ ipHash: row.ipHash }),
      });
      message.success(`已解除 ${row.ipAddress} 的封禁`);
      await load();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '解封失败');
    } finally {
      setUnbanning(null);
    }
  }

  const columns: ColumnsType<IpBanRow> = [
    {
      title: '来源 IP',
      dataIndex: 'ipAddress',
      render: (_, row) => <Typography.Text code>{row.ipAddress}</Typography.Text>,
    },
    {
      title: '状态',
      dataIndex: 'banned',
      width: 110,
      render: (_, row) =>
        row.banned ? <Tag color="error">已封禁</Tag> : <Tag color="warning">失败记录</Tag>,
    },
    {
      title: '密码错误次数',
      dataIndex: 'failureCount',
      width: 130,
      render: (_, row) => (
        <Typography.Text type={row.failureCount >= rule.failureLimit ? 'danger' : undefined}>
          {row.failureCount}
        </Typography.Text>
      ),
    },
    {
      title: '最后失败时间',
      dataIndex: 'lastFailureAt',
      width: 180,
      render: (_, row) => (
        <Typography.Text type="secondary" style={{ fontSize: 12, whiteSpace: 'nowrap' }}>
          {row.lastFailureAt ? formatDateTime(row.lastFailureAt) : '—'}
        </Typography.Text>
      ),
    },
    {
      title: '最后尝试账号',
      dataIndex: 'lastIdentifier',
      render: (_, row) => row.lastIdentifier || '—',
    },
    {
      title: '封禁到期',
      dataIndex: 'bannedUntil',
      width: 200,
      render: (_, row) =>
        row.banned && row.bannedUntil ? (
          <Space direction="vertical" size={0}>
            <Typography.Text style={{ fontSize: 12 }}>
              {formatDateTime(row.bannedUntil)}
            </Typography.Text>
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              剩余 {remainingText(row)}
            </Typography.Text>
          </Space>
        ) : (
          '—'
        ),
    },
    {
      title: '操作',
      key: 'action',
      width: 100,
      render: (_, row) => (
        <Popconfirm
          title="解除该 IP 的封禁？"
          description="失败次数与封禁记录会一并清除。"
          okText="确认解封"
          cancelText="取消"
          onConfirm={() => unban(row)}
        >
          <Button
            type="link"
            size="small"
            danger
            icon={<StopOutlined />}
            loading={unbanning === row.ipHash}
          >
            解封
          </Button>
        </Popconfirm>
      ),
    },
  ];

  return (
    <AdminPage
      title="IP 封禁"
      description={`同一 IP 连续 ${rule.failureLimit} 次密码错误即封禁 ${rule.banHours} 小时，作用于整个后台；登录成功会清空计数。`}
      extra={
        <Button icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
          刷新
        </Button>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="仅密码错误计入次数"
        description="验证码错误、动态验证码错误与频率限制不计入；在服务器上误封了真实管理员时，也可在此直接解封。"
      />
      <Table<IpBanRow>
        rowKey="ipHash"
        columns={columns}
        dataSource={rows}
        loading={loading}
        size="middle"
        pagination={{ defaultPageSize: 20, showSizeChanger: true }}
        locale={{ emptyText: '暂无失败记录或封禁 IP' }}
      />
    </AdminPage>
  );
}
