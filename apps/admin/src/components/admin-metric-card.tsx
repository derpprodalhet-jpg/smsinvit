import { Typography } from 'antd';
import { Link } from '../lib/navigation';
import type { CSSProperties, ReactNode } from 'react';
import type { AdminTone } from './admin-feedback';

const ICON_BG: Record<AdminTone, string> = {
  default: '#eef3f4',
  success: '#e8f6ef',
  warning: '#fff7e8',
  error: '#fdecec',
  processing: '#e6f2f2',
};

const ICON_FG: Record<AdminTone, string> = {
  default: '#5c6b7a',
  success: '#1f7a4d',
  warning: '#b7791f',
  error: '#c0392b',
  processing: '#2a7a7b',
};

/** Independent metric tile — 16px radius, icon chip, tabular value (§7). */
export function AdminMetricCard({
  title,
  value,
  description,
  icon,
  tone = 'default',
  href,
  style,
}: {
  title: ReactNode;
  value?: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  tone?: AdminTone;
  href?: string;
  style?: CSSProperties;
}) {
  const body = (
    <div className="admin-metric-card" style={style}>
      <div className="admin-metric-card__head">
        {icon ? (
          <span
            className="admin-metric-card__icon"
            style={{ background: ICON_BG[tone], color: ICON_FG[tone] }}
            aria-hidden
          >
            {icon}
          </span>
        ) : null}
        <Typography.Text className="admin-metric-card__title">{title}</Typography.Text>
      </div>
      <div className="admin-metric-card__value">{value == null || value === '' ? '—' : value}</div>
      {description ? <div className="admin-metric-card__desc">{description}</div> : null}
    </div>
  );

  if (href) {
    return (
      <Link className="admin-metric-card__link" href={href}>
        {body}
      </Link>
    );
  }
  return body;
}

/** Page-level secondary bar: hint text + actions (no fake date filters). */
export function AdminPageToolbar({
  hint,
  actions,
  style,
}: {
  hint?: ReactNode;
  actions?: ReactNode;
  style?: CSSProperties;
}) {
  if (!hint && !actions) return null;
  return (
    <div className="admin-page-toolbar" style={style}>
      <div className="admin-page-toolbar__hint">{hint}</div>
      {actions ? <div className="admin-page-toolbar__actions">{actions}</div> : null}
    </div>
  );
}
