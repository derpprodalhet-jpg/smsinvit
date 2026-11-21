import { useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { SmsRedeemPanel } from './components/sms-redeem-panel';
import { InvitationRedeemPanel } from './components/invitation-redeem-panel';
import { RedeemIntro } from './components/redeem-intro';
import { apiFetch } from './lib/api';
import './styles.css';

function App() {
  const invitation = ['/supprot_codex', '/supprot_codex/', '/invitation', '/invitation/'].includes(
    window.location.pathname,
  );
  const prefix = '/supprot_sim/';
  let code = '';
  if (window.location.pathname.startsWith(prefix)) {
    try {
      code = decodeURIComponent(window.location.pathname.slice(prefix.length));
    } catch {
      code = '';
    }
  }
  const [intro, setIntro] = useState(
    code
      ? 'CDK 将自动验证。复制手机号到目标站触发短信后，点击「获取验证码」。'
      : '粘贴接码 CDK，获取手机号与验证码。',
  );
  const [notices, setNotices] = useState<string[]>([]);
  // 「Codex 邀请」的可用账号数原本由面板渲染在卡片内，运营要求挪到标题行右侧并放大，
  // 所以在这一层取一次。可兑换账号数始终是实数（后端只送已注册 GPT、未授权且未占用
  // 的号）；收到负数/非数字就当未知隐藏，绝不显示"不限量"。
  //
  // 审查 8.5：这个数字原先只在组件加载时读一次，长时间停留或领取之后就会过时。
  // 现在抽成回调，并在页面重新可见时刷新；真实分配仍以服务端事务判断为准，
  // 这里只是让标题上的数字不至于明显失真。
  const [poolCount, setPoolCount] = useState<number | null>(null);
  const [poolUpdatedAt, setPoolUpdatedAt] = useState<Date | null>(null);
  const refreshPoolCount = useCallback(() => {
    if (!invitation) return;
    void apiFetch<{ poolAvailable?: number }>('/api/v1/invitation/page-content')
      .then(({ data }) => {
        const value = data?.poolAvailable;
        setPoolCount(typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null);
        setPoolUpdatedAt(new Date());
      })
      .catch(() => setPoolCount(null));
  }, [invitation]);
  useEffect(() => {
    refreshPoolCount();
  }, [refreshPoolCount]);
  useEffect(() => {
    if (!invitation) return;
    const onVisibility = () => {
      if (!document.hidden) refreshPoolCount();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, [invitation, refreshPoolCount]);
  useEffect(() => {
    if (invitation) return;
    void apiFetch<{ intro?: string; notices?: string[] }>('/api/v1/sms/page-content')
      .then(({ data }) => {
        if (!code && data?.intro?.trim()) setIntro(data.intro.trim());
        if (Array.isArray(data?.notices)) setNotices(data.notices.filter((n) => n?.trim()));
      })
      .catch(() => {});
  }, [invitation, code]);
  return (
    <>
      <header className="redeem-header">
        <a className="redeem-brand" href="/supprot_sim">
          SmsInvit
        </a>
        <nav aria-label="兑换服务">
          <a href="/supprot_sim" aria-current={invitation ? undefined : 'page'}>
            接码兑换
          </a>
          <a href="/supprot_codex" aria-current={invitation ? 'page' : undefined}>
            Codex 邀请
          </a>
        </nav>
      </header>
      <main className="redeem-main space-y-7">
        <RedeemIntro
          title={invitation ? 'Codex 邀请' : '接码兑换'}
          description={invitation ? '用卡密领取邮箱，发送邀请后再开始。' : intro}
          poolCount={invitation ? poolCount : null}
          poolUpdatedAt={invitation ? poolUpdatedAt : null}
        />
        {invitation ? (
          <InvitationRedeemPanel />
        ) : (
          <SmsRedeemPanel
            initialCode={code || undefined}
            autoStart={Boolean(code)}
            hideCodeForm={Boolean(code)}
          />
        )}
        {!invitation && notices.length > 0 && (
          <details className="space-y-3">
            <summary className="cursor-pointer text-sm font-semibold text-[var(--color-muted)]">
              重要提示
            </summary>
            <ul className="space-y-2 rounded-2xl border border-[var(--color-border)] bg-[var(--color-card)] p-4">
              {notices.map((n) => (
                <li key={n} className="text-sm text-[var(--color-muted)]">
                  {n}
                </li>
              ))}
            </ul>
          </details>
        )}
      </main>
    </>
  );
}
// The root is an alias to the SMS page; the two public landing pages are
// /supprot_sim (接码兑换) and /supprot_codex (Codex 邀请).
if (window.location.pathname === '/') window.history.replaceState(null, '', '/supprot_sim');
createRoot(document.getElementById('root')!).render(<App />);
