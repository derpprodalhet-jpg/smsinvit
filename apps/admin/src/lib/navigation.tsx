import { useSyncExternalStore, type AnchorHTMLAttributes } from 'react';
// The server inserts the deployment-owned base before any asset reference.
// The same immutable bundle works under /admin/ and a custom entry.
const configuredBase =
  typeof document === 'undefined'
    ? '/admin/'
    : document.querySelector('base[data-admin-base]')?.getAttribute('href') || '/admin/';
const BASE = /^\/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\/$/.test(configuredBase)
  ? configuredBase.slice(0, -1)
  : '/admin';
function withinBase(value: string) {
  const pathname = value.split(/[?#]/, 1)[0];
  return pathname === BASE || pathname.startsWith(`${BASE}/`);
}
const event = 'smsinvit:navigate';
function path() {
  return withinBase(window.location.pathname)
    ? window.location.pathname.slice(BASE.length) || '/'
    : '/';
}
function subscribe(callback: () => void) {
  window.addEventListener('popstate', callback);
  window.addEventListener(event, callback);
  return () => {
    window.removeEventListener('popstate', callback);
    window.removeEventListener(event, callback);
  };
}
export function adminHref(href: string) {
  if (!href.startsWith('/') || href.startsWith('//') || withinBase(href)) return href;
  return `${BASE}${href}`;
}
function navigate(href: string, replace = false) {
  const target = adminHref(href);
  if (!withinBase(target)) {
    window.location.assign(target);
    return;
  }
  window.history[replace ? 'replaceState' : 'pushState'](null, '', target);
  window.dispatchEvent(new Event(event));
  window.scrollTo(0, 0);
}
const router = {
  push: (href: string) => navigate(href),
  replace: (href: string) => navigate(href, true),
  back: () => window.history.back(),
  refresh: () => window.location.reload(),
};
export function useRouter() {
  return router;
}
export function usePathname() {
  return useSyncExternalStore(subscribe, path, path);
}
export function Link({
  href = '/',
  children,
  onClick,
  ...props
}: AnchorHTMLAttributes<HTMLAnchorElement>) {
  return (
    <a
      {...props}
      href={adminHref(href)}
      onClick={(e) => {
        onClick?.(e);
        if (
          e.defaultPrevented ||
          e.button !== 0 ||
          e.metaKey ||
          e.ctrlKey ||
          e.shiftKey ||
          e.altKey ||
          props.target === '_blank' ||
          props.download
        )
          return;
        if (!href.startsWith('/') || href.startsWith('//')) return;
        e.preventDefault();
        navigate(href);
      }}
    >
      {children}
    </a>
  );
}
