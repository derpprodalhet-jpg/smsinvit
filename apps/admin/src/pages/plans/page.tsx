import { useEffect, useRef, useState } from 'react';
import { ProTable, type ActionType, type ProColumns } from '@ant-design/pro-components';
import {
  Button,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Tag,
  Typography,
  message,
} from 'antd';
import { AdminPage } from '../../components/admin-page';
import { AdminDetailDrawer, type DetailGroup } from '../../components/admin-detail-drawer';
import { adminApi } from '../../lib/admin-api';
import { Link } from '../../lib/navigation';
import type { Plan } from '../../lib/redeem-inventory';
import { PRODUCT_KIND_LABEL } from '../../lib/invitation';
import { resolveLabel, unknownLabel, yesNoLabel } from '../../lib/zh-label';
import { formatDateTime } from '../../lib/format-datetime';
import { localizedText } from '../../lib/zh-text';

const KINDS = [
  { value: 'SMS_REDEEM', label: '接码' },
  { value: 'CODEX_INVITE_REDEEM', label: 'Codex 邀请' },
];
/** 套餐属性键 → 中文；详情里按字段展示快照，未知键显示「字段（key）」。 */
const PLAN_FIELD_LABEL: Record<string, string> = {
  productKind: '兑换服务',
  cdkValidDays: '卡密有效天数',
  smsService: '接码服务',
  smsServiceName: '接码服务名称',
  smsCountry: '号码国家',
  smsCountryName: '号码国家名称',
  smsMinPrice: '最低号码价格（USD）',
  smsMaxPrice: '最高号码价格（USD）',
  smsMinCount: '候选供应商最少库存',
  smsPhoneException: '排除的号码前缀',
  invitationPreferences: '邀请偏好',
};
const planFieldLabel = (key: string) => PLAN_FIELD_LABEL[key] || `字段（${key}）`;

/** 属性值按字段类型中文化；未确诊的枚举走 unknownLabel，避免漏出裸英文。 */
function planFieldValue(key: string, value: unknown, services: { code: string; name: string }[]) {
  if (value === undefined || value === null || value === '') return '—';
  if (key === 'productKind') return resolveLabel(PRODUCT_KIND_LABEL, String(value));
  if (key === 'smsService') {
    const matched = services.find((item) => item.code === String(value));
    return matched?.name ? `${matched.name}（${matched.code}）` : unknownLabel(String(value));
  }
  if (key === 'cdkValidDays') return `${String(value)} 天（生成起算）`;
  if (typeof value === 'boolean') return yesNoLabel(value);
  if (typeof value === 'object') return JSON.stringify(value);
  return localizedText(String(value));
}

