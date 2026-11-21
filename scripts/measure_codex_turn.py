#!/usr/bin/env python3
"""测量 Codex CLI「say hi」单轮耗时：模型 × 出口（直连/代理）× lean 档。

为什么要专用工具：同一份 ~10039 token 输入的生产实测是 10.1s / 18.3s / 29.8s，
单次样本没有统计意义。这里**逐轮交替**跑各臂并给 median，才能回答"哪个配置更快"。

用法（在 api 镜像里跑，源码是只读挂载）：

    # 1) 主动测量某个配置
    docker exec -i smsinvit-desktop-api-1 python /app/scripts/measure_codex_turn.py \
        --email <已兑换完的处置账号> \
        --models gpt-5.5,gpt-5.6-luna,gpt-5.6-sol,gpt-5.6-terra \
        --arms direct --runs 5

    # 2) 复核**真实兑换**的 CLI 耗时（只读日志，不消耗任何东西）
    docker exec -i smsinvit-desktop-api-1 python /app/scripts/measure_codex_turn.py --check-logs

约定：
  · 只用 `invitation_v2_codex_auth` 里**已经兑现成功**（cli_completed_at 非空）的
    处置账号，不占业务配额，也不需要 CDK/接码；
  · 跑的是真实代码路径 `webui.codex_turn.run_codex_turn`（与生产同一份实现）；
  · codex 在私有 CODEX_HOME 里轮换的凭据会**回写数据库**，否则测几次会把账号测废；
  · CODEX_HOME 由 run_codex_turn 自己建在自己清理，本脚本不额外留目录。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace

VENDOR = Path(__file__).resolve().parents[1] / 'vendor' / 'codex_invit'
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

# registrar / codex_cli 在**模块级**读这两个环境变量（与 InvitationService 一致），
# 所以必须在 import webui.* 之前设好，否则 import 直接 KeyError。
DATA_DIR = Path(os.environ.get('SMSINVIT_INVITATION_DATA_DIR') or '/app/runtime/invitation')
os.environ.setdefault('SMSINVIT_INVITATION_LOG_DIR', str(DATA_DIR / 'logs'))
os.environ.setdefault('CODEX_HOMES_ROOT', str(DATA_DIR / 'codex-homes'))


def bootstrap():
    """连上邀请库；返回 (db, codex_turn, codex_auth, proxy_pool)。"""
    from smsinvit.config import Config
    from smsinvit.db import Database
    from smsinvit.invitation import mysql
    from smsinvit.security import SecretCodec
    config = Config.from_env()
    db = Database(config.database_url(), connect_args=config.database_connect_args())
    codec = SecretCodec(config.get('smsinvit.inventory-encryption-key'),
                        config.get('smsinvit.inventory-fingerprint-key'))
    mysql._services = None
    mysql._runtime_owner = None
    mysql._cache = {}
    mysql._redaction_values = set()
    mysql.configure(SimpleNamespace(db=db, codec=codec))
    from webui import codex_auth, codex_turn, proxy_pool
    return db, codex_turn, codex_auth, proxy_pool


def load_creds(db, email: str) -> dict:
    from smsinvit.invitation import mysql
    row = db.one('SELECT * FROM invitation_v2_codex_auth WHERE email=?', email)
    if not row:
        raise SystemExit(f'账号不在 codex_auth 表里：{email}')
    if not row.get('cli_completed_at'):
        raise SystemExit(f'拒绝用未兑现的账号测量（会影响业务）：{email}')
    creds = {name: mysql.revealed(row.get(name)) for name in
             ('access_token', 'refresh_token', 'id_token', 'account_id')}
    if not creds.get('access_token'):
        raise SystemExit(f'账号凭据不可用：{email}')
    return creds


def save_rotated(email: str, creds: dict, tokens: dict) -> None:
    """把 codex 轮换后的凭据回写，避免把处置账号测废。

    写库用的是邀请引擎自己的 `webui.db.save_codex_auth`（它会走机密列加密），
    不是 api 层的 Database 对象。
    """
    from webui import db as invitation_db

    merged = {key: tokens.get(key) or creds.get(key) or ''
              for key in ('access_token', 'refresh_token', 'id_token', 'account_id')}
    invitation_db.save_codex_auth(email, **merged,
                                  last_refresh=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    creds.update(merged)


def run_arm(codex_turn, *, email: str, creds: dict, model: str, prompt: str,
            proxy: str, lean: bool, timeout: int) -> dict:
    overrides = {
        'CODEX_REDEEM_MODEL': model,
        'CODEX_REDEEM_PROMPT': prompt,
        'CODEX_REDEEM_TIMEOUT': str(timeout),
    }
    if lean:
        overrides['CODEX_TURN_LEAN'] = '1'
    started = time.time()
    res = codex_turn.run_codex_turn(
        email=email,
        access_token=creds['access_token'],
        refresh_token=creds['refresh_token'],
        id_token=creds.get('id_token') or '',
        proxy=proxy,
        overrides=overrides,
    )
    marks = res.get('breakdown') or {}
    usage = res.get('usage') or {}

    def sec(key):
        value = marks.get(key)
        return round(int(value) / 1000.0, 1) if value is not None else None

    return {
        'ok': bool(res.get('ok')),
        'elapsed': round(time.time() - started, 1),
        'startup': sec('thread_ms'),
        'model_phase': (round((int(marks['completed_ms']) - int(marks['turn_ms'])) / 1000.0, 1)
                        if marks.get('completed_ms') is not None and marks.get('turn_ms') is not None
                        else None),
        'teardown': (round((int(marks['exit_ms']) - int(marks['completed_ms'])) / 1000.0, 1)
                     if marks.get('completed_ms') is not None and marks.get('exit_ms') is not None
                     else None),
        'in': usage.get('input_tokens'),
        'out': usage.get('output_tokens'),
        'reason': usage.get('reasoning_output_tokens'),
        'ws_retries': marks.get('ws_retries', 0),
        'reply': str(res.get('reply') or '')[:24],
        'error': str(res.get('error') or '')[:160],
        'tokens': {k: res.get(k) for k in
                   ('access_token', 'refresh_token', 'id_token', 'account_id') if res.get(k)},
    }


def median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 1) if values else None


def run_production_path(db, codex_auth, emails, creds_by_email, args) -> int:
    """走生产边界 `_complete_cli`（不是直接调 run_codex_turn）。

    这样验证的是**真实生效的默认值**：库里没有 `sayhi_direct` 行 → 直连；
    `sayhi_model` 行为空 → `codex_turn.DEFAULT_SAYHI_MODEL`。跑完照常回写轮换凭据。
    """
    from smsinvit.invitation import mysql
    from smsinvit.invitation.runtime import TASK_SETTINGS

    settings = {row['key']: mysql.revealed(row['value'], sensitive=mysql.is_secret_setting(row['key']))
                for row in db.all('SELECT `key`,value FROM invitation_v2_settings')}
    print(f'生产路径验收：账号 {emails[0]} / sayhi_direct 行={settings.get("sayhi_direct", "<缺失>")} '
          f'/ sayhi_model 行={settings.get("sayhi_model", "<缺失>")!r}')
    token = TASK_SETTINGS.set(settings)      # 与 runtime.py 冻结设置的方式一致
    try:
        for email in emails:
            started = time.time()
            try:
                result = codex_auth._complete_cli(email, dict(creds_by_email[email]),
                                                  proxy=args.proxy or '', job_id='measure',
                                                  on_stage=None)
            except Exception as exc:  # noqa: BLE001  生产边界会把失败抛成 JobFailure
                print('%-22s %.1fs 失败：%s' % (email.split('@')[0], time.time() - started,
                                              f'{type(exc).__name__}: {exc}'[:160]), flush=True)
                continue
            print('%-22s %.1fs ok=%s cli_elapsed=%s export=%s'
                  % (email.split('@')[0], time.time() - started, result.get('account_id') != '',
                     result.get('cli_elapsed'), (result.get('export') or {}).get('ok')))
    finally:
        TASK_SETTINGS.reset(token)
    return 0


def check_logs(log_dir: Path, tail: int) -> int:
    """只读复核：把真实兑换的 `[codex_turn]` 记录整成一张表（不碰 DB、不发请求）。

    主人问"现在还是慢吗"时，这一条命令就能回答：看最近几轮的 `出口` 与 `合计`。
    """
    import glob
    import re

    pattern = re.compile(
        r'^(\d{2}:\d{2}:\d{2}) \[INFO\] codex_turn: \[codex_turn\] '
        r'(?:跑一轮兑现：(?P<email>[^（]+)（模型 (?P<model>[^，]+)，身份 [^，]+，代理 (?P<egress>有|无)）'
        r'|兑现成功（(?P<ok>[0-9.]+)s）：(?P<reply>.*)'
        r'|明细: 启动 (?P<startup>[0-9.]+|-)s / 模型 (?P<model_phase>[0-9.]+|-)s / '
        r'收尾 (?P<teardown>[0-9.]+|-)s / 合计 (?P<total>[0-9.]+)s / '
        r'in=(?P<in>[0-9-]+) out=(?P<out>[0-9-]+) reason=(?P<reason>[0-9-]+) / WS重试 (?P<ws>\d+))')
    rows = []
    # 按**文件修改时间**排序：按路径排序时新日志可能排在很前面，tail 会取到旧记录。
    for path in sorted(glob.glob(str(log_dir / '*.log')),
                       key=lambda p: Path(p).stat().st_mtime if Path(p).exists() else 0):
        try:
            lines = Path(path).read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            match = pattern.match(line.strip())
            if not match:
                continue
            if match.group('email'):
                rows.append({'time': match.group(1), 'email': match.group('email').strip(),
                             'model': match.group('model'), 'egress': match.group('egress')})
            elif rows:
                rows[-1].update({key: match.group(key) for key in
                                 ('startup', 'model_phase', 'teardown', 'total',
                                  'in', 'out', 'reason', 'ws') if match.group(key) is not None})
    rows = [row for row in rows if row.get('total')]
    if not rows:
        print(f'{log_dir} 里还没有带 "明细" 的 codex_turn 记录（说明改版后还没跑过真实兑换）')
        return 0
    print('%-10s %-22s %-16s %-5s %6s %6s %6s %7s %7s %s'
          % ('时间', '账号', '模型', '出口', '启动', '模型段', '收尾', '合计', 'in', 'WS重试'))
    for row in rows[-max(1, tail):]:
        print('%-10s %-22s %-16s %-5s %6s %6s %6s %7s %7s %s'
              % (row.get('time'), row.get('email', '')[:22], row.get('model', ''),
                 '直连' if row.get('egress') == '无' else '代理',
                 row.get('startup'), row.get('model_phase'), row.get('teardown'),
                 row.get('total'), row.get('in'), row.get('ws')))
    return 0


def main() -> int:
    # codex_turn / codex_auth 的分段耗时是 INFO 级：不配置日志就等于把证据丢掉。
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--email', action='append',
                        help='已兑现完的处置账号；可重复或逗号分隔（会轮换使用）')
    parser.add_argument('--check-logs', action='store_true',
                        help='只读复核真实兑换的 CLI 耗时（不需要 --email、不碰 DB）')
    parser.add_argument('--log-dir', default=str(DATA_DIR / 'logs'))
    parser.add_argument('--tail', type=int, default=8, help='--check-logs 显示最近几条')
    parser.add_argument('--models', default='gpt-5.5',
                        help='逗号分隔，例如 gpt-5.5,gpt-5.6-luna')
    parser.add_argument('--arms', default='direct', help='direct / proxy，逗号分隔')
    parser.add_argument('--proxy', default='', help='arms 含 proxy 时用；留空则从代理池取')
    parser.add_argument('--runs', type=int, default=5, help='每臂轮数（交替执行）')
    parser.add_argument('--prompt', default='你好')
    parser.add_argument('--timeout', type=int, default=180)
    parser.add_argument('--lean', action='store_true', help='加上 CODEX_TURN_LEAN=1')
    parser.add_argument('--via-complete-cli', action='store_true',
                        help='走生产边界 codex_auth._complete_cli（验证直连默认/回退/写库）')
    parser.add_argument('--json', action='store_true', help='只输出 JSON（便于留档）')
    args = parser.parse_args()

    if args.check_logs:
        return check_logs(Path(args.log_dir), args.tail)
    if not args.email:
        raise SystemExit('测量需要 --email（或改用 --check-logs 只读复核）')
    db, codex_turn, codex_auth, proxy_pool = bootstrap()
    emails = [e.strip().lower() for chunk in args.email for e in chunk.split(',') if e.strip()]
    if not emails:
        raise SystemExit('至少要一个 --email')
    creds_by_email = {email: load_creds(db, email) for email in emails}
    if args.via_complete_cli:
        return run_production_path(db, codex_auth, emails, creds_by_email, args)
    models = [m.strip() for m in args.models.split(',') if m.strip()]
    arms = [a.strip() for a in args.arms.split(',') if a.strip()]
    pool_proxy = args.proxy.strip()
    if 'proxy' in arms and not pool_proxy:
        picked = proxy_pool.pick(purpose='codex') or {}
        pool_proxy = str(picked.get('url') or '')
        if not pool_proxy:
            print('代理池没有可用代理 → 去掉 proxy 臂', file=sys.stderr)
            arms = [a for a in arms if a != 'proxy']
    if not arms:
        raise SystemExit('没有可跑的臂')

    plan = [(model, arm) for _ in range(max(1, args.runs))
            for model in models for arm in arms]
    report = {'emails': emails, 'prompt': args.prompt, 'lean': bool(args.lean),
              'timeout': args.timeout, 'runs': [], 'started_at': time.time()}
    print(f'账号 {emails} / 模型 {models} / 臂 {arms} / 每臂 {args.runs} 轮 / lean={args.lean}')
    print('%-16s %-6s %-22s %6s %6s %6s %6s %6s %-6s %-6s %s'
          % ('模型', '出口', '账号', '启动', '模型段', '收尾', '合计', 'in', 'out', 'reason', '结果'))
    for index, (model, arm) in enumerate(plan, 1):
        # 账号轮换：同一个处置号连跑多轮会被服务端挑刺（实测连跑几轮后
        # workspace routing discovery 直接 401），分散到多个号上更接近生产。
        email = emails[(index - 1) % len(emails)]
        creds = creds_by_email[email]
        proxy = '' if arm == 'direct' else pool_proxy
        try:
            result = run_arm(codex_turn, email=email, creds=creds, model=model,
                             prompt=args.prompt, proxy=proxy, lean=args.lean,
                             timeout=args.timeout)
        except Exception as exc:  # noqa: BLE001
            result = {'ok': False, 'elapsed': None, 'error': f'{type(exc).__name__}: {exc}'[:160],
                      'tokens': {}}
        save_rotated(email, creds, result.get('tokens') or {})
        report['runs'].append({'model': model, 'arm': arm, 'email': email,
                               **{k: v for k, v in result.items() if k != 'tokens'}})
        print('%-16s %-6s %-22s %6s %6s %6s %6s %6s %-6s %-6s %s'
              % (model, arm, email.split('@')[0], result.get('startup'),
                 result.get('model_phase'), result.get('teardown'), result.get('elapsed'),
                 result.get('in'), result.get('out'), result.get('reason'),
                 result.get('reply') or result.get('error') or ''))
        if index < len(plan):
            time.sleep(2)   # 别让连续两轮撞上同一个服务端热区

    summary = []
    for model in models:
        for arm in arms:
            rows = [r for r in report['runs'] if r['model'] == model and r['arm'] == arm]
            ok = [r for r in rows if r.get('ok')]
            summary.append({
                'model': model, 'arm': arm, 'ok_rate': f'{len(ok)}/{len(rows)}',
                'median_elapsed': median([r.get('elapsed') for r in ok]),
                'median_model_phase': median([r.get('model_phase') for r in ok]),
                'median_startup': median([r.get('startup') for r in ok]),
                'median_in': median([r.get('in') for r in ok]),
                'errors': [r.get('error') for r in rows if not r.get('ok')],
            })
    report['summary'] = summary
    print('\n%-16s %-6s %-6s %-8s %-10s %-8s %s'
          % ('模型', '出口', 'ok', '合计中位', '模型段中位', '启动中位', 'in 中位'))
    for row in summary:
        print('%-16s %-6s %-6s %-8s %-10s %-8s %s'
              % (row['model'], row['arm'], row['ok_rate'], row['median_elapsed'],
                 row['median_model_phase'], row['median_startup'], row['median_in']))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
