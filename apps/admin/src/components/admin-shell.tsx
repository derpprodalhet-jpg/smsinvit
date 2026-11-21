import {
  AuditOutlined,
  BellOutlined,
  CloudServerOutlined,
  ControlOutlined,
  DashboardOutlined,
  FieldTimeOutlined,
  GlobalOutlined,
  HistoryOutlined,
  KeyOutlined,
  LogoutOutlined,
  MailOutlined,
  MobileOutlined,
  SafetyCertificateOutlined,
  SendOutlined,
  SettingOutlined,
  SolutionOutlined,
  StopOutlined,
  TagOutlined,
  TeamOutlined,
  UnorderedListOutlined,
  UserAddOutlined,
} from '@ant-design/icons';
import type { ProLayoutProps } from '@ant-design/pro-components';
import { ProLayout } from '@ant-design/pro-components';
import { Avatar, Dropdown, Space, Spin, Typography } from 'antd';
import { Link } from '../lib/navigation';
import { usePathname, useRouter } from '../lib/navigation';
import { useEffect, useMemo } from 'react';
import { useAdminAuth } from '../lib/admin-auth-context';

const PUBLIC_PATHS = ['/login'];
const TOTP_ALLOWED_WHEN_REQUIRED = ['/security', '/login'];

type NavNode = {
  path: string;
  name: string;
  icon?: React.ReactNode;
  roles: string[];
  children?: NavNode[];
};

const STAFF = ['ADMIN', 'SUPER_ADMIN'];
const NAV: NavNode[] = [
  { path: '/', name: '兑换工作台', icon: <DashboardOutlined />, roles: STAFF },
  { path: '/plans', name: '套餐管理', icon: <ControlOutlined />, roles: STAFF },
  { path: '/batches', name: '卡密批次', icon: <KeyOutlined />, roles: STAFF },
  {
    path: '/sms',
    name: '接码',
    icon: <MobileOutlined />,
    roles: STAFF,
    children: [
      { path: '/sms', name: '接码概览', icon: <DashboardOutlined />, roles: STAFF },
      { path: '/sms/redeems', name: '接码记录与异常', icon: <UnorderedListOutlined />, roles: STAFF },
      { path: '/sms/cdk', name: '接码卡密管理', icon: <KeyOutlined />, roles: STAFF },
      { path: '/sms/settings', name: '接码配置', icon: <SettingOutlined />, roles: STAFF },
    ],
  },
  {
    path: '/invitation',
    name: 'Codex 邀请',
    icon: <TeamOutlined />,
    roles: STAFF,
    children: [
      { path: '/invitation', name: '邀请概览', icon: <DashboardOutlined />, roles: STAFF },
      { path: '/invitation/mail-accounts', name: '邮箱管理', icon: <MailOutlined />, roles: STAFF },
      { path: '/invitation/register', name: '单次注册', icon: <UserAddOutlined />, roles: STAFF },
      { path: '/invitation/registered', name: '注册结果', icon: <SolutionOutlined />, roles: STAFF },
      { path: '/invitation/runs', name: '运行记录', icon: <HistoryOutlined />, roles: STAFF },
      { path: '/invitation/cdk', name: '卡密管理', icon: <KeyOutlined />, roles: STAFF },
      { path: '/invitation/proxies', name: '代理池', icon: <GlobalOutlined />, roles: STAFF },
      { path: '/invitation/sms-prices', name: '接码价格', icon: <TagOutlined />, roles: STAFF },
      { path: '/invitation/settings', name: '服务配置', icon: <SettingOutlined />, roles: STAFF },
    ],
  },
  {
    path: '/system',
    name: '系统管理',
    icon: <SettingOutlined />,
    roles: STAFF,
    children: [
      { path: '/staff', name: '管理员', icon: <TeamOutlined />, roles: STAFF },
      { path: '/security', name: '登录安全', icon: <SafetyCertificateOutlined />, roles: STAFF },
      { path: '/security/ip-bans', name: 'IP 封禁', icon: <StopOutlined />, roles: STAFF },
      { path: '/site/notifications', name: '通知配置', icon: <BellOutlined />, roles: STAFF },
      { path: '/notification-jobs', name: '通知发送记录', icon: <SendOutlined />, roles: STAFF },
      { path: '/jobs', name: '持久任务', icon: <CloudServerOutlined />, roles: STAFF },
      { path: '/schedulers', name: '定时任务', icon: <FieldTimeOutlined />, roles: STAFF },
      { path: '/audit', name: '操作审计', icon: <AuditOutlined />, roles: STAFF },
    ],
  },
];

function canSee(roles: string[] | undefined, allowed: string[]) {
  const set = new Set(roles || []);
  return allowed.some((r) => set.has(r));
}

