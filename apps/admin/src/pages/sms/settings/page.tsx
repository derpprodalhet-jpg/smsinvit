import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Popconfirm,
  Select,
  Space,
  Table,
  Tabs,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useState } from 'react';
import { SmsLineCheckStatus } from '../../../components/sms-line-check-status';
import { AdminPage } from '../../../components/admin-page';
import { adminApi } from '../../../lib/admin-api';
import { unknownLabel } from '../../../lib/zh-label';

type ExcludedArea = { prefix: string; detectionCount: number; lineType: string };
type Settings = {
  autoExcludedAreas?: ExcludedArea[];
  lineChecks?: { numlookupapi?: boolean; abstractapi?: boolean };
  apiKeyConfigured?: boolean;
  apiKeyMasked?: string | null;
  baseUrl?: string;
  publicApiBase?: string;
  webhookAllowedIps?: string;
  unavailableMessage?: string;
  pageIntro?: string;
  importantNotices?: string;
  minBalance?: number;
  webhookUrl?: string | null;
  webhookTokenMasked?: string | null;
};

type TabKey = 'upstream' | 'webhook' | 'balance' | 'copy' | 'prices';

type CountryRow = { id: number; chn: string; eng: string };
type ServiceRow = { code: string; name: string };
type ProviderRow = { providerId: string; count: number; priceUsd: number };
type PricesBoard = {
  service?: string;
  serviceName?: string;
  country?: number;
  countryName?: string;
  providers?: ProviderRow[];
  minCostUsd?: number;
  minCostCnyMinor?: number;
  totalCount?: number;
  usdCnyRate?: number;
  rateSource?: string;
  rateFetchedAt?: string;
};
type FxInfo = {
  usdCnyRate?: number;
  rateSource?: string;
  rateFetchedAt?: string;
  fallbackRate?: number;
};

