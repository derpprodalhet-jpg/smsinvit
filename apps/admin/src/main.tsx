import { lazy, Suspense } from 'react';
import { createRoot } from 'react-dom/client';
import { Spin } from 'antd';
import { AntdProvider } from './lib/antd-provider';
import { AdminAuthProvider } from './lib/admin-auth-context';
import { usePathname } from './lib/navigation';
import { AdminShell } from './components/admin-shell';
import { InvitationRuntimeProvider } from './lib/invitation-runtime';
import './pages/globals.css';
const pages = {
  '/': lazy(() => import('./pages/page')),
  '/login': lazy(() => import('./pages/login/page')),
  '/plans': lazy(() => import('./pages/plans/page')),
  '/batches': lazy(() => import('./pages/batches/page')),
  '/staff': lazy(() => import('./pages/staff/page')),
  '/security': lazy(() => import('./pages/security/page')),
  '/security/ip-bans': lazy(() => import('./pages/security/ip-bans/page')),
  '/sms': lazy(() => import('./pages/sms/page')),
  '/sms/redeems': lazy(() => import('./pages/sms/redeems/page')),
  '/sms/cdk': lazy(() => import('./pages/sms/cdk/page')),
  '/sms/settings': lazy(() => import('./pages/sms/settings/page')),
  '/invitation': lazy(() => import('./pages/invitation/page')),
  '/invitation/runs': lazy(() => import('./pages/invitation/runs/page')),
  '/invitation/mail-accounts': lazy(() => import('./pages/invitation/mail-accounts/page')),
  '/invitation/proxies': lazy(() => import('./pages/invitation/proxies/page')),
  '/invitation/settings': lazy(() => import('./pages/invitation/settings/page')),
  '/invitation/register': lazy(() => import('./pages/invitation/register/page')),
  '/invitation/registered': lazy(() => import('./pages/invitation/registered/page')),
  '/invitation/cdk': lazy(() => import('./pages/invitation/cdk/page')),
  '/invitation/sms-prices': lazy(() => import('./pages/invitation/sms-prices/page')),
  '/jobs': lazy(() => import('./pages/jobs/page')),
  '/schedulers': lazy(() => import('./pages/schedulers/page')),
  '/notification-jobs': lazy(() => import('./pages/notification-jobs/page')),
  '/audit': lazy(() => import('./pages/audit/page')),
  '/site/notifications': lazy(() => import('./pages/site/notifications/page')),
};
function App() {
  const path = usePathname().replace(/\/$/, '') || '/';
  const Page = pages[path as keyof typeof pages];
  return (
    <AntdProvider>
      <AdminAuthProvider>
        <InvitationRuntimeProvider>
          <AdminShell>
            <Suspense
              fallback={
                <div style={{ padding: 50, textAlign: 'center' }}>
                  <Spin size="large" />
                </div>
              }
            >
              {Page ? <Page key={path} /> : <p>页面不存在</p>}
            </Suspense>
          </AdminShell>
        </InvitationRuntimeProvider>
      </AdminAuthProvider>
    </AntdProvider>
  );
}
createRoot(document.getElementById('root')!).render(<App />);