function isActivePath(pathname: string, href: string) {
  if (href === '/') return pathname === '/';
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** 收集导航树全部叶子路径。 */
function navLeaves(): Array<{ path: string; roles: string[]; name: string; parentName?: string }> {
  const leaves: Array<{ path: string; roles: string[]; name: string; parentName?: string }> = [];
  const walk = (nodes: NavNode[], parentName?: string) => {
    for (const node of nodes) {
      if (node.children && node.children.length > 0) {
        walk(node.children, node.name);
      } else {
        leaves.push({ path: node.path, roles: node.roles, name: node.name, parentName });
      }
    }
  };
  walk(NAV);
  return leaves;
}

/**
 * 取最长匹配叶子路径，避免 `/sms/redeems` 同时点亮 `/sms` 概览。
 * ProLayout 默认按前缀匹配会把父级叶子一并选中。
 */
function activeLeafPath(pathname: string): string {
  let match: string | undefined;
  for (const leaf of navLeaves()) {
    if (!isActivePath(pathname, leaf.path)) continue;
    if (!match || leaf.path.length > match.length) match = leaf.path;
  }
  return match || pathname;
}

function pageContext(pathname: string): { name: string; parentName?: string } {
  let match: { path: string; name: string; parentName?: string } | undefined;
  for (const leaf of navLeaves()) {
    if (!isActivePath(pathname, leaf.path)) continue;
    if (!match || leaf.path.length > match.path.length) match = leaf;
  }
  return match ? { name: match.name, parentName: match.parentName } : { name: '工作台' };
}

/** Prefer the longest matching leaf so /community/reports wins over /community. */
function rolesForPath(pathname: string): string[] | null {
  let match: { path: string; roles: string[] } | undefined;
  for (const leaf of navLeaves()) {
    if (!isActivePath(pathname, leaf.path)) continue;
    if (!match || leaf.path.length > match.path.length) match = leaf;
  }
  return match ? match.roles : null;
}

function filterNavNodes(
  nodes: NavNode[],
  roles: string[] | undefined,
  totpSetupRequired?: boolean,
): NonNullable<ProLayoutProps['route']>['routes'] {
  return nodes.flatMap((entry) => {
    if (!canSee(roles, entry.roles)) return [];
    if (entry.children?.length) {
      const children = filterNavNodes(entry.children, roles, totpSetupRequired);
      if (!children?.length) return [];
      return [{ path: entry.path, name: entry.name, icon: entry.icon, routes: children }];
    }
    if (totpSetupRequired && entry.path !== '/security') return [];
    return [{ path: entry.path, name: entry.name, icon: entry.icon }];
  });
}

/**
 * 由 ProLayout 负责渲染图标的一级**叶子**项。
 *
 * 注意不能简单用「path 是否在 NAV 顶层」来判断：各模块分组的第一个子项
 * （接码概览 `/sms`、邀请概览 `/invitation`）
 * 与所属分组**同名**，那样会被误判成一级、图标就丢了。所以这里只收「没有 children 的
 * 一级项」，其余一律由 menuItemRender 自己补图标。
 */
const PRO_LAYOUT_ICON_PATHS = new Set(
  NAV.filter((entry) => !entry.children?.length).map((entry) => entry.path),
);

function hasProLayoutIcon(path?: string): boolean {
  return !!path && PRO_LAYOUT_ICON_PATHS.has(path);
}

function filterNav(
  roles: string[] | undefined,
  totpSetupRequired?: boolean,
): ProLayoutProps['route'] {
  return { path: '/', routes: filterNavNodes(NAV, roles, totpSetupRequired) };
}

export function AdminShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { user, loading, logout } = useAdminAuth();
  const isPublic = PUBLIC_PATHS.includes(pathname);
  const pathRoles = useMemo(() => rolesForPath(pathname), [pathname]);
  const totpSetupRequired = Boolean(user?.totpSetupRequired);

  useEffect(() => {
    if (loading) return;
    if (isPublic) {
      if (user) router.replace('/');
      return;
    }
    if (!user) {
      router.replace('/login');
      return;
    }
    const canAccess = (user.roles || []).some((role) => ['ADMIN', 'SUPER_ADMIN'].includes(role));
    if (!canAccess) {
      void logout().then(() => router.replace('/login'));
      return;
    }
    if (user.totpSetupRequired && !TOTP_ALLOWED_WHEN_REQUIRED.includes(pathname)) {
      router.replace('/security');
      return;
    }
    if (pathRoles && !canSee(user.roles, pathRoles)) {
      router.replace('/');
    }
  }, [isPublic, loading, user, router, logout, pathRoles, pathname]);

  if (isPublic) {
    // Only block the form while auth state is unknown. Stale sessions resolve to
    // user=null after /me fails and clear the session flag — show the login form.
    if (loading) {
      return (
        <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
          <Spin tip="正在验证登录状态…" size="large" />
        </div>
      );
    }
    if (user) {
      return (
        <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
          <Spin tip="正在跳转…" size="large" />
        </div>
      );
    }
    return <>{children}</>;
  }

  if (loading) {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
        <Spin tip="正在验证登录状态…" size="large" />
      </div>
    );
  }

  if (!user) return null;

  if (pathRoles && !canSee(user.roles, pathRoles) && pathname !== '/security') {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center' }}>
        <Typography.Text type="secondary">无权限访问该页面，正在返回仪表盘…</Typography.Text>
      </div>
    );
  }

  const roleLabel = user.roles?.[0] || 'USER';
  const selectedMenuPath = activeLeafPath(pathname);
  const headerCtx = pageContext(pathname);

  return (
    <div className="admin-shell">
      <ProLayout
        title="SmsInvit"
        layout="side"
        siderWidth={248}
        contentWidth="Fluid"
        logo={<span className="admin-brand-mark">L</span>}
        fixSiderbar
        fixedHeader
        location={{ pathname: selectedMenuPath }}
        route={filterNav(user.roles, totpSetupRequired)}
        menuProps={{ selectedKeys: [selectedMenuPath] }}
        headerContentRender={() => (
          <div className="admin-header-context">
            {headerCtx.parentName ? (
              <>
                <span className="admin-header-context__muted">{headerCtx.parentName}</span>
                <span className="admin-header-context__muted">/</span>
              </>
            ) : null}
            <span>{headerCtx.name}</span>
          </div>
        )}
        token={{
          header: {
            colorBgHeader: '#ffffff',
            colorHeaderTitle: '#17253d',
            colorTextMenu: '#667085',
            colorTextMenuSelected: '#2a7a7b',
            colorBgMenuItemSelected: '#e6f2f2',
            heightLayoutHeader: 64,
          },
          sider: {
            colorMenuBackground: '#fff',
            colorTextMenuSelected: '#2a7a7b',
            colorBgMenuItemSelected: '#e6f2f2',
            colorTextMenu: '#5c6b7a',
            colorTextMenuActive: '#2a7a7b',
            colorBgMenuItemHover: '#f4f8f8',
          },
          pageContainer: {
            paddingBlockPageContainerContent: 24,
            paddingInlinePageContainerContent: 28,
            colorBgPageContainer: '#eef3f4',
          },
        }}
        // Side layout would park avatarProps in the sider footer (bottom-left).
        // Put the account menu in the header top-right instead.
        avatarProps={false}
        menuFooterRender={false}
        actionsRender={() => {
          const initial = (user.displayName || user.username || '?').slice(0, 1).toUpperCase();
          return [
            <Dropdown
              key="account"
              menu={{
                items: [
                  {
                    key: 'user',
                    disabled: true,
                    label: `${user.email} · ${roleLabel}`,
                  },
                  {
                    key: 'security',
                    icon: <SafetyCertificateOutlined />,
                    label: '安全设置',
                    onClick: () => router.push('/security'),
                  },
                  { type: 'divider' },
                  {
                    key: 'logout',
                    icon: <LogoutOutlined />,
                    label: '退出登录',
                    onClick: async () => {
                      await logout();
                      router.replace('/login');
                    },
                  },
                ],
              }}
            >
              <Space className="admin-header-user" size={8} style={{ cursor: 'pointer' }}>
                <Avatar size="small" style={{ background: '#2a7a7b' }}>
                  {initial}
                </Avatar>
                <span className="admin-header-user__name">{user.displayName || user.username}</span>
              </Space>
            </Dropdown>,
          ];
        }}
        menuItemRender={(item, dom) => {
          const path = item.path || '/';
          // Group nodes (e.g. /blog) have no page — only link real leaves.
          const childRoutes = (item as { routes?: unknown }).routes;
          if (Array.isArray(childRoutes) && childRoutes.length > 0) {
            return <span>{dom}</span>;
          }
          // ProLayout 在**展开态**只给一级项渲染图标，二级项拿到的是空的、还被内联
          // `display:none` 的占位（`ant-pro-base-menu-inline-item-icon`）→ 二级菜单
          // 看起来没有图标。二级项在这里自己补图标；一级项继续用 ProLayout 渲染的
          // `dom`（含图标），避免出现两个图标。
          const label = hasProLayoutIcon(path) ? (
            dom
          ) : (
            <span className="admin-menu-child">
              {item.icon ? <span className="admin-menu-child-icon">{item.icon}</span> : null}
              <span className="admin-menu-child-text">{item.name}</span>
            </span>
          );
          return <Link href={path}>{label}</Link>;
        }}
        onMenuHeaderClick={() => router.push(totpSetupRequired ? '/security' : '/')}
      >
        {children}
      </ProLayout>
    </div>
  );
}
