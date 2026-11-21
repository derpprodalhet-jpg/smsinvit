import type { ReactNode } from 'react';
import { Button, Descriptions, Drawer, Space, Typography, message } from 'antd';
import type { DescriptionsProps } from 'antd';

/**
 * 列表瘦身统一用这个详情抽屉。
 *
 * 约定：列表只留关键列，其余字段按 `groups` 分组放进这里。字段名一律中文，
 * 未确诊的枚举走 `resolveLabel` / `unknownLabel`（见 lib/zh-label.ts）。
 */
export type DetailGroup = {
  key: string;
  title?: string;
  items: NonNullable<DescriptionsProps['items']>;
};

/** 详情里的一键复制：安全上下文走剪贴板，否则退回隐藏文本框。 */
export async function copyDetailText(text: string): Promise<void> {
  if (!text) {
    message.info('没有可复制的内容');
    return;
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const element = document.createElement('textarea');
      element.value = text;
      element.style.position = 'fixed';
      element.style.left = '-9999px';
      document.body.appendChild(element);
      element.select();
      try {
        if (!document.execCommand('copy')) throw new Error('复制失败');
      } finally {
        element.remove();
      }
    }
    message.success('已复制');
  } catch {
    message.error('复制失败，请在详情里手动选择复制。');
  }
}

/** 等宽且可复制的值：用于长 ID、卡密、令牌。 */
export function CopyableValue({ value }: { value?: string | null }) {
  const text = String(value ?? '').trim();
  if (!text) return <>—</>;
  return (
    <Space size={4} style={{ maxWidth: '100%' }}>
      <Typography.Text code style={{ overflowWrap: 'anywhere' }}>
        {text}
      </Typography.Text>
      <Button type="link" size="small" onClick={() => void copyDetailText(text)}>
        复制
      </Button>
    </Space>
  );
}

export function AdminDetailDrawer({
  title,
  subtitle,
  open,
  onClose,
  width = 760,
  extra,
  groups = [],
  children,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  open: boolean;
  onClose: () => void;
  width?: number;
  extra?: ReactNode;
  groups?: DetailGroup[];
  children?: ReactNode;
}) {
  return (
    <Drawer
      title={title}
      width={Math.min(width, typeof window === 'undefined' ? width : window.innerWidth)}
      open={open}
      onClose={onClose}
      destroyOnClose
      extra={
        <Space wrap>
          {extra}
          <Button onClick={onClose}>关闭</Button>
        </Space>
      }
    >
      {subtitle ? (
        <Typography.Paragraph type="secondary" style={{ marginTop: 0 }}>
          {subtitle}
        </Typography.Paragraph>
      ) : null}
      <Space direction="vertical" size="large" style={{ width: '100%' }}>
        {groups.map((group) => (
          <div key={group.key}>
            {group.title ? (
              <Typography.Title level={5} style={{ marginTop: 0 }}>
                {group.title}
              </Typography.Title>
            ) : null}
            <Descriptions bordered size="small" column={1} items={group.items} />
          </div>
        ))}
        {children}
      </Space>
    </Drawer>
  );
}
