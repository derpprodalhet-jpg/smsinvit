import { useEffect, useId, useRef, useState } from 'react';
import { Button, Input, Typography } from 'antd';
import { fetchLoginCaptcha, type CaptchaChallenge } from '../lib/auth';

export type AdminBotPayload = {
  captchaToken: string;
  captchaAnswer: string;
  ready: boolean;
};

/** Every submitted challenge is single-use, even if the password was incorrect. */
export function AdminBotGuard({
  resetKey = 0,
  onChange,
}: {
  resetKey?: number;
  onChange: (payload: AdminBotPayload) => void;
}) {
  const id = useId();
  const [captcha, setCaptcha] = useState<CaptchaChallenge | null>(null);
  const [answer, setAnswer] = useState('');
  const [loading, setLoading] = useState(true);
  const [expired, setExpired] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    let cancelled = false;
    let expiry: ReturnType<typeof setTimeout> | undefined;
    setCaptcha(null);
    setAnswer('');
    setLoading(true);
    setExpired(false);
    onChangeRef.current({ captchaToken: '', captchaAnswer: '', ready: false });
    void fetchLoginCaptcha()
      .then((data) => {
        if (cancelled) return;
        setCaptcha(data);
        expiry = setTimeout(
          () => {
            setExpired(true);
            setAnswer('');
            onChangeRef.current({ captchaToken: '', captchaAnswer: '', ready: false });
          },
          Math.max(0, data.expiresInSeconds) * 1000,
        );
      })
      .catch(() => {
        if (!cancelled) setCaptcha(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      if (expiry !== undefined) clearTimeout(expiry);
    };
  }, [resetKey, refresh]);

  return (
    <div style={{ marginBottom: 24 }}>
      <label htmlFor={id} style={{ display: 'block', marginBottom: 8 }}>
        计算验证码
      </label>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
        <Button
          type="default"
          htmlType="button"
          disabled={loading}
          aria-label="刷新计算验证码"
          onClick={() => {
            onChangeRef.current({ captchaToken: '', captchaAnswer: '', ready: false });
            setRefresh((value) => value + 1);
          }}
          style={{ minWidth: '48%', minHeight: 44, fontWeight: 600 }}
        >
          {loading
            ? '加载中…'
            : expired
              ? '已过期，点击刷新'
              : captcha?.prompt || '加载失败，点击重试'}
        </Button>
        <Input
          id={id}
          size="large"
          value={answer}
          disabled={loading || expired || !captcha}
          onChange={(event) => {
            const value = event.target.value;
            setAnswer(value);
            onChangeRef.current({
              captchaToken: captcha?.captchaToken || '',
              captchaAnswer: value,
              ready: Boolean(captcha && !expired && /^\d{1,3}$/.test(value.trim())),
            });
          }}
          placeholder="计算结果"
          inputMode="numeric"
          maxLength={3}
          autoComplete="off"
          style={{ minHeight: 44 }}
        />
      </div>
      <Typography.Text type="secondary" style={{ fontSize: 12 }}>
        点击算式可换一道题，验证码在提交后失效。
      </Typography.Text>
    </div>
  );
}
