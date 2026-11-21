import { Space, Tag, Tooltip } from 'antd';
import { AdminStatusTag } from './admin-feedback';
import { phoneBoundLabel, phoneBoundShort, phoneBoundTone } from '../lib/invitation';

/** 邮箱池与注册结果共享的注册接码记录。 */
export type InvitationPhoneRecord = {
  phone_bound_ok?: number;
  phone_number?: string | null;
  phone_code?: string | null;
};

export function InvitationPhoneFields({ record }: { record: InvitationPhoneRecord }) {
  const number = String(record.phone_number ?? '').trim();
  const code = String(record.phone_code ?? '').trim();
  if (phoneBoundShort(record.phone_bound_ok).tone !== 'success' && !number && !code) return null;
  const visibleValue = (value: string) =>
    value === '***' ? '无凭据权限' : value || '未记录';
  return (
    <Space direction="vertical" size={0}>
      <span>手机号：{visibleValue(number)}</span>
      <span>验证码：{visibleValue(code)}</span>
    </Space>
  );
}

/** 悬浮内容带上实际接码证据，不能只显示成功说明。 */
export function InvitationPhoneTooltip({ record }: { record: InvitationPhoneRecord }) {
  return (
    <Space direction="vertical" size={4}>
      <span>{phoneBoundShort(record.phone_bound_ok).hint}</span>
      <InvitationPhoneFields record={record} />
    </Space>
  );
}

export function InvitationPhoneStatus({
  record,
  compact = false,
}: {
  record: InvitationPhoneRecord;
  compact?: boolean;
}) {
  if (compact) {
    const status = phoneBoundShort(record.phone_bound_ok);
    return (
      <Tooltip title={<InvitationPhoneTooltip record={record} />}>
        <span>
          <AdminStatusTag tone={status.tone}>{status.label}</AdminStatusTag>
        </span>
      </Tooltip>
    );
  }
  return (
    <Space direction="vertical" size={4}>
      <Tag color={phoneBoundTone(record.phone_bound_ok)}>
        {phoneBoundLabel(record.phone_bound_ok)}
      </Tag>
      <InvitationPhoneFields record={record} />
    </Space>
  );
}