export default function SmsSettingsPage() {
  const [form] = Form.useForm();
  const [loading, setLoading] = useState(false);
  // 每个按钮只在自己触发的请求进行时转圈：存的是动作 key，不是「有人在保存」。
  const [saving, setSavingAction] = useState<string | null>(null);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [publicApiBase, setPublicApiBase] = useState('');
  const [apiKeyConfigured, setApiKeyConfigured] = useState(false);
  const [apiKeyMasked, setApiKeyMasked] = useState<string | null>(null);
  const [autoExcludedAreas, setAutoExcludedAreas] = useState<ExcludedArea[]>([]);
  const [areaInput, setAreaInput] = useState('');
  const [areaBusy, setAreaBusy] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<TabKey>('upstream');

  const [countries, setCountries] = useState<CountryRow[]>([]);
  const [services, setServices] = useState<ServiceRow[]>([]);
  const [lineChecks, setLineChecks] = useState<Settings['lineChecks']>({});
  const [priceService, setPriceService] = useState<string | undefined>();
  const [priceCountry, setPriceCountry] = useState<number | undefined>();
  const [prices, setPrices] = useState<PricesBoard | null>(null);
  const [pricesLoading, setPricesLoading] = useState(false);
  const [fx, setFx] = useState<FxInfo | null>(null);
  const [fallbackDraft, setFallbackDraft] = useState('7.2');

  async function apply(data?: Settings | null) {
    setLineChecks(data?.lineChecks || {});
    form.setFieldsValue({
      apiKey: '',
      numlookupApiKey: '',
      abstractApiKey: '',
      baseUrl: data?.baseUrl || 'https://smsbower.page/stubs/handler_api.php',
      webhookAllowedIps: data?.webhookAllowedIps || '',
      unavailableMessage: data?.unavailableMessage || '服务器正在维护，请稍后再试',
      pageIntro: data?.pageIntro || '粘贴 CDK 或打开接码兑换链接，即可获取手机号与验证码。',
      importantNotices:
        data?.importantNotices ||
        [
          '请在 CDK 有效期内使用（从后台生成时间起算，以卡密套餐说明为准）。过期后无法兑换。',
          '第一封验证码到达后 CDK 即视为已使用；目标站重发的第二封码，需等通知后通过「收下一封验证码」查看。',
          '请勿分享或出售未使用的 CDK，泄露后他人可直接兑换。',
          '若提示「服务器正在维护」，请稍后再试，此时不会消耗您的换号次数。',
          '遇到问题请保留 CDK 与生成批次，通过客服联系处理。',
        ].join('\n'),
      minBalance: data?.minBalance ?? 1,
    });
    setWebhookUrl(data?.webhookUrl || null);
    setPublicApiBase(data?.publicApiBase || '');
    setApiKeyConfigured(Boolean(data?.apiKeyConfigured));
    setAutoExcludedAreas(data?.autoExcludedAreas || []);
    setApiKeyMasked(data?.apiKeyMasked || null);
  }

  async function load() {
    setLoading(true);
    try {
      const { data } = await adminApi<Settings>('/api/v1/admin/sms/settings');
      await apply(data);
    } catch (err) {
      message.error(err instanceof Error ? err.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }

  async function changeArea(prefix: string, remove = false) {
    if (areaBusy) return;
    if (!remove && !/^(?:\+?1[\s-]?)?[2-9][0-9]{2}$/.test(prefix.trim())) {
      message.error('请输入三位区号或 +1 xxx，例如 +1 832；不要填写完整号码。');
      return;
    }
    setAreaBusy(remove ? prefix : 'add');
    try {
      const { data } = await adminApi<ExcludedArea[]>(
        `/api/v1/admin/sms/excluded-areas${remove ? '/' + encodeURIComponent(prefix) : ''}`,
        { method: remove ? 'DELETE' : 'POST', ...(remove ? {} : { body: JSON.stringify({ prefix: prefix.trim() }) }) },
      );
      setAutoExcludedAreas(data || []);
      if (!remove) setAreaInput('');
      message.success(remove ? '已从排除池删除，后续取号不再使用这条池内规则' : '区号已加入排除池');
    } catch (err) {
      message.error(err instanceof Error ? err.message : '区号操作失败，请重试');
    } finally {
      setAreaBusy(null);
    }
  }

  const loadCatalogMeta = useCallback(async () => {
    try {
      const [svc, cty, fxRes] = await Promise.all([
        adminApi<ServiceRow[]>('/api/v1/admin/sms/catalog/services'),
        adminApi<CountryRow[]>('/api/v1/admin/sms/catalog/countries'),
        adminApi<FxInfo>('/api/v1/admin/sms/catalog/fx'),
      ]);
      setServices(svc.data || []);
      setCountries(cty.data || []);
      setFx(fxRes.data || null);
      if (fxRes.data?.fallbackRate != null) {
        setFallbackDraft(String(fxRes.data.fallbackRate));
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : '目录加载失败（请先配置 API Key）');
    }
  }, []);

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    if (activeTab === 'prices') void loadCatalogMeta();
  }, [activeTab, loadCatalogMeta]);

  async function onSave(opts?: { rotateToken?: boolean; clearApiKey?: boolean }) {
    // 清除 / 轮换 / 保存当前页是三个不同动作：只让点击的那一个按钮转圈。
    setSavingAction(
      opts?.clearApiKey ? 'clear-api-key' : opts?.rotateToken ? 'rotate-token' : 'save',
    );
    try {
      // Rotate / clear must not rewrite unrelated tabs — backend merges only non-null fields.
      if (opts?.rotateToken || opts?.clearApiKey) {
        const { data } = await adminApi<Settings>('/api/v1/admin/sms/settings', {
          method: 'POST',
          body: JSON.stringify({
            rotateWebhookToken: Boolean(opts?.rotateToken),
            clearApiKey: Boolean(opts?.clearApiKey),
          }),
        });
        await apply(data);
        message.success(opts?.clearApiKey ? '已清除 API Key' : '已轮换 Webhook Token');
        return;
      }

      let body: Record<string, unknown> = {};
      if (activeTab === 'upstream') {
        const values = await form.validateFields(['apiKey', 'baseUrl', 'numlookupApiKey', 'abstractApiKey']);
        body = {
          apiKey: values.apiKey || undefined,
          numlookupApiKey: values.numlookupApiKey || undefined,
          abstractApiKey: values.abstractApiKey || undefined,
          baseUrl: values.baseUrl,
        };
      } else if (activeTab === 'webhook') {
        const values = await form.validateFields(['webhookAllowedIps']);
        body = { webhookAllowedIps: values.webhookAllowedIps ?? '' };
      } else if (activeTab === 'balance') {
        const values = await form.validateFields(['minBalance']);
        body = { minBalance: values.minBalance };
      } else if (activeTab === 'copy') {
        const values = await form.validateFields([
          'unavailableMessage',
          'pageIntro',
          'importantNotices',
          'deliveryInstructions',
        ]);
        body = {
          unavailableMessage: values.unavailableMessage,
          pageIntro: values.pageIntro,
          importantNotices: values.importantNotices,
          deliveryInstructions: values.deliveryInstructions,
        };
      } else {
        message.info('当前页无需保存');
        return;
      }

      const { data } = await adminApi<Settings>('/api/v1/admin/sms/settings', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      await apply(data);
      message.success('已保存当前页配置');
    } catch (err) {
      if (err instanceof Error) message.error(err.message);
    } finally {
      setSavingAction(null);
    }
  }

  async function loadPrices() {
    if (!priceService || priceCountry == null) {
      message.warning('请先选择服务和国家');
      return;
    }
    setPricesLoading(true);
    try {
      const qs = new URLSearchParams({
        service: priceService,
        country: String(priceCountry),
      });
      const { data } = await adminApi<PricesBoard>(`/api/v1/admin/sms/catalog/prices?${qs}`);
      setPrices(data || null);
      if (data?.usdCnyRate != null) {
        setFx((prev) => ({
          ...prev,
          usdCnyRate: data.usdCnyRate,
          rateSource: data.rateSource,
          rateFetchedAt: data.rateFetchedAt,
        }));
      }
    } catch (err) {
      message.error(err instanceof Error ? err.message : '价目加载失败');
      setPrices(null);
    } finally {
      setPricesLoading(false);
    }
  }

  async function saveFallbackRate() {
    const n = Number(fallbackDraft);
    if (!Number.isFinite(n) || n <= 0 || n >= 100) {
      message.error('请输入合理的正数汇率（如 7.2）');
      return;
    }
    setSavingAction('save-fx-rate');
    try {
      await adminApi('/api/v1/admin/settings', {
        method: 'POST',
        body: JSON.stringify({
          key: 'runtime.sms_usd_cny_rate_fallback',
          category: 'sms',
          valueJson: { value: String(n) },
          isSecret: false,
        }),
      });
      message.success('兜底汇率已保存');
      await loadCatalogMeta();
    } catch (err) {
      message.error(err instanceof Error ? err.message : '保存失败');
    } finally {
      setSavingAction(null);
    }
  }

  return (
    <AdminPage
      title="接码配置"
      description="全部 SMSBower 配置保存在后台数据库（API Key 加密存储），无需写 .env。"
    >
      <Card bordered={false} loading={loading}>
        <Alert
          type="info"
          showIcon
          style={{ marginBottom: 16 }}
          message="公网 API 基址（运行时配置）"
          description={
            <Space direction="vertical" size={4}>
              <Typography.Text type="secondary">
                Webhook URL 使用运行时配置里的「公网 API 基址」：部署时由
                deploy/smsinvit/runtime.example.yaml 的 smsinvit.public-api-base 写入，也可通过管理
                API 更新同一项。
              </Typography.Text>
              <Typography.Text>
                当前基址：
                <Typography.Text code copyable={Boolean(publicApiBase)}>
                  {publicApiBase || '（未配置）'}
                </Typography.Text>
              </Typography.Text>
            </Space>
          }
        />
        <Form
          form={form}
          layout="vertical"
          style={{ maxWidth: activeTab === 'prices' ? 960 : 640 }}
        >
          <Tabs
            activeKey={activeTab}
            onChange={(key) => setActiveTab(key as TabKey)}
            style={{ marginBottom: 8 }}
            items={[
              {
                key: 'upstream',
                label: '上游连接',
                children: (
                  <>
                    <Typography.Paragraph type="secondary">
                      API Key 状态：
                      {apiKeyConfigured
                        ? `已配置（${apiKeyMasked || '****'}）`
                        : '未配置 — 请在下方填写并保存'}
                    </Typography.Paragraph>
                    <Form.Item
                      name="apiKey"
                      label="SMSBower API Key"
                      extra="留空表示不修改；填写后加密保存。勿写入环境变量。"
                    >
                      <Input.Password
                        placeholder={apiKeyConfigured ? '已配置，输入新值可覆盖' : '粘贴 API Key'}
                      />
                    </Form.Item>
                    <Form.Item name="baseUrl" label="API Base URL" rules={[{ required: true }]}>
                      <Input placeholder="https://smsbower.page/stubs/handler_api.php" />
                    </Form.Item>
                    <Alert type="info" showIcon message="号码线路检测（免费额度）"
                      description="优先使用 numlookupapi，额度耗尽或服务不可用时切换 Abstract API；均不可用时跳过检测，继续取号。明确返回非 mobile 的号码仍会拦截；确认 VoIP 才自动排除整个 +1 区号。不自动升级付费套餐。" />
                    <SmsLineCheckStatus />
                    <Form.Item name="numlookupApiKey" label="numlookupapi API Key" extra="留空保留，填写后加密保存。">
                      <Input.Password autoComplete="new-password" placeholder={lineChecks?.numlookupapi ? '已配置，输入新值可覆盖' : '尚未配置'} />
                    </Form.Item>
                    <Form.Item name="abstractApiKey" label="Abstract Phone Validation API Key" extra="需使用 Phone Validation 产品密钥，留空保留。">
                      <Input.Password autoComplete="new-password" placeholder={lineChecks?.abstractapi ? '已配置，输入新值可覆盖' : '尚未配置'} />
                    </Form.Item>
                    <Alert type="info" showIcon message="区号排除池"
                      description="按 +1 xxx 区号排除号码，支持手动添加、删除。删除仅移除池内规则，套餐和历史号码的排除规则仍有效。可用检测服务确认 VoIP 后会自动加入此池。" />
                    <Space wrap style={{ margin: '12px 0' }}>
                      <Input aria-label="添加排除区号" placeholder="例如 +1 832 或 832" maxLength={32}
                        value={areaInput} disabled={Boolean(areaBusy)} onChange={(e) => setAreaInput(e.target.value)}
                        onPressEnter={(e) => { e.preventDefault(); void changeArea(areaInput); }} />
                      <Button aria-label="添加区号" loading={areaBusy === 'add'} disabled={Boolean(areaBusy) || !areaInput.trim()}
                        onClick={() => void changeArea(areaInput)}>添加区号</Button>
                      <Button disabled={Boolean(areaBusy)} loading={areaBusy === 'refresh'} onClick={async () => {
                        setAreaBusy('refresh');
                        try {
                          const { data } = await adminApi<ExcludedArea[]>('/api/v1/admin/sms/excluded-areas');
                          setAutoExcludedAreas(data || []);
                        } catch (err) { message.error(err instanceof Error ? err.message : '刷新失败'); }
                        finally { setAreaBusy(null); }
                      }}>刷新排除池</Button>
                    </Space>
                    <Table<ExcludedArea> rowKey="prefix" size="small" dataSource={autoExcludedAreas}
                      pagination={{ pageSize: 10, showSizeChanger: false }} locale={{ emptyText: '暂无排除区号' }}
                      columns={[
                        { title: '区号', dataIndex: 'prefix', render: (value: string) => `+1 ${value.slice(1)}` },
                        { title: '来源', dataIndex: 'lineType', render: (value: string) => value === 'manual' ? '手动添加' : ['voip', 'fixedVoip', 'nonFixedVoip'].includes(value) ? 'VoIP 自动' : unknownLabel(value) },
                        { title: '检测次数', dataIndex: 'detectionCount' },
                        { title: '操作', render: (_, row) => (
                          <Popconfirm title={`删除 +1 ${row.prefix.slice(1)}？`} description="删除后该区号不再受本排除池限制，套餐和历史号码规则仍有效。"
                            okText="确认删除" cancelText="取消" onConfirm={() => changeArea(row.prefix, true)}>
                            <Button danger size="small" disabled={Boolean(areaBusy)} loading={areaBusy === row.prefix}
                              aria-label={`删除 +1 ${row.prefix.slice(1)}`}>删除</Button>
                          </Popconfirm>
                        ) },
                      ]} />
                    <Button
                      danger
                      loading={saving === 'clear-api-key'}
                      onClick={() => void onSave({ clearApiKey: true })}
                    >
                      清除 API Key
                    </Button>
                  </>
                ),
              },
              {
                key: 'webhook',
                label: 'Webhook',
                children: (
                  <>
                    <Form.Item
                      name="webhookAllowedIps"
                      label="Webhook 允许 IP"
                      extra="逗号分隔；留空表示不校验来源 IP（生产建议填写 SMSBower 文档公布的 IP）"
                    >
                      <Input.TextArea rows={2} placeholder="1.2.3.4,5.6.7.8" />
                    </Form.Item>
                    <Form.Item label="Webhook URL（粘贴到 SMSBower 个人中心）">
                      <Input.TextArea
                        rows={2}
                        readOnly
                        value={
                          webhookUrl || '请先在运行时配置中填写公网 API 基址，并保存/轮换 Token'
                        }
                      />
                    </Form.Item>
                    <Button
                      loading={saving === 'rotate-token'}
                      onClick={() => void onSave({ rotateToken: true })}
                    >
                      轮换 Webhook Token
                    </Button>
                  </>
                ),
              },
              {
                key: 'balance',
                label: '余额熔断',
                children: (
                  <Form.Item name="minBalance" label="最低余额熔断阈值">
                    <InputNumber min={0} step={0.01} style={{ width: '100%' }} />
                  </Form.Item>
                ),
              },
              {
                key: 'prices',
                label: '价格一览',
                children: (
                  <Space direction="vertical" size={16} style={{ width: '100%' }}>
                    <Alert
                      type="info"
                      showIcon
                      message={
                        fx
                          ? `当前汇率 1 USD = ${Number(fx.usdCnyRate).toFixed(4)} CNY（${fx.rateSource || '—'}）${
                              fx.rateFetchedAt ? ` · ${fx.rateFetchedAt}` : ''
                            }`
                          : '加载汇率中…'
                      }
                      description={
                        <Space wrap align="center">
                          <Typography.Text type="secondary">
                            兜底汇率（实时接口失败时用）
                          </Typography.Text>
                          <InputNumber
                            min={0.1}
                            max={99}
                            step={0.01}
                            value={Number(fallbackDraft) || undefined}
                            onChange={(v) => setFallbackDraft(v == null ? '' : String(v))}
                          />
                          <Button
                            loading={saving === 'save-fx-rate'}
                            onClick={() => void saveFallbackRate()}
                          >
                            保存兜底汇率
                          </Button>
                          <Typography.Text type="secondary">
                            汇率来源属运行时配置，由部署或管理 API 维护。
                          </Typography.Text>
                        </Space>
                      }
                    />
                    <Space wrap>
                      <Select
                        showSearch
                        optionFilterProp="label"
                        style={{ minWidth: 220 }}
                        placeholder="服务"
                        value={priceService}
                        options={services.map((s) => ({
                          value: s.code,
                          label: s.name || unknownLabel(s.code),
                        }))}
                        onChange={(v) => setPriceService(v)}
                      />
                      <Select
                        showSearch
                        optionFilterProp="label"
                        style={{ minWidth: 220 }}
                        placeholder="国家"
                        value={priceCountry}
                        options={countries.map((c) => ({
                          value: c.id,
                          label: `${c.chn || c.eng || c.id}（${c.id}）`,
                        }))}
                        onChange={(v) => setPriceCountry(v)}
                      />
                      <Button
                        type="primary"
                        loading={pricesLoading}
                        onClick={() => void loadPrices()}
                      >
                        查询价目
                      </Button>
                    </Space>
                    {prices ? (
                      <>
                        <Typography.Text type="secondary">
                          {prices.serviceName || unknownLabel(prices.service)} ·{' '}
                          {prices.countryName ||
                            (prices.country != null ? unknownLabel(String(prices.country)) : '—')}
                          {prices.minCostUsd != null
                            ? ` · 最低 $${Number(prices.minCostUsd).toFixed(4)}`
                            : ''}
                          {prices.minCostCnyMinor != null
                            ? ` ≈ ¥${(prices.minCostCnyMinor / 100).toFixed(2)}`
                            : ''}
                          {prices.totalCount != null ? ` · 库存合计 ${prices.totalCount}` : ''}
                        </Typography.Text>
                        <Table
                          size="small"
                          rowKey="providerId"
                          pagination={{ pageSize: 20 }}
                          dataSource={prices.providers || []}
                          columns={[
                            { title: '供应商 ID', dataIndex: 'providerId' },
                            { title: '库存', dataIndex: 'count', width: 100 },
                            {
                              title: '价格 (USD)',
                              dataIndex: 'priceUsd',
                              width: 120,
                              render: (v: number) => Number(v).toFixed(4),
                            },
                          ]}
                        />
                      </>
                    ) : null}
                  </Space>
                ),
              },
              {
                key: 'copy',
                label: '页面文案',
                children: (
                  <>
                    <Form.Item
                      name="unavailableMessage"
                      label="无号 / 维护提示文案"
                      rules={[{ required: true, message: '请填写提示文案' }]}
                    >
                      <Input.TextArea rows={2} />
                    </Form.Item>
                    <Form.Item
                      name="pageIntro"
                      label="接码页顶部说明"
                      extra="显示在「灵卡接码」标题下方，类似个人中心副标题位置"
                      rules={[{ required: true, message: '请填写顶部说明' }]}
                    >
                      <Input.TextArea rows={2} />
                    </Form.Item>
                    <Form.Item
                      name="importantNotices"
                      label="重要提示"
                      extra="每行一条，显示在接码页底部「重要提示」列表"
                      rules={[{ required: true, message: '请填写重要提示' }]}
                    >
                      <Input.TextArea rows={8} placeholder="每行一条提示" />
                    </Form.Item>
                  </>
                ),
              },
            ]}
          />

          {activeTab !== 'prices' ? (
            <Button
              type="primary"
              loading={saving === 'save'}
              onClick={() => void onSave()}
              style={{ marginTop: 8 }}
            >
              保存当前页
            </Button>
          ) : null}
        </Form>
      </Card>
    </AdminPage>
  );
}
