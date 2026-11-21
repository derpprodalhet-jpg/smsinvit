import {
  Alert,
  Button,
  Card,
  Col,
  Form,
  Input,
  InputNumber,
  Modal,
  Row,
  Select,
  Slider,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  invitationAction,
  invitationApi,
  invitationErrorMessage,
  invitationTime,
} from '../../../lib/invitation-api';
import { useInvitationQuery } from '../../../lib/use-invitation-query';
import { Link } from '../../../lib/navigation';
import { unknownLabel } from '../../../lib/zh-label';
import { localizedText } from '../../../lib/zh-text';
import {
  changedConfigPayload,
  configDraft,
  EXPORT_GROUPS,
  INVITE_GROUPS,
  isInvitationMask,
  POOL_GROUPS,
  SMS_GROUPS,
  type ConfigField,
  type InvitationConfig,
} from './config-values';
import {
  RuntimeDetails,
  SmsBalanceDetails,
  SmsSelftestDetails,
  type RuntimeResult,
  type SmsBalanceResult,
  type SmsSelftestResult,
} from './diagnostics';

type Provider = {
  kind: string;
  display_name: string;
  pooled?: boolean;
  ephemeral?: boolean;
  line_segments?: number;
  config_fields?: ConfigField[];
};
type Country = {
  id: string;
  name_cn: string;
  openai_sms_safe?: boolean;
  price?: number;
  count?: number;
};
type ConfigResponse = { config?: InvitationConfig };
type Section = 'mail' | 'sms' | 'export' | 'invite' | 'pool';
type Diagnostic = {
  title: string;
  kind: 'sms' | 'balance' | 'runtime' | 'message';
  data: SmsSelftestResult | SmsBalanceResult | RuntimeResult | { message?: string };
};

const groupsBySection = {
  sms: SMS_GROUPS,
  export: EXPORT_GROUPS,
  invite: INVITE_GROUPS,
  pool: POOL_GROUPS,
};
/**
 * 账号池的数值项与邀请设置共用 /settings/invite 端点往返，但各自只提交本区块的字段，
 * 因此两套配置互不覆盖。
 */
const sectionEndpoint = (section: Section) => (section === 'pool' ? 'invite' : section);

