import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch, pollRetryDelay } from '../lib/api';
import { UserFacingError } from './error-with-request-id';
import { canRetryDeepLink } from '../lib/recovery-rules';

type SessionView = {
  id: string;
  phone?: string;
  phoneMasked?: string;
  phoneHistory?: string | null;
  status: string;
  smsCode?: string | null;
  smsCodes?: string[] | null;
  codeCount?: number;
  maxCodes?: number;
  attempt?: number;
  attemptCount?: number;
  maxAttempts?: number;
  canRetry?: boolean;
  canRequestNextSms?: boolean;
  listeningNextSms?: boolean;
  nextListenUntil?: string | null;
  providerFinalized?: boolean;
  cancelEligibleAt?: string | null;
  numberExpiresAt?: string | null;
  keyExpiresAt?: string | null;
  keyStatus?: string;
  cancelGraceSeconds?: number;
  codePending?: boolean;
  numberInvalid?: boolean;
  resultPending?: boolean;
  statusQueryUnavailable?: boolean;
  smsService?: string | null;
  smsCountry?: number | string | null;
  smsServiceName?: string | null;
  smsCountryName?: string | null;
  sessionSecret?: string | null;
};

type PhoneParts = {
  dial: string;
  national: string;
  area: string;
  local: string;
  display: string;
  withCountry: string;
  withoutCountry: string;
};

/** Idle → user can click 获取验证码; listening → silent poll; exhausted → only 换号. */
type ListenPhase = 'idle' | 'listening' | 'exhausted';

const DEFAULT_LISTEN_SEC = 300;

/** Public redeem UI: show friendly name only — strip "(ChatGPT)" / codes like "dr". */
function formatPublicServiceName(name?: string | null, code?: string | null): string {
  const raw = (name || code || '').trim();
  if (!raw) return '—';
  // Drop Latin or fullwidth parentheticals, e.g. "OpenAI (ChatGPT)" → "OpenAI"
  const cleaned = raw
    .replace(/\s*[（(][^）)]*[）)]\s*/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  return cleaned || '—';
}
const LISTEN_POLL_MS = 1000;

const CODE_MARKERS = ['①', '②', '③'];

function codesFromSession(session: SessionView | null | undefined): string[] {
  if (!session) return [];
  if (Array.isArray(session.smsCodes) && session.smsCodes.length > 0) {
    return session.smsCodes.map(String).filter((c) => c.trim());
  }
  if (session.smsCode) return [String(session.smsCode)];
  return [];
}

/** Parse server time to epoch ms. Handles ISO-Z, local strings, and Jackson LocalDateTime arrays. */
function parseServerTimeMs(value: unknown): number | null {
  if (value == null) return null;
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value < 1e12 ? value * 1000 : value;
  }
  if (Array.isArray(value) && value.length >= 3) {
    const y = Number(value[0]);
    const m = Number(value[1]);
    const d = Number(value[2]);
    const h = Number(value[3] ?? 0);
    const min = Number(value[4] ?? 0);
    const s = Math.floor(Number(value[5] ?? 0));
    const t = new Date(y, m - 1, d, h, min, s).getTime();
    return Number.isNaN(t) ? null : t;
  }
  const t = new Date(String(value)).getTime();
  return Number.isNaN(t) ? null : t;
}

function digitsOnly(value: string): string {
  return value.replace(/\D/g, '');
}

/** Format provider phone for display / copy. Prefers NANP (+1 区号 号码). */
function formatPhone(raw: string | undefined | null): PhoneParts | null {
  if (!raw) return null;
  const d = digitsOnly(raw);
  if (!d) return null;

  let dial = '';
  let national = d;
  if (d.length === 11 && d.startsWith('1')) {
    dial = '1';
    national = d.slice(1);
  } else if (d.length === 10) {
    dial = '1';
    national = d;
  } else if (d.length > 10) {
    dial = d.slice(0, d.length - 10);
    national = d.slice(-10);
  }

  const area = national.length >= 3 ? national.slice(0, 3) : '';
  const local = national.length > 3 ? national.slice(3) : national;
  const display = dial ? `+${dial} ${area}${local ? ` ${local}` : ''}`.trim() : national;
  return {
    dial,
    national,
    area,
    local,
    display,
    withCountry: dial ? `+${dial}${national}` : national,
    withoutCountry: national,
  };
}

