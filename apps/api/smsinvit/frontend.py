"""Static SPA boundaries and a deployment-owned, relocatable admin entry."""
from pathlib import Path
import re

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .errors import Error

PUBLIC_PAGES = {'', 'supprot_sim', 'supprot_codex', 'invitation'}
RESERVED = PUBLIC_PAGES | {'api', 'assets', 'health', 'favicon.svg', 'brand-mark.svg'}


def admin_path(config):
    raw = str(config.get('smsinvit.admin-path', '/admin/'))
    if not re.fullmatch(r'/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/?', raw):
        raise ValueError('Admin path must be one /path/ segment of 1–64 letters, digits, underscores or hyphens')
    if raw.strip('/').lower() in RESERVED:
        raise ValueError('Admin path conflicts with a public or API route')
    return raw.rstrip('/') + '/'


def is_admin_path(path, base):
    return path == base.rstrip('/') or path.startswith(base)


def register_frontend(app, config, base):
    prefix = base.strip('/')
    static = Path(str(config.get('SMSINVIT_STATIC_DIR', Path(__file__).resolve().parents[2] / 'static')))

    @app.get(base + 'invitation/native', include_in_schema=False)
    @app.get(base + 'invitation/native/', include_in_schema=False)
    def retired_home():
        return RedirectResponse(base + 'invitation', status_code=308, headers={'Cache-Control': 'no-store'})

    @app.get(base + 'invitation/native/{path:path}', include_in_schema=False)
    def retired_assets(path: str):
        raise Error('NOT_FOUND', '资源不存在。', 404)

    if not static.exists():
        return

    root = static.resolve()
    admin_root = (root / 'admin').resolve()

    def missing():
        raise Error('NOT_FOUND', '页面不存在。', 404)

    def safe_target(directory, relative):
        if any(part in ('.', '..') for part in relative.split('/')) or any(c in relative for c in ('\\', '\0', '%')):
            missing()
        target = (directory / relative).resolve()
        if directory != target and directory not in target.parents:
            missing()
        return target

    def admin_html():
        index = admin_root / 'index.html'
        if not index.is_file():
            missing()
        # base contains only startup-validated ASCII path characters. Insertion
        # precedes every asset reference and adds no executable inline script.
        markup = index.read_text(encoding='utf-8')
        tag = f'<base href="{base}" data-admin-base>'
        if re.search(r'<head(?:\s[^>]*)?>', markup, re.I):
            markup = re.sub(r'(<head(?:\s[^>]*)?>)', lambda m: m[0] + tag, markup, count=1, flags=re.I)
        else:
            markup = tag + markup
        return HTMLResponse(markup, headers={'Cache-Control': 'no-store', 'X-Robots-Tag': 'noindex, nofollow'})

    @app.get('/{path:path}', include_in_schema=False)
    def static_file(request: Request, path: str):
        if path == prefix:
            suffix = ('?' + request.url.query) if request.url.query else ''
            return RedirectResponse(base + suffix, status_code=308, headers={'Cache-Control': 'no-store'})
        if path.startswith(prefix + '/'):
            relative = path[len(prefix) + 1:]
            target = safe_target(admin_root, relative)
            if relative in ('', 'index.html'):
                return admin_html()
            if relative.startswith('assets/'):
                if target.is_file():
                    return FileResponse(target, headers={'Cache-Control': 'public, max-age=31536000, immutable'})
                missing()
            if '.' in relative.rsplit('/', 1)[-1]:
                missing()
            return admin_html()
        # Reject the on-disk admin tree before checking for real files. A custom
        # entry must not leave /admin/index.html or its bundles reachable.
        if path == 'admin' or path.startswith(('admin/', 'api/')):
            missing()
        target = safe_target(root, path)
        if target == admin_root or admin_root in target.parents:
            missing()
        if path.startswith('assets/') or path in ('favicon.svg', 'brand-mark.svg'):
            if target.is_file():
                return FileResponse(target)
            missing()
        if path.rstrip('/') in PUBLIC_PAGES or path.startswith('supprot_sim/'):
            index = root / 'index.html'
            if index.is_file():
                return FileResponse(index, headers={'Cache-Control': 'no-store'})
        missing()