export default function PlansPage() {
  const action = useRef<ActionType>();
  const [form] = Form.useForm();
  const [editing, setEditing] = useState<Plan | null>();
  const [detail, setDetail] = useState<Plan | null>(null);
  const [saving, setSaving] = useState(false);
  const [serviceFilter, setServiceFilter] = useState<string | undefined>();
  const kind = Form.useWatch('productKind', form);
  const [services, setServices] = useState<{ code: string; name: string }[]>([]);
  const [countries, setCountries] = useState<{ id: number; chn: string; eng: string }[]>([]);
  useEffect(() => {
    void Promise.allSettled([
      adminApi<typeof services>('/api/v1/admin/sms/catalog/services'),
      adminApi<typeof countries>('/api/v1/admin/sms/catalog/countries'),
    ]).then(([s, c]) => {
      if (s.status === 'fulfilled') setServices(s.value.data || []);
      if (c.status === 'fulfilled') setCountries(c.value.data || []);
    });
  }, []);
  function edit(plan?: Plan) {
    setEditing(plan || null);
    form.resetFields();
    form.setFieldsValue(
      plan
        ? { code: plan.code, name: plan.name, ...plan.attributesJson }
        : {
            productKind: 'SMS_REDEEM',
            cdkValidDays: 15,
            smsMinPrice: 0,
            smsMinCount: 20,
            invitationPreferences: {},
          },
    );
  }
  async function save() {
    try {
      const values = await form.validateFields();
      setSaving(true);
      const { code, name, ...fields } = values;
      const attrs = { ...(editing?.attributesJson || {}) };
      const sms = [
        'smsService',
        'smsCountry',
        'smsMinPrice',
        'smsMaxPrice',
        'smsMinCount',
        'smsPhoneException',
      ];
      if (fields.productKind !== 'SMS_REDEEM') for (const k of sms) delete attrs[k];
      if (fields.productKind !== 'CODEX_INVITE_REDEEM') delete attrs.invitationPreferences;
      for (const [k, v] of Object.entries(fields)) {
        if (v === undefined || v === '') delete attrs[k];
        else attrs[k] = v;
      }
      await adminApi(editing ? `/api/v1/admin/plans/${editing.id}` : '/api/v1/admin/plans', {
        method: editing ? 'PATCH' : 'POST',
        body: JSON.stringify(
          editing ? { name, attributesJson: attrs } : { code, name, attributesJson: attrs },
        ),
      });
      message.success('套餐已保存；已生成卡密保持生成时的配置');
      setEditing(undefined);
      action.current?.reload();
    } catch (e) {
      if (e instanceof Error && !(e as { errorFields?: unknown }).errorFields)
        message.error(e.message);
    } finally {
      setSaving(false);
    }
  }
  /** 一句话摘要：列表不再显示长配置，只在详情里展开。 */
  function configSummary(plan: Plan) {
    const attrs = plan.attributesJson || {};
    if (attrs.productKind === 'SMS_REDEEM')
      return `${attrs.smsServiceName || attrs.smsService || '—'} · ${attrs.smsCountryName || attrs.smsCountry || '—'}`;
    if (attrs.productKind === 'CODEX_INVITE_REDEEM') return '邀请专用配置';
    return '—';
  }
  const columns: ProColumns<Plan>[] = [
    { title: '名称', dataIndex: 'name', search: false },
    { title: '套餐编号', dataIndex: 'code', search: false },
    {
      title: '服务',
      search: false,
      render: (_, r) => resolveLabel(PRODUCT_KIND_LABEL, String(r.attributesJson.productKind)),
    },
    {
      title: '有效期',
      search: false,
      render: (_, r) => `${r.attributesJson.cdkValidDays ?? 15} 天（生成起算）`,
    },
    {
      title: '状态',
      dataIndex: 'status',
      search: false,
      render: (_, r) =>
        r.status === 'ACTIVE' ? (
          <Tag color="green">启用</Tag>
        ) : (
          <Tag>{r.status === 'DISABLED' ? '停用' : unknownLabel(r.status)}</Tag>
        ),
    },
    {
      title: '操作',
      valueType: 'option',
      width: 260,
      render: (_, r) => (
        <Space wrap size={0}>
          <Button type="link" size="small" onClick={() => setDetail(r)}>
            详情
          </Button>
          <Button type="link" size="small" onClick={() => edit(r)}>
            编辑
          </Button>
          <Link href={`/batches?skuId=${r.id}`}>生成卡密</Link>
          <Popconfirm
            title={r.status === 'ACTIVE' ? '停用套餐？' : '启用套餐？'}
            onConfirm={async () => {
              await adminApi(
                `/api/v1/admin/plans/${r.id}/${r.status === 'ACTIVE' ? 'disable' : 'enable'}`,
                { method: 'POST' },
              );
              action.current?.reload();
            }}
          >
            <Button type="link" size="small">
              {r.status === 'ACTIVE' ? '停用' : '启用'}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];
  const detailAttrs = detail ? Object.entries(detail.attributesJson || {}) : [];
  const detailGroups: DetailGroup[] = detail
    ? [
        {
          key: 'config',
          title: '配置明细',
          items: [
            { key: 'summary', label: '配置摘要', children: configSummary(detail) },
            ...detailAttrs.map(([key, value]) => ({
              key,
              label: planFieldLabel(key),
              children: planFieldValue(key, value, services),
            })),
          ],
        },
        {
          key: 'lifecycle',
          title: '上下架信息',
          items: [
            {
              key: 'status',
              label: '状态',
              children: detail.status === 'ACTIVE' ? '启用' : unknownLabel(detail.status),
            },
            { key: 'code', label: '套餐编号', children: detail.code },
            { key: 'id', label: '套餐 ID', children: detail.id },
            {
              key: 'createdAt',
              label: '创建时间',
              children: detail.createdAt ? formatDateTime(detail.createdAt) : '—',
            },
            {
              key: 'updatedAt',
              label: '更新时间',
              children: detail.updatedAt ? formatDateTime(detail.updatedAt) : '—',
            },
          ],
        },
      ]
    : [];
  return (
    <AdminPage
      title="套餐管理"
      description="配置接码、Codex 邀请；生成时保存配置快照，后续编辑不会改变已有卡密。"
    >
      <ProTable<Plan>
        rowKey="id"
        actionRef={action}
        columns={columns}
        search={false}
        pagination={{ defaultPageSize: 20 }}
        params={{ productKind: serviceFilter }}
        toolBarRender={() => [
          <Select
            key="service"
            aria-label="按兑换服务筛选"
            allowClear
            placeholder="全部兑换服务"
            style={{ width: 180 }}
            value={serviceFilter}
            options={KINDS}
            onChange={setServiceFilter}
          />,
          <Button key="new" type="primary" onClick={() => edit()}>
            新增套餐
          </Button>,
        ]}
        request={async (p) => {
          const query = new URLSearchParams({
            page: String(p.current || 1),
            pageSize: String(p.pageSize || 20),
          });
          if (serviceFilter) query.set('productKind', serviceFilter);
          const r = await adminApi<Plan[]>(`/api/v1/admin/plans?${query}`);
          return { data: r.data, total: r.meta?.total || 0, success: true };
        }}
      />
      <AdminDetailDrawer
        title={`套餐详情 · ${detail?.name || ''}`}
        subtitle={detail ? `${detail.code} · ${configSummary(detail)}` : undefined}
        open={!!detail}
        onClose={() => setDetail(null)}
        groups={detailGroups}
      >
        <Typography.Paragraph type="secondary" style={{ margin: 0 }}>
          生成卡密时会保存当时的配置快照；这里修改配置不会改变已生成的卡密。
        </Typography.Paragraph>
      </AdminDetailDrawer>
      <Modal
        title={editing ? '编辑套餐' : '新增套餐'}
        open={editing !== undefined}
        onCancel={() => setEditing(undefined)}
        onOk={() => void save()}
        confirmLoading={saving}
        width={680}
        destroyOnClose
      >
        <Form form={form} layout="vertical" preserve={false}>
          <Form.Item name="code" label="套餐编号" rules={[{ required: true }]}>
            <Input disabled={!!editing} maxLength={100} />
          </Form.Item>
          <Form.Item name="name" label="套餐名称" rules={[{ required: true }]}>
            <Input maxLength={255} />
          </Form.Item>
          <Form.Item name="productKind" label="兑换服务" rules={[{ required: true }]}>
            <Select options={KINDS} disabled={!!editing} />
          </Form.Item>
          <Form.Item
            name="cdkValidDays"
            label="卡密有效天数（从生成时间起算）"
            rules={[{ required: true }]}
          >
            <InputNumber min={1} max={3650} />
          </Form.Item>
          {kind === 'SMS_REDEEM' ? (
            <>
              <Form.Item name="smsService" label="接码服务" rules={[{ required: true }]}>
                <Select
                  showSearch
                  optionFilterProp="label"
                  options={services.map((s) => ({ value: s.code, label: `${s.name} (${s.code})` }))}
                />
              </Form.Item>
              <Form.Item name="smsCountry" label="号码国家" rules={[{ required: true }]}>
                <Select
                  showSearch
                  optionFilterProp="label"
                  options={countries.map((c) => ({
                    value: c.id,
                    label: `${c.chn || c.eng} (${c.id})`,
                  }))}
                />
              </Form.Item>
              <Space align="start">
                <Form.Item
                  name="smsMinPrice"
                  label="最低号码价格（USD）"
                  rules={[{ required: true }]}
                >
                  <InputNumber min={0} precision={4} />
                </Form.Item>
                <Form.Item
                  name="smsMaxPrice"
                  label="最高号码价格（USD）"
                  rules={[{ required: true }]}
                >
                  <InputNumber min={0.0001} precision={4} />
                </Form.Item>
                <Form.Item
                  name="smsMinCount"
                  label="候选供应商最少库存"
                  rules={[{ required: true }]}
                >
                  <InputNumber min={1} />
                </Form.Item>
              </Space>
              <Form.Item name="smsPhoneException" label="排除的号码前缀"
                extra="填写含国家码的号码前缀，多个用逗号分隔；不要填写完整手机号。例如 1800 表示 +1 800 免费电话号段。区号不能判断是否为虚拟号；只排除已确认不适用的号段。换号会自动排除之前号码前缀，仅对新发卡密生效。">
                <Input placeholder="例如：1832,1925" />
              </Form.Item>
            </>
          ) : kind === 'CODEX_INVITE_REDEEM' ? (
            <Typography.Paragraph type="secondary">
              邀请任务使用邀请后台中的独立邮箱、代理及执行配置。这里登记兑换资格与卡密有效期。
            </Typography.Paragraph>
          ) : null}
        </Form>
      </Modal>
    </AdminPage>
  );
}
