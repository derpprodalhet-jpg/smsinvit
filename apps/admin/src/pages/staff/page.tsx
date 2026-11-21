import { useRef, useState } from 'react';
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components';
import { Button, Form, Input, Modal, Popconfirm, Space, Tag, message } from 'antd';
import { AdminPage } from '../../components/admin-page';
import { adminApi } from '../../lib/admin-api';
import { useAdminAuth } from '../../lib/admin-auth-context';
import { resolveLabel } from '../../lib/zh-label';
import { staffRoleLabel } from '../../lib/zh-maps';
type Staff = {
  id: string;
  username: string;
  email: string;
  displayName?: string;
  status: string;
  roles: string[];
  lastLoginAt?: string;
  createdAt: string;
};
/** 管理员状态：未命中一律走「未知（原始值）」，绝不把英文状态显示出来。 */
const STAFF_STATUS_LABEL: Record<string, string> = {
  ACTIVE: '正常',
  DISABLED: '停用',
  BANNED: '封禁',
};
const rank = (roles: string[]) =>
  roles.includes('SUPER_ADMIN') ? 2 : roles.includes('ADMIN') ? 1 : 0;
export default function StaffPage() {
  const { user } = useAdminAuth();
  const action = useRef<ActionType>();
  const [form] = Form.useForm();
  const [create, setCreate] = useState(false);
  const [reset, setReset] = useState<Staff | null>(null);
  const [password, setPassword] = useState('');
  const [confirmation, setConfirmation] = useState('');
  const [saving, setSaving] = useState(false);
  async function submit() {
    setSaving(true);
    try {
      if (reset) {
        if (password.length < 8 || password !== confirmation) {
          message.warning('密码至少8位，且两次输入一致');
          return;
        }
        await adminApi(`/api/v1/admin/staff/${reset.id}/reset-password`, {
          method: 'POST',
          body: JSON.stringify({ newPassword: password }),
        });
        setReset(null);
        setPassword('');
        setConfirmation('');
      } else {
        const v = await form.validateFields();
        await adminApi('/api/v1/admin/staff', { method: 'POST', body: JSON.stringify(v) });
        setCreate(false);
        form.resetFields();
      }
      message.success('管理员信息已保存');
      action.current?.reload();
    } catch (e) {
      if (e instanceof Error) message.error(e.message);
    } finally {
      setSaving(false);
    }
  }
  const columns: ProColumns<Staff>[] = [
    { title: '用户名', dataIndex: 'username', search: false },
    { title: '邮箱', dataIndex: 'email', search: false },
    { title: '显示名称', dataIndex: 'displayName', search: false },
    {
      title: '角色',
      search: false,
      render: (_, r) =>
        (r.roles || []).map((x) => <Tag key={x}>{staffRoleLabel(x)}</Tag>),
    },
    {
      title: '状态',
      dataIndex: 'status',
      valueEnum: Object.fromEntries(
        Object.entries(STAFF_STATUS_LABEL).map(([value, text]) => [value, { text }]),
      ),
      render: (_, r) => resolveLabel(STAFF_STATUS_LABEL, r.status),
    },
    { title: '最近登录', dataIndex: 'lastLoginAt', valueType: 'dateTime', search: false },
    {
      title: '操作',
      valueType: 'option',
      render: (_, r) => {
        const own = r.id === user?.id;
        const superior = !!user && rank(user.roles) > rank(r.roles || []);
        return (
          <Space>
            {(own || superior) && (
              <Button
                type="link"
                onClick={() => {
                  setReset(r);
                  setPassword('');
                  setConfirmation('');
                }}
              >
                重置密码
              </Button>
            )}
            {superior && !own && (
              <>
                <Popconfirm
                  title={r.status === 'ACTIVE' ? '停用该管理员？' : '启用该管理员？'}
                  onConfirm={async () => {
                    await adminApi(`/api/v1/admin/staff/${r.id}/status`, {
                      method: 'PATCH',
                      body: JSON.stringify({
                        status: r.status === 'ACTIVE' ? 'DISABLED' : 'ACTIVE',
                      }),
                    });
                    action.current?.reload();
                  }}
                >
                  <Button type="link">{r.status === 'ACTIVE' ? '停用' : '启用'}</Button>
                </Popconfirm>
                <Popconfirm
                  title="删除该管理员？"
                  description="该账号将无法再登录。"
                  onConfirm={async () => {
                    await adminApi(`/api/v1/admin/staff/${r.id}`, { method: 'DELETE' });
                    action.current?.reload();
                  }}
                >
                  <Button type="link" danger>
                    删除
                  </Button>
                </Popconfirm>
              </>
            )}
          </Space>
        );
      },
    },
  ];
  return (
    <AdminPage
      title="管理员"
      description="沿用原有管理员权限；超级管理员可创建普通管理员，账号安全设置由本人管理。"
    >
      <ProTable<Staff>
        rowKey="id"
        actionRef={action}
        columns={columns}
        pagination={{ defaultPageSize: 20 }}
        toolBarRender={() =>
          user?.roles.includes('SUPER_ADMIN')
            ? [
                <Button key="create" type="primary" onClick={() => setCreate(true)}>
                  新增管理员
                </Button>,
              ]
            : []
        }
        request={async (p) => {
          const q = new URLSearchParams({
            page: String(p.current || 1),
            pageSize: String(p.pageSize || 20),
          });
          if (p.status) q.set('status', String(p.status));
          const r = await adminApi<Staff[]>(`/api/v1/admin/staff?${q}`);
          return { data: r.data, total: r.meta?.total || 0, success: true };
        }}
      />
      <Modal
        title="新增管理员"
        open={create}
        onCancel={() => setCreate(false)}
        onOk={() => void submit()}
        confirmLoading={saving}
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="username"
            label="用户名"
            rules={[
              { required: true },
              { pattern: /^[a-zA-Z0-9_-]{3,30}$/, message: '3至30位字母、数字、下划线或连字符' },
            ]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="email" label="邮箱" rules={[{ required: true }, { type: 'email' }]}>
            <Input />
          </Form.Item>
          <Form.Item name="displayName" label="显示名称">
            <Input maxLength={100} />
          </Form.Item>
          <Form.Item
            name="password"
            label="初始密码"
            rules={[{ required: true, min: 8, max: 128 }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        title={`重置密码 · ${reset?.username}`}
        open={!!reset}
        onCancel={() => {
          setReset(null);
          setPassword('');
          setConfirmation('');
        }}
        onOk={() => void submit()}
        confirmLoading={saving}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Input.Password
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="新密码（至少8位）"
          />
          <Input.Password
            autoComplete="new-password"
            value={confirmation}
            onChange={(e) => setConfirmation(e.target.value)}
            placeholder="再次输入新密码"
          />
        </Space>
      </Modal>
    </AdminPage>
  );
}
