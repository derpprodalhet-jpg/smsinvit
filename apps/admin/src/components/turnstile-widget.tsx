import { useEffect, useId, useRef } from 'react';
import { apiFetch } from '../lib/api';

declare global {
  interface Window {
    turnstile?: {
      render: (
        el: HTMLElement,
        opts: {
          sitekey: string;
          theme?: 'auto';
          size?: 'flexible';
          callback: (token: string) => void;
          'expired-callback': () => void;
          'error-callback': (code: string) => void;
          'before-interactive-callback': () => void;
          'after-interactive-callback': () => void;
          'unsupported-callback': () => void;
          retry: 'auto';
          'retry-interval': number;
          'refresh-expired': 'auto';
          'refresh-timeout': 'auto';
          'timeout-callback': () => void;
        },
      ) => string;
      remove: (id: string) => void;
    };
  }
}

/** Bound Cloudflare's loading/spinner time, while allowing visitors time to solve a ready challenge. */
export function TurnstileWidget({
  onToken,
  onEnabledChange,
  resetKey = 0,
}: {
  onToken: (token: string) => void;
  onEnabledChange?: (enabled: boolean) => void;
  resetKey?: number;
}) {
  const hostId = useId();
  const host = useRef<HTMLDivElement>(null);
  const callbacks = useRef({ onToken, onEnabledChange });
  callbacks.current = { onToken, onEnabledChange };

  useEffect(() => {
    let closed = false;
    let widgetId: string | undefined;
    const abort = new AbortController();
    const startedAt = Date.now();
    let fallbackMilliseconds = 5000;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let hasToken = false;
    let script: HTMLScriptElement | null = null;
    let poller: ReturnType<typeof setInterval> | undefined;
    const clearDeadline = () => {
      clearTimeout(timer);
      timer = undefined;
    };
    const removeWidget = () => {
      if (widgetId === undefined) return;
      try {
        window.turnstile?.remove(widgetId);
      } catch {
        /* already removed */
      }
      widgetId = undefined;
    };
    const fallback = () => {
      if (closed) return;
      closed = true;
      clearDeadline();
      clearInterval(poller);
      abort.abort();
      window.removeEventListener('smsinvit:captcha-fallback', fallback);
      script?.removeEventListener('error', fallback);
      removeWidget();
      callbacks.current.onEnabledChange?.(false);
      callbacks.current.onToken('');
    };
    const armDeadline = (milliseconds: number) => {
      clearDeadline();
      if (milliseconds <= 0) {
        fallback();
        return;
      }
      timer = setTimeout(fallback, milliseconds);
    };
    const invalidateToken = () => {
      if (closed) return;
      hasToken = false;
      callbacks.current.onToken('');
      // Expiry starts a fresh wait; retries must not extend an already pending deadline.
      if (timer === undefined) armDeadline(fallbackMilliseconds);
    };
    armDeadline(fallbackMilliseconds);
    window.addEventListener('smsinvit:captcha-fallback', fallback);
    void (async () => {
      try {
        const { data } = await apiFetch<{
          enabled: boolean;
          siteKey: string;
          fallbackSeconds?: number;
        }>('/api/v1/security/turnstile', { cache: 'no-store', signal: abort.signal });
        if (closed) return;
        if (!data.enabled || !data.siteKey) {
          fallback();
          return;
        }
        if (
          typeof data.fallbackSeconds === 'number' &&
          Number.isInteger(data.fallbackSeconds) &&
          data.fallbackSeconds >= 1 &&
          data.fallbackSeconds <= 60
        ) {
          fallbackMilliseconds = data.fallbackSeconds * 1000;
        }
        // Rendering an iframe does not mean it is ready: keep timing until interaction or success.
        armDeadline(fallbackMilliseconds - (Date.now() - startedAt));
        if (closed) return;
        callbacks.current.onEnabledChange?.(true);
        const render = () => {
          if (closed || !window.turnstile || !host.current || widgetId !== undefined) return;
          clearInterval(poller);
          try {
            widgetId = window.turnstile.render(host.current, {
              sitekey: data.siteKey,
              theme: 'auto',
              size: 'flexible',
              retry: 'auto',
              'retry-interval': 8000,
              'refresh-expired': 'auto',
              'refresh-timeout': 'auto',
              callback: (token) => {
                if (closed || !token.trim()) return;
                clearDeadline();
                hasToken = true;
                failures = 0;
                callbacks.current.onToken(token);
              },
              'expired-callback': invalidateToken,
              'timeout-callback': invalidateToken,
              'before-interactive-callback': () => {
                if (closed) return;
                clearDeadline();
                failures = 0;
              },
              'after-interactive-callback': () => {
                if (closed || hasToken) return;
                if (timer === undefined) armDeadline(fallbackMilliseconds);
              },
              'unsupported-callback': fallback,
              'error-callback': (code) => {
                if (closed) return;
                invalidateToken();
                // Configuration failures cannot recover by retrying. Network/challenge failures can.
                const permanent = [
                  '110100',
                  '110110',
                  '110200',
                  '200100',
                  '400020',
                  '400021',
                  '400070',
                ];
                if (permanent.includes(code)) {
                  fallback();
                  return;
                }
                // Expiry can refresh within the remaining loading budget.
                if (code === '110600' || code === '110620') return;
                if (++failures >= 3) fallback();
              },
            });
            if (closed) removeWidget();
          } catch {
            fallback();
          }
        };
        if (!window.turnstile) {
          script = document.querySelector<HTMLScriptElement>('script[data-smsinvit-turnstile]');
          if (!script) {
            script = document.createElement('script');
            script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
            script.async = true;
            script.dataset.smsinvitTurnstile = '1';
            script.addEventListener('error', fallback);
            document.head.appendChild(script);
          } else script.addEventListener('error', fallback);
          // Readiness is checked independently of load: CSP/blockers may never fire load/error.
          poller = setInterval(render, 100);
        }
        render();
      } catch {
        fallback();
      }
    })();
    return () => {
      closed = true;
      abort.abort();
      clearDeadline();
      clearInterval(poller);
      window.removeEventListener('smsinvit:captcha-fallback', fallback);
      script?.removeEventListener('error', fallback);
      removeWidget();
    };
  }, [resetKey]);

  return <div id={hostId} ref={host} />;
}
