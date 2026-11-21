/** Public progress never describes internal work or advances on a local timer. */
export function NumericProgress({ value, label, success = false, failed = false }: {
  value?: number | null;
  label: string;
  success?: boolean;
  failed?: boolean;
}) {
  const percent = success ? 100 : Math.max(0, Math.min(95, Number.isFinite(value) ? Math.round(value!) : 0));
  return (
    <div className={`numeric-progress${failed ? ' numeric-progress-failed' : ''}`}>
      <span className="numeric-progress-value" aria-hidden="true">{percent}%</span>
      <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent} className="numeric-progress-track">
        <div className="numeric-progress-fill" style={{ transform: `scaleX(${percent / 100})` }} />
      </div>
    </div>
  );
}

import { useEffect, useRef, type ReactNode } from 'react';

/** 在固定宽度容器内自动缩小字号，避免步骤标题在窄屏溢出。 */
function FitStepLabel({ text, active }: { text: string; active: boolean }) {
  const ref = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const parent = el.parentElement;
    if (!parent) return;

    const fit = () => {
      const max = window.matchMedia('(min-width: 640px)').matches ? 16 : 14;
      // 下限从 9px 提到 11px（审查 8.4）：9px 的中文在实际设备上基本无法阅读，
      // 与其把说明缩到看不清，不如让布局多留一点高度。
      const min = 11;
      let size = max;
      el.style.fontSize = `${size}px`;
      el.style.whiteSpace = 'nowrap';
      while (size > min && el.scrollWidth > parent.clientWidth) {
        size -= 0.5;
        el.style.fontSize = `${size}px`;
      }
      if (el.scrollWidth > parent.clientWidth) {
        el.style.whiteSpace = 'normal';
        el.style.lineHeight = '1.15';
        while (size > min && el.scrollHeight > parent.clientHeight) {
          size -= 0.5;
          el.style.fontSize = `${size}px`;
        }
      }
    };

    fit();
    const ro = new ResizeObserver(fit);
    ro.observe(parent);
    window.addEventListener('resize', fit);
    return () => {
      ro.disconnect();
      window.removeEventListener('resize', fit);
    };
  }, [text]);

  return (
    <span
      ref={ref}
      className={`block w-full text-center font-semibold leading-tight ${
        active ? 'text-[var(--color-ink)]' : 'text-[var(--color-muted)]'
      }`}
    >
      {text}
    </span>
  );
}

/** 四步卡片：序号实心圆圈 + 卡片边框；完成打勾，当前青色高亮，未到变灰。 */
export function RedemptionSteps({ current, labels, ariaLabel = '操作步骤' }: {
  current: number;
  labels: readonly string[];
  ariaLabel?: string;
}) {
  const nodes: ReactNode[] = [];
  labels.forEach((label, i) => {
    const n = i + 1;
    const active = n === current;
    const done = n < current;
    nodes.push(
      <li key={`step-${n}`} className="min-w-0" aria-current={active ? 'step' : undefined}>
        <div
          // 审查 8.1：窄屏先把高度收紧，把首屏让给输入框和主按钮。仍是四张带序号圆圈的
          // 可视卡片（触控目标 ≥44px、圆圈保持可见），只是不再占掉两行卡片的高空间。
          className={`flex h-full min-h-[4.75rem] w-full min-w-0 flex-col items-center gap-1.5 rounded-2xl border-2 px-1.5 py-2.5 sm:min-h-[7.25rem] sm:gap-2 sm:px-3 sm:py-5 ${
            active
              ? 'border-[var(--color-brand)] bg-[var(--color-brand)]/10 shadow-[var(--shadow-card)]'
              : done
                ? 'border-[var(--color-brand)]/45 bg-[var(--color-card)]'
                : 'border-[var(--color-border)] bg-[var(--color-card)] opacity-65'
          }`}
        >
          <span
            className={`inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-sm font-bold sm:h-10 sm:w-10 sm:text-base ${
              active || done
                ? 'bg-[var(--color-brand)] text-[var(--color-canvas)]'
                : 'bg-[var(--color-canvas)] text-[var(--color-muted)]'
            }`}
            aria-hidden="true"
          >
            {done ? '✓' : n}
          </span>
          <div className="flex h-7 w-full min-w-0 items-center justify-center overflow-hidden sm:h-9">
            <FitStepLabel text={label} active={active} />
          </div>
        </div>
      </li>,
    );
    if (i < labels.length - 1) {
      nodes.push(
        <li
          key={`line-${n}`}
          aria-hidden="true"
          // 窄屏是 2×2，没有"下一步"方向，连接线只在单排布局（sm 起）出现。
          className={`hidden w-2 shrink-0 items-center self-center sm:flex sm:w-4 ${
            done ? 'text-[var(--color-brand)]' : 'text-[var(--color-border)]'
          }`}
        >
          <span
            className={`block h-0.5 w-full ${done ? 'bg-[var(--color-brand)]' : 'bg-[var(--color-border)]'}`}
          />
        </li>,
      );
    }
  });

  return (
    <ol
      className="steps-rail grid w-full items-stretch gap-x-0"
      aria-label={ariaLabel}
    >
      {nodes}
    </ol>
  );
}