async function writeClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    try {
      const el = document.createElement('textarea');
      el.value = text;
      el.setAttribute('readonly', '');
      el.style.position = 'fixed';
      el.style.left = '-9999px';
      document.body.appendChild(el);
      el.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(el);
      return ok;
    } catch {
      return false;
    }
  }
}

function CopyIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
      stroke="currentColor"
      strokeWidth="1.8"
    >
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V5a2 2 0 0 1 2-2h10" strokeLinecap="round" />
    </svg>
  );
}

export function SmsRedeemPanel({
  initialCode,
  autoStart = false,
  hideCodeForm = false,
}: {
  /** CDK from URL path `/supprot_sim/[code]`. */
  initialCode?: string;
  /** Auto-call redeem on mount when initialCode is set. */
  autoStart?: boolean;
  /** Hide manual CDK input (deep-link mode). */
  hideCodeForm?: boolean;
}) {
  const [code, setCode] = useState(initialCode?.trim() ?? '');
  const [busy, setBusy] = useState(false);
  const [booting, setBooting] = useState(Boolean(autoStart && initialCode?.trim()));
  const [error, setError] = useState<unknown>(null);
  const [queryUnavailable, setQueryUnavailable] = useState(false);
  const [session, setSession] = useState<SessionView | null>(null);
  const [copyHint, setCopyHint] = useState<string | null>(null);
  const [phoneMenuOpen, setPhoneMenuOpen] = useState(false);
  const [listenPhase, setListenPhase] = useState<ListenPhase>('idle');
  const [listenRemain, setListenRemain] = useState(0);
  const [nextListenRemain, setNextListenRemain] = useState(0);
  // F08：深链（/supprot_sim/<CDK>）自动兑换失败后，必须给出面板内的下一步，
  // 而不是只能刷新页面或退回普通入口。retryable 区分"临时故障可重查"与"确实失败"——
  // 卡密无效时不该劝用户重试。
  const [manualMode, setManualMode] = useState(false);
  const [deepLink, setDeepLink] = useState<{ failed: boolean; retryable: boolean }>({
    failed: false,
    retryable: false,
  });
  const sessionIdRef = useRef<string | null>(null);
  const sessionSecretRef = useRef<string | null>(null);
  const pollGenRef = useRef(0);
  const pollControllerRef = useRef<AbortController | null>(null);
  const phoneMenuRef = useRef<HTMLDivElement | null>(null);
  const copyHintTimer = useRef<number | null>(null);
  const autoStartedRef = useRef(false);
  const listenEndsAtRef = useRef<number | null>(null);
  const listenPhaseRef = useRef<ListenPhase>('idle');
  const nextListenEndsAtRef = useRef<number | null>(null);

  const setPhase = useCallback((phase: ListenPhase) => {
    listenPhaseRef.current = phase;
    setListenPhase(phase);
  }, []);

  const applySession = useCallback(
    (data: SessionView, gen: number) => {
      if (gen !== pollGenRef.current) return;
      sessionIdRef.current = data.id;
      if (data.sessionSecret) sessionSecretRef.current = data.sessionSecret;
      setSession(data);
      setQueryUnavailable(Boolean(data.statusQueryUnavailable));
      if (data.listeningNextSms) {
        let ends = parseServerTimeMs(data.nextListenUntil);
        if (ends == null) {
          // Keep a still-valid local deadline; otherwise seed a full grace window.
          if (nextListenEndsAtRef.current && nextListenEndsAtRef.current > Date.now()) {
            ends = nextListenEndsAtRef.current;
          } else {
            const grace = Math.max(30, data.cancelGraceSeconds ?? DEFAULT_LISTEN_SEC);
            ends = Date.now() + grace * 1000;
          }
        }
        nextListenEndsAtRef.current = ends;
        setNextListenRemain(Math.max(0, Math.ceil((ends - Date.now()) / 1000)));
      } else {
        nextListenEndsAtRef.current = null;
        setNextListenRemain(0);
      }
      if (
        data.status === 'SUCCESS' ||
        data.keyStatus === 'USED' ||
        data.smsCode ||
        (data.smsCodes && data.smsCodes.length)
      ) {
        setError(null);
        // First-code listen ends when SUCCESS; next-sms listen is driven by server fields.
        if (listenPhaseRef.current === 'listening') {
          setPhase('idle');
          listenEndsAtRef.current = null;
          setListenRemain(0);
        }
      } else if (data.numberInvalid) {
        setPhase('exhausted');
        listenEndsAtRef.current = null;
        setListenRemain(0);
      }
    },
    [setPhase],
  );

  const flashCopied = useCallback((label: string) => {
    setCopyHint(label);
    if (copyHintTimer.current) window.clearTimeout(copyHintTimer.current);
    copyHintTimer.current = window.setTimeout(() => setCopyHint(null), 1800);
  }, []);

  const copyText = useCallback(
    async (text: string, label: string) => {
      const ok = await writeClipboard(text);
      flashCopied(ok ? label : '复制失败，请手动选择');
      setPhoneMenuOpen(false);
    },
    [flashCopied],
  );

  const redeemCode = useCallback(
    async (raw: string) => {
      setBusy(true);
      setError(null);
      setPhase('idle');
      listenEndsAtRef.current = null;
      setListenRemain(0);
      pollGenRef.current += 1;
      pollControllerRef.current?.abort();
      pollControllerRef.current = null;
      const gen = pollGenRef.current;
      try {
        const { data } = await apiFetch<SessionView>('/api/v1/sms/redeem', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ code: raw.trim() }),
        });
        applySession(data, gen);
        if (data.numberInvalid && data.keyStatus !== 'USED' && data.status !== 'SUCCESS') {
          setPhase('exhausted');
        }
      } catch (err) {
        if (gen === pollGenRef.current) {
          setSession(null);
          sessionIdRef.current = null;
          sessionSecretRef.current = null;
          setError(err);
          // 深链自动兑换失败时记录一下：redeem 后端是幂等的（已有活跃会话会直接复用，
          // 不会二次租号），所以这里可以安全地提供"重新查询原兑换"。
          if (autoStart) setDeepLink({ failed: true, retryable: canRetryDeepLink(err) });
        }
      } finally {
        setBusy(false);
        setBooting(false);
      }
    },
    [applySession, setPhase, autoStart],
  );

  const poll = useCallback(
    async (sessionId: string, opts?: { silent?: boolean }) => {
      if (document.hidden || pollControllerRef.current || pollRetryDelay('sms') > 0) return null;
      const gen = pollGenRef.current;
      const secret = sessionSecretRef.current;
      if (!secret) {
        if (!opts?.silent) setError(new Error('会话密钥缺失，请重新兑换'));
        return null;
      }
      const controller = new AbortController();
      let timedOut = false;
      const timeout = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, 15000);
      pollControllerRef.current = controller;
      try {
        const { data } = await apiFetch<SessionView>(`/api/v1/sms/sessions/${sessionId}`, {
          headers: { 'X-Session-Secret': secret },
          signal: controller.signal,
        });
        if (controller.signal.aborted || gen !== pollGenRef.current) return null;
        const tracked = sessionIdRef.current;
        if (tracked && tracked !== sessionId && data.id !== tracked && data.id !== sessionId) {
          return null;
        }
        applySession(data, gen);
        return data;
      } catch (err) {
        if (gen !== pollGenRef.current || (controller.signal.aborted && !timedOut)) return null;
        setQueryUnavailable(true);
        // While silently listening for SMSBower, do not spam "no code" / transient errors.
        if (!opts?.silent) {
          setError(err);
        }
        return null;
      } finally {
        window.clearTimeout(timeout);
        if (pollControllerRef.current === controller) pollControllerRef.current = null;
      }
    },
    [applySession],
  );

  useEffect(() => {
    // 用户选择"改用手动输入"后不再自动重试，避免表单刚出现又被自动兑换拿走。
    if (!autoStart || manualMode || !initialCode?.trim() || autoStartedRef.current) return;
    autoStartedRef.current = true;
    void redeemCode(initialCode.trim());
  }, [autoStart, manualMode, initialCode, redeemCode]);

  // Poll while allocating a number after redeem / 换号.
  useEffect(() => {
    if (!session?.id || busy) return;
    if (!['PENDING_NUMBER', 'CANCEL_PENDING', 'ACQUIRE_UNKNOWN'].includes(session.status)) return;
    if (session.keyStatus === 'FAILED' || session.keyStatus === 'EXPIRED') return;
    const timer = window.setInterval(() => {
      const id = sessionIdRef.current;
      if (id) void poll(id, { silent: true });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [session?.id, session?.status, session?.keyStatus, busy, poll]);

  // Always sync WAITING_CODE (idle / listening / exhausted): webhook may mark SUCCESS
  // before the user clicks「获取验证码」, and the UI must pick that up without a refresh.
  useEffect(() => {
    if (!session?.id || busy) return;
    if (session.status !== 'WAITING_CODE') return;
    if (
      session.keyStatus === 'FAILED' ||
      session.keyStatus === 'EXPIRED' ||
      session.keyStatus === 'USED'
    ) {
      return;
    }
    const pull = () => {
      const id = sessionIdRef.current;
      if (id) void poll(id, { silent: true });
    };
    pull();
    const intervalMs = listenPhase === 'listening' ? LISTEN_POLL_MS : 2500;
    const poller = window.setInterval(pull, intervalMs);
    return () => window.clearInterval(poller);
  }, [session?.id, session?.status, session?.keyStatus, busy, listenPhase, poll]);

  // Poll while listening for another SMS on the same number (after「获取新短信」).
  useEffect(() => {
    if (!session?.id || busy) return;
    if (session.status !== 'SUCCESS') return;
    if (!session.listeningNextSms) return;
    const pull = () => {
      const id = sessionIdRef.current;
      if (id) void poll(id, { silent: true });
    };
    pull();
    const poller = window.setInterval(pull, LISTEN_POLL_MS);
    return () => window.clearInterval(poller);
  }, [session?.id, session?.status, session?.listeningNextSms, busy, poll]);

  // Countdown for「获取新短信」— use local endsAt seeded on applySession (stable vs zone-less JSON).
  useEffect(() => {
    if (!session?.listeningNextSms) {
      setNextListenRemain(0);
      nextListenEndsAtRef.current = null;
      return;
    }
    let finalizedOnce = false;
    const tick = () => {
      const ends = nextListenEndsAtRef.current;
      if (!ends) return;
      const left = Math.max(0, Math.ceil((ends - Date.now()) / 1000));
      setNextListenRemain(left);
      if (left <= 0 && !finalizedOnce) {
        finalizedOnce = true;
        const id = sessionIdRef.current;
        if (id) void poll(id, { silent: true });
      }
    };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [session?.listeningNextSms, session?.nextListenUntil, poll]);

  // After「获取验证码」: countdown only (polling handled above).
  useEffect(() => {
    if (listenPhase !== 'listening') return;
    if (!session?.id) return;
    if (session.status === 'SUCCESS' || session.keyStatus === 'USED') return;

    const tickRemain = () => {
      const ends = listenEndsAtRef.current;
      if (!ends) return;
      const left = Math.max(0, Math.ceil((ends - Date.now()) / 1000));
      setListenRemain(left);
      if (left <= 0 && listenPhaseRef.current === 'listening') {
        setPhase('exhausted');
        listenEndsAtRef.current = null;
      }
    };
    tickRemain();
    const countdown = window.setInterval(tickRemain, 1000);
    return () => window.clearInterval(countdown);
  }, [listenPhase, session?.id, session?.status, session?.keyStatus, setPhase]);

  // Tab focus: pull latest so a SUCCESS webhook while backgrounded still shows the code.
  useEffect(() => {
    const onVisible = () => {
      if (document.hidden) {
        pollControllerRef.current?.abort();
        pollControllerRef.current = null;
        return;
      }
      const id = sessionIdRef.current;
      if (!id) return;
      void poll(id, { silent: true });
    };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('online', onVisible);
    return () => {
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('online', onVisible);
    };
  }, [poll]);

  useEffect(() => {
    if (!phoneMenuOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (!phoneMenuRef.current?.contains(e.target as Node)) setPhoneMenuOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, [phoneMenuOpen]);

  useEffect(
    () => () => {
      pollGenRef.current += 1;
      pollControllerRef.current?.abort();
      if (copyHintTimer.current) window.clearTimeout(copyHintTimer.current);
    },
    [],
  );

  async function onRedeem(e: React.FormEvent) {
    e.preventDefault();
    await redeemCode(code);
  }

  async function onFetchCode() {
    if (!session?.id || listenPhase !== 'idle' || busy) return;
    setError(null);
    const grace = Math.max(30, session.cancelGraceSeconds ?? DEFAULT_LISTEN_SEC);
    // Listen at least `grace` seconds from click; never end before cancelEligibleAt
    // so「换号」appears only when upstream early-cancel window has also passed.
    let ends = Date.now() + grace * 1000;
    // The server's number lifetime is authoritative; the cancellation grace is
    // only the earliest change time, not the deadline for receiving an SMS.
    const deadline = parseServerTimeMs(session.numberExpiresAt);
    if (deadline != null) ends = deadline;
    listenEndsAtRef.current = ends;
    setListenRemain(Math.max(0, Math.ceil((ends - Date.now()) / 1000)));
    setPhase('listening');
  }

  async function onRetry() {
    if (!session?.id) return;
    setBusy(true);
    setError(null);
    setPhase('idle');
    listenEndsAtRef.current = null;
    setListenRemain(0);
    pollGenRef.current += 1;
    pollControllerRef.current?.abort();
    pollControllerRef.current = null;
    const gen = pollGenRef.current;
    const fromId = session.id;
    const prev = session;
    try {
      const { data } = await apiFetch<SessionView>(`/api/v1/sms/sessions/${fromId}/retry`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(sessionSecretRef.current ? { 'X-Session-Secret': sessionSecretRef.current } : {}),
        },
        body: '{}',
      });
      applySession(data, gen);
      // A pending provider read may return the original number. Only a new
      // acquisition should restart the first-code UI and its listening countdown.
      const newAcquisition = data.id !== fromId || data.phone !== prev.phone || data.attempt !== prev.attempt;
      if (data.status === 'PENDING_NUMBER' || (data.status === 'WAITING_CODE' && newAcquisition)) {
        setPhase('idle');
      } else if (data.numberInvalid || data.status === 'WAITING_CODE') {
        setPhase('exhausted');
      }
    } catch (err) {
      if (gen === pollGenRef.current) {
        setSession(prev);
        setPhase('exhausted');
        setError(err);
        void poll(fromId);
      }
    } finally {
      setBusy(false);
    }
  }

  async function onNextSms() {
    if (!session?.id || busy) return;
    setBusy(true);
    setError(null);
    pollGenRef.current += 1;
    pollControllerRef.current?.abort();
    pollControllerRef.current = null;
    const gen = pollGenRef.current;
    try {
      const { data } = await apiFetch<SessionView>(`/api/v1/sms/sessions/${session.id}/next-sms`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(sessionSecretRef.current ? { 'X-Session-Secret': sessionSecretRef.current } : {}),
        },
        body: '{}',
      });
      applySession(data, gen);
    } catch (err) {
      if (gen === pollGenRef.current) {
        setError(err);
      }
    } finally {
      setBusy(false);
    }
  }

  const phoneParts = useMemo(
    () => formatPhone(session?.phone || session?.phoneMasked),
    [session?.phone, session?.phoneMasked],
  );
  const attempt = session?.attempt ?? session?.attemptCount ?? 0;
  const maxAttempts = session?.maxAttempts ?? 3;
  const showForm = !hideCodeForm && (!autoStart || manualMode);
  const usedDone = session?.keyStatus === 'USED' || session?.status === 'SUCCESS';
  const displayedCodes = codesFromSession(session);
  const maxCodes = session?.maxCodes ?? 3;
  const keyDead = session?.keyStatus === 'FAILED' || session?.keyStatus === 'EXPIRED';
  const waitingAlive =
    Boolean(session) &&
    !usedDone &&
    !keyDead &&
    Boolean(phoneParts) &&
    session!.status === 'WAITING_CODE' &&
    !session!.numberInvalid;

  // Mutually exclusive: 获取验证码 XOR 换号.
  const showFetchButton = waitingAlive && listenPhase === 'idle';
  const showListening = waitingAlive && listenPhase === 'listening';
  const showRetryButton =
    Boolean(session) &&
    !usedDone &&
    !keyDead &&
    (listenPhase === 'exhausted' || Boolean(session!.numberInvalid)) &&
    attempt < maxAttempts;
  const showNextSmsButton = Boolean(session?.canRequestNextSms) && !session?.listeningNextSms;
  // Don't keep "监听中 (0s)" — once countdown hits 0, treat listen UI as ended until server finalizes.
  const showNextListening = Boolean(session?.listeningNextSms) && nextListenRemain > 0;
  const showNextListenEnded = Boolean(session?.listeningNextSms) && nextListenRemain <= 0;

  return (
    <div className="space-y-6">
      {showForm ? (
        <form
          onSubmit={onRedeem}
          className="service-panel flex flex-col gap-3"
        >
          <label
            htmlFor="sms-cdk-input"
            className="shrink-0 text-base font-semibold text-[var(--color-ink)] md:text-lg"
          >
            输入 CDK
          </label>
          <input
            id="sms-cdk-input"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            placeholder="粘贴购买获得的兑换码"
            className="min-w-0 flex-1 rounded-xl border-2 border-[var(--color-brand)]/35 bg-[var(--color-card)] px-4 py-3.5 text-base font-mono tracking-wide text-[var(--color-ink)] shadow-[inset_0_1px_2px_rgba(0,0,0,0.04)] outline-none ring-[var(--color-brand)]/20 placeholder:text-[var(--color-muted)] focus:border-[var(--color-brand)] focus:ring-4"
            autoComplete="off"
            spellCheck={false}
          />
          <button
            type="submit"
            disabled={busy || !code.trim()}
            className="shrink-0 rounded-xl bg-[var(--color-brand)] px-6 py-3.5 text-base font-semibold text-white disabled:opacity-50 md:min-w-[7.5rem]"
          >
            {busy ? '处理中…' : '开始接码'}
          </button>
        </form>
      ) : null}

      {booting ? (
        <p className="text-sm text-[var(--color-muted)]">正在验证 CDK 并获取手机号…</p>
      ) : null}

      {error ? (
        <div className="space-y-3 rounded-xl border border-[var(--color-danger)]/30 bg-[var(--color-danger-bg)] px-4 py-3">
          <UserFacingError err={error} />
          {deepLink.failed ? (
            <div className="space-y-2">
              <p className="text-sm text-[var(--color-ink)]">
                {deepLink.retryable
                  ? '本次是链接自动兑换，刚才的连接中断了。已开始的兑换不受影响——重新查询会接着原会话继续，不会重复租号。'
                  : '本次是链接自动兑换，但没有成功。可以改用手动输入重新填写卡密。'}
              </p>
              <div className="flex flex-wrap gap-2">
                {deepLink.retryable ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      setError(null);
                      void redeemCode(initialCode?.trim() || code.trim());
                    }}
                    className="rounded-xl bg-[var(--color-brand)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                  >
                    重新查询原兑换
                  </button>
                ) : null}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setError(null);
                    setDeepLink({ failed: false, retryable: false });
                    setManualMode(true);
                  }}
                  className="rounded-xl border border-[var(--color-border)] px-4 py-2 text-sm text-[var(--color-ink)] hover:bg-[var(--color-canvas)]"
                >
                  改为手动输入
                </button>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      {copyHint ? (
        <div className="rounded-xl border border-[var(--color-success)]/30 bg-[var(--color-success-bg)] px-4 py-2 text-sm text-[var(--color-success)]">
          {copyHint}
        </div>
      ) : null}

      {session ? (
        <div className="space-y-4 rounded-2xl border border-[var(--color-border)] bg-[var(--color-card)] p-5 shadow-[var(--shadow-card)]">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-base font-semibold text-[var(--color-ink)]">手机号与验证码</h2>
            <span className="text-xs text-[var(--color-muted)]">
              第 {attempt}/{maxAttempts} 次
            </span>
          </div>

          {session.smsService || session.smsCountry != null ? (
            <p className="text-sm text-[var(--color-muted)]">
              服务：{formatPublicServiceName(session.smsServiceName, session.smsService)}
              {' · '}
              国家：{session.smsCountryName || '—'}
            </p>
          ) : null}

          {(session.keyExpiresAt || session.numberExpiresAt || session.cancelEligibleAt) &&
          !usedDone ? (
            <ul className="space-y-1 text-xs text-[var(--color-muted)]">
              {session.keyExpiresAt ? (
                <li>卡密到期：{new Date(session.keyExpiresAt).toLocaleString()}</li>
              ) : null}
              {session.numberExpiresAt ? (
                <li>号码等待截止：{new Date(session.numberExpiresAt).toLocaleString()}</li>
              ) : null}
              {session.cancelEligibleAt && session.status === 'WAITING_CODE' ? (
                <li>最早可换号：{new Date(session.cancelEligibleAt).toLocaleString()}</li>
              ) : null}
              <li>
                剩余换号机会：
                {Math.max(
                  0,
                  (session.maxAttempts ?? maxAttempts) - (session.attemptCount ?? attempt),
                )}
              </li>
            </ul>
          ) : null}

          {usedDone ? (
            <p className="rounded-lg bg-[var(--color-success-bg)] px-3 py-2 text-sm text-[var(--color-success)]">
              该 CDK 已使用。以下为当时的手机号与验证码，可复制补填；不能再换号。
            </p>
          ) : null}

          {session.resultPending && !usedDone ? (
            <p className="rounded-lg bg-[var(--color-warning-bg)] px-3 py-2 text-sm text-[var(--color-warning)]">
              取号或取消结果确认中，请稍候。确认前请勿重复换号（当前剩余换号机会{' '}
              {Math.max(
                0,
                (session.maxAttempts ?? maxAttempts) - (session.attemptCount ?? attempt),
              )}{' '}
              次）。若长时间无更新，请联系客服并提供本页参考信息。
            </p>
          ) : null}

          {queryUnavailable && !usedDone ? (
            <p role="status" className="text-sm text-[var(--color-warning)]">
              验证码查询暂时失败，正在重试原号码。这不代表目标网站未发送短信，请勿重复取号。
            </p>
          ) : null}

          {!usedDone &&
          !session.resultPending &&
          !session.canRetry &&
          !session.numberInvalid &&
          attempt >= maxAttempts ? (
            <p className="rounded-lg bg-[var(--color-warning-bg)] px-3 py-2 text-sm text-[var(--color-warning)]">
              换号次数已用尽，无法继续取号。请联系客服处理。
            </p>
          ) : null}

          {session.numberInvalid && !usedDone && !session.resultPending ? (
            <p className="rounded-lg bg-[var(--color-warning-bg)] px-3 py-2 text-sm text-[var(--color-warning)]">
              号码已失效（超时或已取消）。若仍有换号次数，请点击下方「换号」；否则请联系客服。
            </p>
          ) : null}

          {phoneParts ? (
            <div>
              <div className="mb-1 text-xs text-[var(--color-muted)]">手机号</div>
              <div className="flex flex-wrap items-center gap-2">
                <div className="break-all font-mono text-2xl tracking-wide text-[var(--color-ink)] select-all">
                  {phoneParts.display}
                </div>
                <div className="relative" ref={phoneMenuRef}>
                  <button
                    type="button"
                    onClick={() => setPhoneMenuOpen((v) => !v)}
                    className="inline-flex items-center gap-1 rounded-lg border border-[var(--color-border)] px-2.5 py-1.5 text-xs text-[var(--color-ink)] hover:bg-[var(--color-canvas)]"
                    aria-haspopup="menu"
                    aria-expanded={phoneMenuOpen}
                  >
                    <CopyIcon className="h-3.5 w-3.5" />
                    复制
                  </button>
                  {phoneMenuOpen ? (
                    <div
                      role="menu"
                      className="absolute left-0 z-20 mt-1 min-w-[11rem] overflow-hidden rounded-xl border border-[var(--color-border)] bg-[var(--color-card)] py-1 shadow-lg"
                    >
                      <button
                        type="button"
                        role="menuitem"
                        className="block w-full px-3 py-2 text-left text-sm text-[var(--color-ink)] hover:bg-[var(--color-canvas)]"
                        onClick={() => void copyText(phoneParts.withCountry, '已复制（含国家号）')}
                      >
                        带国家号
                        <span className="mt-0.5 block font-mono text-xs text-[var(--color-muted)]">
                          {phoneParts.withCountry}
                        </span>
                      </button>
                      <button
                        type="button"
                        role="menuitem"
                        className="block w-full px-3 py-2 text-left text-sm text-[var(--color-ink)] hover:bg-[var(--color-canvas)]"
                        onClick={() =>
                          void copyText(phoneParts.withoutCountry, '已复制（区号+号码）')
                        }
                      >
                        仅区号+号码
                        <span className="mt-0.5 block font-mono text-xs text-[var(--color-muted)]">
                          {phoneParts.withoutCountry}
                        </span>
                      </button>
                    </div>
                  ) : null}
                </div>
              </div>
              {session.phoneHistory && session.phoneHistory.includes(',') ? (
                <p className="mt-2 text-xs text-[var(--color-muted)]">
                  使用记录：<span className="font-mono">{session.phoneHistory}</span>
                </p>
              ) : null}
              {!usedDone && !session.numberInvalid && listenPhase === 'idle' ? (
                <p className="mt-2 text-sm text-[var(--color-muted)]">
                  请将号码填到目标网站并触发短信，然后点击下方「获取验证码」。
                </p>
              ) : null}
              {showListening ? (
                <p className="mt-2 text-sm text-[var(--color-muted)]">
                  正在监听验证码（剩余约 {listenRemain}s）…有码会自动显示，无需反复点击。
                </p>
              ) : null}
              {listenPhase === 'exhausted' && !usedDone && !session.numberInvalid ? (
                <p className="mt-2 text-sm text-[var(--color-muted)]">
                  本轮等待已结束，仍在查询原号码结果；如可换号，可点击下方「换号」。
                </p>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-[var(--color-muted)]">正在分配号码…</p>
          )}

          {displayedCodes.length > 0 ? (
            <div className="space-y-3">
              <div className="mb-1 text-xs text-[var(--color-muted)]">
                验证码（{displayedCodes.length}/{maxCodes}）
              </div>
              {displayedCodes.map((c, i) => (
                <div key={`${c}-${i}`} className="flex flex-wrap items-center gap-2">
                  <span className="text-sm text-[var(--color-muted)]">
                    {CODE_MARKERS[i] ?? `${i + 1}.`}
                  </span>
                  <div className="font-mono text-2xl font-semibold tracking-widest text-[var(--color-brand)] select-all">
                    {c}
                  </div>
                  <button
                    type="button"
                    onClick={() => void copyText(c, `已复制验证码${CODE_MARKERS[i] ?? i + 1}`)}
                    className="inline-flex items-center gap-1 rounded-lg border border-[var(--color-border)] px-2.5 py-1.5 text-xs text-[var(--color-ink)] hover:bg-[var(--color-canvas)]"
                  >
                    <CopyIcon className="h-3.5 w-3.5" />
                    复制
                  </button>
                </div>
              ))}
              <p className="text-sm text-[var(--color-success)]">
                已收到验证码，CDK 已核销。请尽快填回目标网站；不能再换号。
              </p>
              {showNextListening ? (
                <p className="text-sm text-[var(--color-muted)]">
                  正在监听新短信（剩余约 {nextListenRemain}s）…有码会自动追加显示。
                </p>
              ) : null}
              {showNextListenEnded ? (
                <p className="text-sm text-[var(--color-muted)]">监听已结束，正在确认…</p>
              ) : null}
              {session.providerFinalized && !showNextSmsButton && !showNextListening ? (
                <p className="text-sm text-[var(--color-muted)]">
                  {displayedCodes.length >= maxCodes
                    ? '本号码验证码已达上限，接码已结束。'
                    : '未收到新短信或号码已过期，接码已结束。'}
                </p>
              ) : null}
            </div>
          ) : null}

          {session.keyStatus === 'FAILED' ? (
            <p className="text-sm text-[var(--color-danger)]">三次均未收到验证码，CDK 已作废。请联系客服售后。</p>
          ) : null}
          {session.keyStatus === 'EXPIRED' ? (
            <p className="text-sm text-[var(--color-warning)]">该 CDK 已过期。</p>
          ) : null}

          <div className="flex flex-wrap gap-2">
            {showFetchButton ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => void onFetchCode()}
                className="rounded-xl bg-[var(--color-brand)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
              >
                获取验证码
              </button>
            ) : null}
            {showListening ? (
              <button
                type="button"
                disabled
                className="rounded-xl bg-[var(--color-brand)] px-4 py-2 text-sm font-semibold text-white opacity-70"
              >
                监听中（{listenRemain}s）
              </button>
            ) : null}
            {showNextSmsButton ? (
              <button
                type="button"
                disabled={busy}
                onClick={() => void onNextSms()}
                className="rounded-xl bg-[var(--color-brand)] px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
              >
                获取新短信（{displayedCodes.length}/{maxCodes}）
              </button>
            ) : null}
            {showNextListening ? (
              <button
                type="button"
                disabled
                className="rounded-xl bg-[var(--color-brand)] px-4 py-2 text-sm font-semibold text-white opacity-70"
              >
                监听新短信（{nextListenRemain}s）
              </button>
            ) : null}
            {showRetryButton ? (
              <button
                type="button"
                disabled={busy || (!session.canRetry && !session.numberInvalid)}
                onClick={() => void onRetry()}
                className="rounded-xl border border-[var(--color-border)] px-4 py-2 text-sm text-[var(--color-ink)] hover:bg-[var(--color-canvas)] disabled:opacity-50"
              >
                换号（{attempt}/{maxAttempts}）
              </button>
            ) : null}
          </div>
          {showNextSmsButton ? (
            <p className="text-xs text-[var(--color-muted)]">
              若目标网站需再发一次验证码，点击「获取新短信」后在本号码上等待约 5 分钟。
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
