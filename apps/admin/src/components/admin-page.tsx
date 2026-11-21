import { PageContainer } from '@ant-design/pro-components';
import { Card } from 'antd';
import type { ReactNode } from 'react';

type AdminPageProps = {
  title: string;
  description?: string;
  extra?: ReactNode;
  children: ReactNode;
};

/** Thin PageContainer wrapper aligned with Ant Design Pro list pages. */
export function AdminPage({ title, description, extra, children }: AdminPageProps) {
  return (
    <PageContainer className="admin-page" title={title} content={description} extra={extra}>
      {children}
    </PageContainer>
  );
}

export function AdminCard({
  children,
  ...rest
}: { children: ReactNode } & React.ComponentProps<typeof Card>) {
  return (
    <Card bordered={false} styles={{ body: { padding: 0 } }} {...rest}>
      {children}
    </Card>
  );
}
