"""Bind manual authorization to its card before work and reconcile once."""
from __future__ import annotations

import functools
import json
import logging
import time
from datetime import datetime,timezone
from uuid import uuid4

from ..common.cdk import mask_cdk
from ..errors import Error
from . import mysql
from .safety import redact

# 运行记录里每一行都要带上的卡密明细。`cdk_code` 沿用 SENSITIVE/cleaned() 掩码：
# 没有 invitation.credentials 时会被 cleaned() 换成 `***`；`cdk_code_masked` 永远是
# 脱敏值，所以无权限的页面也能看出"这条运行绑的是哪张卡"。
CDK_FIELDS=('cdk_code','cdk_code_masked','cdk_batch','cdk_status','cdk_fail_count','cdk_valid_until')
def epoch(value):
    if not isinstance(value,datetime):return None
    moment=value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return moment.timestamp()


class ManualCompletionMixin:
    def _manual_link(self, run_id, *, lock=False):
        return self.db.one(
            'SELECT * FROM invitation_v2_manual_run_links WHERE run_id=?' + (' FOR UPDATE' if lock else ''),
            mysql.marker(run_id),
        )

    def _mailbox_binding(self, email, *, lock=False):
        return self.db.one(
            'SELECT email,cdk_code,cdk_bound_at FROM invitation_v2_outlook_accounts WHERE email=?'
            + (' FOR UPDATE' if lock else ''), email,
        )

    def _require_mailbox_idle(self, email):
        # A current read also fences a concurrent permit claim when no owner exists.
        # Call after the inventory/card locks; permit claim/release never lock cards.
        if email and self.db.one(
            'SELECT slot_id FROM invitation_v2_execution_permits WHERE email=? '
            'AND owner_token IS NOT NULL FOR UPDATE', email,
        ):
            raise Error('INVITATION_RUNNING', '邮箱有执行中的任务，不能修改或重新开始。', 409)

    def _require_card_idle(self, code_id):
        if self.db.one(
            "SELECT job_id FROM invitation_v2_cdk_jobs WHERE code=? AND status IN ('queued','running') LIMIT 1 FOR UPDATE",
            code_id,
        ):
            raise Error('INVITATION_RUNNING', '卡密有执行中的任务，不能同步。', 409)

    def _manual_card(self, email, code_id, *, require_idle=False):
        self._lock_codes([code_id])
        mailbox = self._mailbox_binding(email, lock=True)
        card = self.db.one('SELECT * FROM invitation_v2_cdk_codes WHERE code=? FOR UPDATE', code_id)
        if not mailbox or mailbox.get('cdk_code') != code_id or not card or card.get('email') != email:
            raise Error('INVITATION_BINDING_CHANGED', '邮箱与卡密绑定已改变，未同步卡密。', 409)
        if card.get('status') not in ('bound', 'failed', 'done'):
            raise Error('INVITATION_INVALID_STATE', '当前卡密状态不能同步。', 409)
        self._require_inventory_available(code_id)
        self._require_no_holds(code_id)
        self._require_card_idle(code_id)
        if require_idle:
            self._require_mailbox_idle(email)
        return mailbox, card

    def _run_for_manual(self, email, run_id, *, lock=False):
        run = self.db.one(
            'SELECT * FROM invitation_v2_runs WHERE run_id=?' + (' FOR UPDATE' if lock else ''),
            mysql.marker(run_id),
        )
        if not run or run.get('email') != email or run.get('kind') != 'codex_auth':
            raise Error('INVITATION_RUN_MISMATCH', '未找到该邮箱的授权运行记录。', 409)
        return run

    def _manual_completion_evidence(self, email, run, *, historical=False):
        auth = self.db.one('SELECT account_id,cli_completed_account_id,invite_processed_at,cli_completed_at '
                           'FROM invitation_v2_codex_auth WHERE email=?', email) or {}
        completed = float(auth.get('cli_completed_at') or 0)
        invite = float(auth.get('invite_processed_at') or 0)
        if (not completed or not invite or invite > completed
                or not auth.get('account_id') or auth.get('account_id') != auth.get('cli_completed_account_id')
                or completed < float(run.get('started_at') or 0)
                or historical and (run.get('status') != 'done' or completed > float(run.get('finished_at') or 0))):
            raise Error('INVITATION_COMPLETION_UNVERIFIED', '缺少与本次运行一致的邀请及 CLI 完成记录。', 409)
        return auth

    def begin_manual_authorization(self, email, run_id):
        """Called inside TaskGate, before any provider or authorization request."""
        email = str(email or '').strip().lower()
        if not run_id:
            raise Error('INVITATION_RUN_REQUIRED', '手动授权需要运行记录。', 409)
        with self.db.transaction():
            self._run_for_manual(email, run_id)
            if self._manual_link(run_id, lock=True):
                raise Error('INVITATION_RUN_ALREADY_BOUND', '该运行已经启动，请查看原运行结果。', 409)
            snapshot = self._mailbox_binding(email)
            code_id = snapshot.get('cdk_code') if snapshot else None
            if code_id:
                self._manual_card(email, code_id)
            else:
                # Lock the unbound mailbox row so the recorded absence is current.
                current = self._mailbox_binding(email, lock=True)
                if current and current.get('cdk_code'):
                    raise Error('INVITATION_BINDING_CHANGED', '邮箱绑定已改变，请重新开始授权。', 409)
            self.db.execute('INSERT INTO invitation_v2_manual_run_links(run_id,code_id,created_at,sync_status) '
                            "VALUES (?,?,?,'pending')", mysql.protected(run_id), code_id, time.time())

    def _apply_manual_card_completion(self, email, run_id, link, run, result, *, historical=False):
        code_id = link.get('code_id')
        if not code_id:
            self.db.execute("UPDATE invitation_v2_manual_run_links SET sync_status='unbound',sync_error=NULL WHERE run_id=?",
                            mysql.marker(run_id))
            return 'unbound'
        mailbox, card = self._manual_card(email, code_id, require_idle=historical)
        auth = self._manual_completion_evidence(email, run, historical=historical)
        completed = float(auth['cli_completed_at'])
        if (float(card.get('redeemed_at') or 0) > float(run.get('started_at') or 0)
                or float(mailbox.get('cdk_bound_at') or 0) > float(run.get('started_at') or 0)
                or float(card.get('finished_at') or 0) > completed):
            raise Error('INVITATION_COMPLETION_UNVERIFIED', '卡密绑定或状态晚于该授权运行，未同步卡密。', 409)
        if not link.get('completed_job_id'):
            job_id = uuid4().hex
            code = mysql.revealed(code_id)
            self.repo.create_cdk_job(job_id, code, email=email, attempt=1)
            self.repo.set_cdk_job(job_id, status='done', stage='done', progress=100,
                                  started_at=run.get('started_at'), finished_at=completed, error=None)
            self.repo.set_cdk_status(code, 'done', finished_at=completed, error=None,
                                     invite_processed_at=auth['invite_processed_at'], result_json=json.dumps({
                                         'email': email, 'account_id': auth['account_id'],
                                         'export': (result or {}).get('export'), 'source': 'manual_authorization',
                                         'source_run_id': run_id,
                                     }, ensure_ascii=False))
            self.repo.add_cdk_event(code, job_id, 'manual_completed', f'run={run_id}')
            self.db.execute("UPDATE invitation_v2_manual_run_links SET completed_job_id=?,sync_status='done',sync_error=NULL WHERE run_id=?",
                            mysql.protected(job_id), mysql.marker(run_id))
        return 'done'

    def finalize_manual_authorization(self, email, run_id, result):
        """An upstream success stays successful even if local card sync fails."""
        email = str(email or '').strip().lower()
        if not result.get('ok'):
            reason = str(result.get('reason') or '授权未完成')
            try:
                from webui.registrar import classify_error
                category = classify_error(reason)
            except Exception:
                category = 'unknown'
            with self.db.transaction():
                self.db.execute("UPDATE invitation_v2_manual_run_links SET sync_status='failed',sync_error=? WHERE run_id=?",
                                mysql.protected('授权未完成，未同步卡密。'), mysql.marker(run_id))
                self.repo.finish_run(run_id, 'failed', reason, category=category)
            return {**result, '_run_finalized': True, 'cdk_sync_status': 'failed', 'cdk_sync_error': '授权未完成，未同步卡密。'}
        try:
            with self.db.transaction():
                link = self._manual_link(run_id, lock=True)
                if not link:
                    raise Error('INVITATION_RUN_UNBOUND', '未保存本次授权的卡密关联，未同步卡密。', 409)
                run = self._run_for_manual(email, run_id, lock=True)
                if run.get('status') == 'done' and link.get('sync_status') in ('done', 'unbound'):
                    return {**result, '_run_finalized': True, 'cdk_sync_status': link['sync_status'], 'cdk_sync_error': ''}
                status = self._apply_manual_card_completion(email, run_id, link, run, result.get('result') or {})
                self.repo.finish_run(run_id, 'done', '')
            return {**result, '_run_finalized': True, 'cdk_sync_status': status, 'cdk_sync_error': ''}
        except Exception as exc:
            message = redact(str(exc)) if isinstance(exc, Error) else '授权已完成，但卡密状态写回失败，请复核。'
            finalized = False
            try:
                with self.db.transaction():
                    self.db.execute("UPDATE invitation_v2_manual_run_links SET sync_status='failed',sync_error=? WHERE run_id=?",
                                    mysql.protected(message), mysql.marker(run_id))
                    self.repo.finish_run(run_id, 'done', '')
                finalized = True
            except Exception:
                # Preserve the upstream result for the worker's run-only fallback;
                # An unavailable database must never trigger another OAuth attempt.
                logging.getLogger(__name__).error('Manual authorization completed; local completion persistence unavailable')
            return {**result, '_run_finalized': finalized, 'cdk_sync_status': 'failed', 'cdk_sync_error': message}

    def managed_manual_authorizer(self, original):
        @functools.wraps(original)
        def authorize(email, *args, **kwargs):
            run_id = kwargs.get('run_id', '')
            self.begin_manual_authorization(email, run_id)
            try:
                result = original(email, *args, **kwargs)
            except Exception as exc:
                result = {'ok': False, 'reason': redact(f'{type(exc).__name__}: {exc}')}
            return self.finalize_manual_authorization(email, run_id, result)
        return authorize

    def reconcile_manual_completion(self, email, run_id, *, apply=False):
        """Targeted local reconciliation; no mail, SMS, OAuth or CLI is called."""
        email = str(email or '').strip().lower()
        response = {'email': email, 'run_id': run_id, 'eligible': False, 'applied': False}
        try:
            with self.db.transaction():
                # Completion callbacks take the association lock before the run.
                # Historical reconciliation must follow the same order.
                existing = self._manual_link(run_id, lock=apply)
                run = self._run_for_manual(email, run_id, lock=apply)
                self._manual_completion_evidence(email, run, historical=True)
                mailbox = self._mailbox_binding(email)
                code_id = existing.get('code_id') if existing else (mailbox or {}).get('cdk_code')
                if not code_id:
                    raise Error('INVITATION_RUN_UNBOUND', '该运行没有可核对的卡密绑定。', 409)
                mailbox, card = self._manual_card(email, code_id, require_idle=True)
                completed = float(self.db.scalar('SELECT cli_completed_at FROM invitation_v2_codex_auth WHERE email=?', email) or 0)
                if (float(card.get('redeemed_at') or 0) > float(run.get('started_at') or 0)
                        or float(mailbox.get('cdk_bound_at') or 0) > float(run.get('started_at') or 0)
                        or float(card.get('finished_at') or 0) > completed):
                    raise Error('INVITATION_COMPLETION_UNVERIFIED', '卡密绑定或状态晚于该授权运行。', 409)
                response.update(eligible=True, cdk_status=card['status'], completed_at=completed,
                                cdk_sync_status=(existing or {}).get('sync_status', 'pending'))
                if apply:
                    if not existing:
                        self.db.execute('INSERT INTO invitation_v2_manual_run_links(run_id,code_id,created_at,sync_status) '
                                        "VALUES (?,?,?,'pending')", mysql.protected(run_id), code_id, time.time())
                        existing = self._manual_link(run_id, lock=True)
                    response['cdk_sync_status'] = self._apply_manual_card_completion(email, run_id, existing, run, {}, historical=True)
                    response['applied'] = True
            return response
        except Error as exc:
            return {**response, 'eligible': False, 'error': {'code': exc.code, 'message': redact(str(exc))}}

    def attach_cdk_details(self, value):
        """给运行记录行补上该次运行绑定的卡密明细。

        取值优先级与人工授权同步一致：`manual_run_links.code_id` → 邮箱绑定
        （`outlook_accounts.cdk_code`）→ 该邮箱最近一条 `cdk_jobs.code`。
        三条都可能为空（例如账号工厂补号），此时明细字段留空，不猜也不报错。
        """
        targets={}
        def collect(item):
            if isinstance(item, list):
                for child in item: collect(child)
            elif isinstance(item, dict):
                if item.get('run_id') and item.get('kind') == 'codex_auth':
                    targets[mysql.marker(item['run_id'])] = str(item.get('email') or '').strip().lower()
                for child in item.values(): collect(child)
        collect(value)
        if not targets: return value
        run_markers=list(targets)
        links={row['run_id']: row.get('code_id') for row in self.db.all(
            'SELECT run_id,code_id FROM invitation_v2_manual_run_links WHERE run_id IN ('
            + ','.join('?' for _ in run_markers) + ')', *run_markers)}
        emails=sorted({email for email in targets.values() if email})
        mailboxes={};latest={}
        if emails:
            placeholders=','.join('?' for _ in emails)
            mailboxes={row['email']: row.get('cdk_code') for row in self.db.all(
                'SELECT email,cdk_code FROM invitation_v2_outlook_accounts WHERE email IN (' + placeholders + ')', *emails)}
            for row in self.db.all(
                'SELECT j.email AS email,j.code AS code FROM invitation_v2_cdk_jobs j '
                'JOIN (SELECT email,MAX(created_at) AS maxCreated FROM invitation_v2_cdk_jobs '
                '      WHERE email IN (' + placeholders + ') GROUP BY email) latest_one '
                '  ON latest_one.email=j.email AND latest_one.maxCreated=j.created_at', *emails):
                latest.setdefault(row['email'], row.get('code'))
        markers=sorted({marker for marker in [*links.values(), *mailboxes.values(), *latest.values()] if marker})
        cards={}
        if markers:
            for row in self.db.all(
                'SELECT c.code AS code,c.batch,c.status,c.fail_count AS failCount,p.expires_at AS expiresAt '
                'FROM invitation_v2_cdk_codes c LEFT JOIN invitation_v2_platform_keys p ON p.code_id=c.code '
                'WHERE c.code IN (' + ','.join('?' for _ in markers) + ')', *markers):
                cards[row['code']]=row
        def attach(item):
            if isinstance(item, list): return [attach(child) for child in item]
            if not isinstance(item, dict): return item
            out={key: attach(child) for key, child in item.items()}
            if item.get('run_id') and item.get('kind') == 'codex_auth':
                email=str(item.get('email') or '').strip().lower()
                marker=links.get(mysql.marker(item['run_id'])) or mailboxes.get(email) or latest.get(email) or ''
                row=cards.get(marker) or {}
                try: plain=mysql.revealed(marker) if marker else ''
                except Exception: plain=''
                out['cdk_code']=plain
                out['cdk_code_masked']=mask_cdk(plain)
                out['cdk_batch']=row.get('batch')
                out['cdk_status']=row.get('status')
                out['cdk_fail_count']=row.get('failCount')
                out['cdk_valid_until']=epoch(row.get('expiresAt'))
            return out
        return attach(value)

    def attach_manual_sync_status(self, value):
        """Only augment actual run rows, without revealing card identifiers."""
        ids = set()
        def collect(item):
            if isinstance(item, list):
                for child in item: collect(child)
            elif isinstance(item, dict):
                if item.get('run_id') and item.get('kind') == 'codex_auth':
                    ids.add(mysql.marker(item['run_id']))
                for child in item.values(): collect(child)
        collect(value)
        links = {row['run_id']: row for row in self.db.all(
            'SELECT run_id,sync_status,sync_error FROM invitation_v2_manual_run_links WHERE run_id IN ('
            + ','.join('?' for _ in ids) + ')', *ids,
        )} if ids else {}
        def attach(item):
            if isinstance(item, list): return [attach(child) for child in item]
            if not isinstance(item, dict): return item
            out = {key: attach(child) for key, child in item.items()}
            if item.get('run_id') and item.get('kind') == 'codex_auth':
                link = links.get(mysql.marker(item['run_id']))
                if link:
                    out['cdk_sync_status'] = link['sync_status']
                    out['cdk_sync_error'] = redact(mysql.revealed(link.get('sync_error')) or '')
            return out
        return attach(value)