export function ConfigEditor({ section }: { section: Section }) {
  const [saved, setSaved] = useState<InvitationConfig>({});
  const [draft, setDraft] = useState<InvitationConfig>({});
  const [providers, setProviders] = useState<Provider[]>([]);
  // 加载动作 key：'load' 页面数据（挂载 / 重试 / 重新加载），'reread' 保存成功后的权威复读。
  const [loading, setLoading] = useState<string | null>('load');
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [countries, setCountries] = useState<Country[]>([]);
  const [countrySource, setCountrySource] = useState('');
  const [countryError, setCountryError] = useState('');
  // 国家列表是独立动作：只有它自己转圈，页面数据加载不会点亮它，反之也一样。
  const [countriesLoading, setCountriesLoading] = useState<string | null>(null);
  const [diagnostic, setDiagnostic] = useState<Diagnostic | null>(null);
  const countryRequest = useRef(0);
  const pool = useInvitationQuery<{
    available?: number;
    mail_source?: string;
    pooled?: boolean;
    items?: { email: string; kind: string; imported_at?: number }[];
  }>(section === 'invite' ? '/mail/pool?limit=20' : null);
  const overview = useInvitationQuery<{
    cdk?: { running?: number; queued?: number; done?: number; failed?: number };
  }>(section === 'invite' ? '/overview' : null);
  const mailSource = draft.mail_source || saved.mail_source || '';
  const provider = providers.find((item) => item.kind === mailSource);
  const mailFields = useMemo(
    () => providers.flatMap((item) => item.config_fields || []),
    [providers],
  );
  const allFields = useMemo(
    () =>
      section === 'mail'
        ? [
            { key: 'mail_source', label: '邮箱来源' },
            { key: 'pool_mail_source', label: '补号邮箱来源' },
            ...mailFields,
          ]
        : groupsBySection[section].flatMap((group) => group.fields),
    [mailFields, section],
  );
  const groups =
    section === 'mail'
      ? [{ title: '邮箱来源设置', fields: provider?.config_fields || [] }]
      : groupsBySection[section];

  const load = useCallback(async (action: string = 'load') => {
    setLoading(action);
    setError('');
    try {
      const [response, declarations] = await Promise.all([
        invitationApi<ConfigResponse>(`/settings/${sectionEndpoint(section)}`),
        section === 'mail'
          ? invitationApi<{ providers?: Provider[]; current?: string }>('/mail/providers')
          : Promise.resolve(null),
      ]);
      const config = response.config || {};
      const list = declarations?.providers || [];
      const fields =
        section === 'mail'
          ? [
              { key: 'mail_source', label: '邮箱来源' },
              { key: 'pool_mail_source', label: '补号邮箱来源' },
              ...list.flatMap((item) => item.config_fields || []),
            ]
          : groupsBySection[section].flatMap((group) => group.fields);
      setProviders(list);
      const next = { ...config };
      if (section === 'mail' && !next.mail_source)
        next.mail_source = declarations?.current || list[0]?.kind || '';
      setSaved(next);
      setDraft(configDraft(next, fields));
      setLoaded(true);
    } catch (failure) {
      setError(invitationErrorMessage(failure));
    } finally {
      setLoading(null);
    }
  }, [section]);
  useEffect(() => {
    void load();
  }, [load]);

  const loadCountries = useCallback(async (platform: string) => {
    const request = ++countryRequest.current;
    setCountriesLoading('countries');
    setCountryError('');
    try {
      const result = await invitationApi<{ countries?: Country[]; source?: string }>(
        `/settings/sms/all_countries?provider=${encodeURIComponent(platform === 'auto' ? '' : platform)}`,
      );
      if (request === countryRequest.current) {
        setCountries(result.countries || []);
        setCountrySource(result.source || '');
      }
    } catch (failure) {
      if (request === countryRequest.current) setCountryError(invitationErrorMessage(failure));
    } finally {
      if (request === countryRequest.current) setCountriesLoading(null);
    }
  }, []);
  useEffect(() => {
    if (section === 'sms' && loaded) void loadCountries(draft.sms_platform_mode || 'auto');
  }, [section, loaded, draft.sms_platform_mode, loadCountries]);

  function update(key: string, value: string) {
    setDraft((previous) => ({ ...previous, [key]: value }));
  }
  async function save() {
    const relevant =
      section === 'mail'
        ? allFields.filter(
            (field) =>
              field.key === 'mail_source' ||
              field.key === 'pool_mail_source' ||
              (provider?.config_fields || []).some((item) => item.key === field.key),
          )
        : allFields;
    const missing = relevant.filter(
      (field) =>
        field.required &&
        !draft[field.key]?.trim() &&
        !(field.type === 'password' && saved[field.key]),
    );
    if (missing.length) {
      message.warning(`请填写：${missing.map((field) => field.label).join('、')}`);
      return;
    }
    const payload = changedConfigPayload(saved, draft, relevant);
    if (!Object.keys(payload).length) {
      message.info('没有需要保存的改动');
      return;
    }
    const shortKey = Object.entries(payload).find(
      ([key, value]) => key.startsWith('sms_api_key_') && value.length < 16,
    );
    if (shortKey) {
      message.warning('接码 API Key 至少需要 16 位，请检查后再保存');
      return;
    }
    setBusy('save');
    try {
      await invitationAction(`/settings/${sectionEndpoint(section)}`, payload);
      message.success(
        section === 'invite' && 'cdk_max_concurrency' in payload
          ? '已保存，并发上限在执行实例重启后生效'
          : '配置已保存',
      );
      await load('reread');
    } catch (failure) {
      message.error(invitationErrorMessage(failure));
    } finally {
      setBusy('');
    }
  }
  async function test(kind: string, path: string, body?: Record<string, string>, read = false) {
    setBusy(kind);
    try {
      const result = read
        ? await invitationApi<RuntimeResult>(path)
        : await invitationAction<Diagnostic['data']>(path, body || {});
      const diagnosticKind =
        kind === 'runtime'
          ? 'runtime'
          : kind === 'selftest'
            ? 'sms'
            : kind === 'sms'
              ? 'balance'
              : 'message';
      setDiagnostic({
        title:
          kind === 'runtime'
            ? '运行依赖自检'
            : kind === 'selftest'
              ? '注册接码接口自检'
              : kind === 'sms'
                ? '三平台余额测试'
                : '配置测试结果',
        kind: diagnosticKind,
        data: result,
      });
    } catch (failure) {
      message.error(invitationErrorMessage(failure));
    } finally {
      setBusy('');
    }
  }
  const countryOptions = countries.map((country) => ({
    value: String(country.id),
    label: `${country.id} · ${country.name_cn}${country.openai_sms_safe ? ' · 项目经验名单' : ''}${country.price != null ? ` · ${country.price} / 库存 ${country.count ?? '—'}` : ''}`,
  }));
  function renderField(field: ConfigField) {
    const value = draft[field.key] || '';
    const onChange = (next: string) => update(field.key, next);
    const placeholder =
      isInvitationMask(saved[field.key]) || (field.type === 'password' && saved[field.key])
        ? '已设置，留空保留原值'
        : field.placeholder;
    if (field.type === 'boolean')
      return (
        <Switch
          checked={value === '1' || value === 'true'}
          onChange={(checked) => onChange(checked ? '1' : '0')}
        />
      );
    if (field.type === 'password')
      return (
        <Input.Password
          autoComplete="new-password"
          value={value}
          placeholder={placeholder || '留空保留原值'}
          onChange={(event) => onChange(event.target.value)}
        />
      );
    if (field.type === 'textarea')
      return (
        <Input.TextArea
          rows={3}
          value={value}
          placeholder={placeholder}
          onChange={(event) => onChange(event.target.value)}
        />
      );
    if (field.type === 'number')
      return (
        <InputNumber
          style={{ width: '100%' }}
          value={value ? Number(value) : null}
          min={field.min}
          max={field.max}
          onChange={(next) => onChange(next == null ? '' : String(next))}
        />
      );
    if (field.type === 'weight')
      return (
        <Slider
          value={Number(value || 0)}
          min={0}
          max={1}
          step={0.05}
          onChange={(next) => onChange(String(next))}
        />
      );
    if (field.type === 'select')
      return <Select value={value || undefined} options={field.options} onChange={onChange} />;
    if (field.type === 'platforms') {
      const current = new Set(value.toLowerCase().split(',').map((item) => item.trim()));
      const selected = (field.options || [])
        .filter((option) => current.has(option.value))
        .map((option) => option.value);
      return (
        <Select
          mode="multiple"
          allowClear
          style={{ width: '100%' }}
          options={field.options}
          value={selected}
          placeholder="所有已配置密钥的平台"
          onChange={(next: string[]) => {
            const picked = new Set(next);
            onChange(
              (field.options || [])
                .filter((option) => picked.has(option.value))
                .map((option) => option.value)
                .join(',') || 'auto',
            );
          }}
        />
      );
    }
    if (field.type === 'country' || field.type === 'countries') {
      const multiple = field.type === 'countries';
      const current = value.split(',').filter(Boolean);
      const options = [
        ...countryOptions,
        ...current
          .filter((item) => !countryOptions.some((option) => option.value === item))
          .map((item) => ({ value: item, label: item })),
      ];
      return (
        <Select
          showSearch
          allowClear
          optionFilterProp="label"
          mode={multiple ? 'tags' : undefined}
          loading={countriesLoading === 'countries'}
          options={options}
          value={multiple ? current : value || undefined}
          placeholder={multiple ? '空为不限；可输入国家 ID' : '留空不限制国家'}
          onChange={(next: string | string[] | undefined) =>
            onChange(Array.isArray(next) ? next.join(',') : next || '')
          }
        />
      );
    }
    return (
      <Input
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }

  return (
    <Spin spinning={loading !== null}>
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        {error && (
          <Alert
            showIcon
            type="error"
            message={error}
            action={<Button onClick={() => void load()}>重试</Button>}
          />
        )}
        {section === 'mail' && (
          <Card title="邮箱来源">
            <Select
              disabled={!!busy}
              style={{ width: '100%', maxWidth: 500 }}
              value={mailSource || undefined}
              options={providers.map((item) => ({ value: item.kind, label: item.display_name }))}
              onChange={(value) => update('mail_source', value)}
            />
            {provider && (
              <Space wrap style={{ marginTop: 16 }}>
                <Tag color={provider.pooled ? 'orange' : 'green'}>
                  {provider.pooled ? '号池型，需要导入邮箱' : '自动生成邮箱'}
                </Tag>
                <Tag>{provider.ephemeral ? '每次新地址' : '固定地址'}</Tag>
                {!!provider.line_segments && <Tag>导入格式 {provider.line_segments} 段</Tag>}
              </Space>
            )}
            {provider?.pooled && (
              <Typography.Paragraph style={{ marginTop: 12, marginBottom: 0 }}>
                该来源通过 <Link href="/invitation/mail-accounts">邮箱管理</Link>{' '}
                导入账号，不需要单独测试。
              </Typography.Paragraph>
            )}
          </Card>
        )}
        {section === 'mail' && (
          <Card title="补号邮箱来源" style={{ marginTop: 16 }}>
            <Select
              aria-label="补号邮箱来源"
              disabled={!!busy}
              style={{ width: '100%', maxWidth: 500 }}
              value={draft.pool_mail_source ?? saved.pool_mail_source ?? ''}
              options={[
                { value: '', label: '跟随上方（与兑换同一个来源）' },
                ...providers.map((item) => ({ value: item.kind, label: item.display_name })),
              ]}
              onChange={(value) => update('pool_mail_source', value)}
            />
            <Typography.Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
              账号工厂补号时单独使用这个来源，和上面的兑换来源互不影响。选「自动生成邮箱」这类来源时，
              补号会在注册那一刻现造地址并直接入池；选「号池型」来源时，补号会消耗邮箱管理里导入的「邮箱（未注册）」。
              每个号都按自己登记时的来源取验证码与邀请信，所以两边可以不同。
            </Typography.Paragraph>
          </Card>
        )}
        {section === 'sms' && (
          <>
            <Alert
              showIcon
              type="info"
              message="这里配置 Codex 注册的手机验证接码。"
              description="每个号码仅使用一次，失败可换新号。各平台密钥独立保存，余额测试同时查询三个平台的已保存配置。"
            />
            <Space wrap>
              <Button
                loading={countriesLoading === 'countries'}
                onClick={() => void loadCountries(draft.sms_platform_mode || 'auto')}
              >
                刷新国家列表
              </Button>
              <Tag>{countrySource === 'live' ? '平台实时国家与库存' : '国家字典'}</Tag>
            </Space>
            {countryError && (
              <Alert type="warning" showIcon message={`国家列表加载失败：${countryError}`} />
            )}
          </>
        )}
        {section === 'export' && (
          <Alert
            showIcon
            type="info"
            message="注册成功后自动导出到已启用的面板，导出失败只记日志。"
            description="需要带 refresh_token 的账号才能在面板正常使用。测试使用已保存配置。"
          />
        )}
        {groups
          .filter((group) => group.fields.length > 0)
          .map((group) => (
            <Card key={group.title} title={group.title}>
              <Form disabled={!!busy} layout="vertical">
                <Row gutter={[24, 0]}>
                  {group.fields.map((field) => (
                    <Col
                      key={field.key}
                      xs={24}
                      md={
                        field.type === 'textarea' ||
                        field.type === 'countries' ||
                        field.type === 'country'
                          ? 24
                          : 12
                      }
                    >
                      <Form.Item label={field.label} required={field.required} help={field.help}>
                        {renderField(field)}
                      </Form.Item>
                    </Col>
                  ))}
                </Row>
              </Form>
            </Card>
          ))}
        {section === 'invite' && (
          <Card title="可兑换邮箱与作业状态">
            <Space wrap>
              <Tag color="green">
                可用邮箱 {pool.data?.available === -1 ? '不限量' : (pool.data?.available ?? '—')}
              </Tag>
              <Tag>来源 {pool.data?.mail_source || '—'}</Tag>
              <Tag>运行 {overview.data?.cdk?.running || 0}</Tag>
              <Tag>排队 {overview.data?.cdk?.queued || 0}</Tag>
              <Tag color="green">完成 {overview.data?.cdk?.done || 0}</Tag>
              <Tag color="red">失败 {overview.data?.cdk?.failed || 0}</Tag>
            </Space>
            {(pool.error || overview.error) && (
              <Alert
                style={{ marginTop: 12 }}
                type="warning"
                message={pool.error || overview.error}
              />
            )}
            <Table
              style={{ marginTop: 16 }}
              rowKey="email"
              size="small"
              dataSource={pool.data?.items || []}
              pagination={false}
              columns={[
                { title: '邮箱', dataIndex: 'email' },
                {
                  title: '来源',
                  dataIndex: 'kind',
                  // 来源一律显示中文名；未知来源不能漏出 kind 原文。
                  render: (value: string) =>
                    providers.find((item) => item.kind === value)?.display_name ||
                    unknownLabel(value),
                },
                { title: '导入时间', dataIndex: 'imported_at', render: invitationTime },
              ]}
            />
          </Card>
        )}
        <Card>
          <Space wrap>
            <Button
              type="primary"
              disabled={!loaded || !!error || (!!busy && busy !== 'save')}
              loading={busy === 'save'}
              onClick={() => void save()}
            >
              保存配置
            </Button>
            <Button
              disabled={!!busy}
              onClick={() =>
                Modal.confirm({ title: '重新加载并放弃未保存改动？', onOk: () => load() })
              }
            >
              重新加载
            </Button>
            {section === 'mail' && provider && !provider.pooled && (
              <Button disabled={!!busy} onClick={() => void test('mail', '/settings/mail/test')}>
                测试邮箱连通性
              </Button>
            )}
            {section === 'sms' && (
              <>
                <Button disabled={!!busy} onClick={() => void test('sms', '/settings/sms/test')}>
                  测试三个平台余额
                </Button>
                <Button disabled={!!busy} onClick={() => void test('selftest', '/sms/selftest')}>
                  接口自检
                </Button>
                <Link href="/invitation/sms-prices">查看接码价格</Link>
              </>
            )}
            {section === 'export' && (
              <>
                <Button
                  disabled={!!busy}
                  onClick={() => void test('cpa', '/settings/export/test', { target: 'cpa' })}
                >
                  测试 CPA 连通性
                </Button>
                <Button
                  disabled={!!busy}
                  onClick={() =>
                    void test('sub2api', '/settings/export/test', { target: 'sub2api' })
                  }
                >
                  测试 SUB2API 连通性
                </Button>
              </>
            )}
            {section === 'invite' && (
              <Button
                disabled={!!busy}
                onClick={() => void test('runtime', '/settings/runtime_check', undefined, true)}
              >
                运行依赖自检
              </Button>
            )}
            {busy && busy !== 'save' && (
              <Typography.Text type="secondary">正在测试已保存配置…</Typography.Text>
            )}
          </Space>
        </Card>
      </Space>
      <Modal
        open={!!diagnostic}
        title={diagnostic?.title}
        width={900}
        footer={null}
        onCancel={() => setDiagnostic(null)}
        destroyOnHidden
      >
        {diagnostic?.kind === 'sms' ? (
          <SmsSelftestDetails result={diagnostic.data as SmsSelftestResult} />
        ) : diagnostic?.kind === 'balance' ? (
          <SmsBalanceDetails result={diagnostic.data as SmsBalanceResult} />
        ) : diagnostic?.kind === 'runtime' ? (
          <RuntimeDetails result={diagnostic.data as RuntimeResult} />
        ) : (
          <Typography.Paragraph>
            {localizedText((diagnostic?.data as { message?: string })?.message, '连接成功')}
          </Typography.Paragraph>
        )}
      </Modal>
    </Spin>
  );
}
