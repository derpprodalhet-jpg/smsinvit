"""Narrow MySQL repository compatibility for the copied invitation engine.

Only invitation tables are addressable. Source SQLite is never opened at runtime.
Each repository function is one transaction; an outer API transaction can encompass
an entire batch. Sensitive values are HMAC identifiers plus AES-GCM ciphertext.
"""
from __future__ import annotations
import functools
import hashlib
import hmac
import sqlite3
from sqlalchemy.exc import IntegrityError
import re
import threading
from collections.abc import Mapping
from typing import Any

TABLES = ('outlook_accounts','settings','registered','runs','sms_refund_tasks','proxy_pool','proxy_events','sms_prices','sms_stats','sms_price_state','cdk_codes','cdk_jobs','cdk_events','codex_auth')
SECRET_FIELDS = frozenset({'password','refresh_token','access_token','session_token','id_token','cookie_header','csrf_token','totp_secret','relay_url','proxy','proxy_used','url','invite_link','auth_json','web_access_token','result_json','extra_json','code','cdk_code','job_id','run_id','value','error','last_error','fail_reason','detail'})
_services: Any = None
_runtime_owner: Any = None
_cache: dict[str,str] = {}
_redaction_values: set[str] = set()
_cache_lock=threading.RLock()


def configure(services,*,runtime=False):
    global _services,_runtime_owner
    with _cache_lock:
        if _runtime_owner is not None and _runtime_owner is not services:
            raise RuntimeError('The copied invitation engine has one immutable service binding per process')
        if _services is not services:_services=services;_cache.clear();_redaction_values.clear()
        if runtime:_runtime_owner=services


def services():
    if _services is None:raise RuntimeError('Invitation repository is not configured')
    return _services


def marker(value):
    if value is None or value=='':return value
    value=str(value)
    if value.startswith('iv2$') and len(value)==68:return value
    return 'iv2$'+hmac.new(services().codec.fingerprint_key,value.encode(),hashlib.sha256).hexdigest()


def is_secret_setting(key):return bool(re.search(r'password|secret|token|api[_-]?key|auth|proxy|cookie|totp|relay_url|_url$',str(key),re.I))

def protected(value,*,sensitive=True):
    if value is None or value=='':return value
    value=str(value)
    if value.startswith('iv2$') and len(value)==68:return value
    s=services();fingerprint=marker(value)[4:]
    s.db.execute('INSERT INTO invitation_v2_secret_values(fingerprint,ciphertext,plaintext_length) VALUES (?,?,?) ON DUPLICATE KEY UPDATE fingerprint=VALUES(fingerprint)',fingerprint,s.codec.encrypt(value),len(value))
    result_marker='iv2$'+fingerprint
    with _cache_lock:
        _cache[result_marker]=value
        if sensitive:_redaction_values.add(value)
    return result_marker


def revealed(value,*,sensitive=True):
    if not isinstance(value,str) or not value.startswith('iv2$') or len(value)!=68:return value
    with _cache_lock:
        if value in _cache:
            clear=_cache[value]
            if sensitive:_redaction_values.add(clear)
            return clear
    row=services().db.one('SELECT ciphertext FROM invitation_v2_secret_values WHERE fingerprint=?',value[4:])
    if not row:raise RuntimeError('Missing encrypted invitation value')
    clear=services().codec.decrypt(row['ciphertext'])
    with _cache_lock:
        _cache[value]=clear
        if sensitive:_redaction_values.add(clear)
    return clear


def secret_literals():
    with _cache_lock:return tuple(v for v in _redaction_values if len(v)>6)

def remember_secrets(values):
    """Cover newly rotated credentials before logging or database persistence."""
    with _cache_lock:_redaction_values.update(str(value) for value in values if value)


class Row(dict):
    def __getitem__(self,key):
        if isinstance(key,int):return list(self.values())[key]
        return super().__getitem__(key)


class Cursor:
    def __init__(self,rows=(),rowcount=0,lastrowid=None,value_key=None):
        self._rows=[Row({k:revealed(v,sensitive=is_secret_setting(r.get('key',value_key)) if k=='value' else k in SECRET_FIELDS) for k,v in r.items()}) for r in rows]
        self.rowcount=rowcount;self.lastrowid=lastrowid;self._position=0
    def fetchone(self):
        if self._position>=len(self._rows):return None
        row=self._rows[self._position];self._position+=1;return row
    def fetchall(self):
        rows=self._rows[self._position:];self._position=len(self._rows);return rows
    def __iter__(self):return iter(self.fetchall())


def _split_expressions(value):
    out=[];level=0;start=0;quoted=False
    for i,c in enumerate(value):
        if c=="'":quoted=not quoted
        if not quoted:
            if c=='(':level+=1
            elif c==')':level-=1
            elif c==',' and level==0:out.append(value[start:i].strip());start=i+1
    out.append(value[start:].strip());return out


def _parameter_fields(sql):
    fields=[]
    ins=re.search(r'INSERT(?: OR (?:REPLACE|IGNORE))? INTO\s+\w+\s*\((.*?)\)\s*VALUES\s*\((.*)\)',sql,re.I|re.S)
    if ins:
        cols=[x.strip(' `') for x in ins.group(1).split(',')]
        expressions=_split_expressions(ins.group(2).split('ON CONFLICT')[0].rstrip())
        for col,expr in zip(cols,expressions):fields.extend([col]*expr.count('?'))
        return fields
    for match in re.finditer(r'\?',sql):
        before=sql[:match.start()]
        field=re.search(r'(?:\b|\.)([A-Za-z_]+)\s*(?:=|<>|!=|LIKE)\s*$',before,re.I)
        if field:fields.append(field.group(1));continue
        in_field=re.search(r'\b([A-Za-z_]+)\s+(?:NOT\s+)?IN\s*\([^)]*$',before,re.I)
        fields.append(in_field.group(1) if in_field else None)
    return fields


def sql_segments(sql):
    """把一条 SQL 切成 [(片段, 是否是字符串字面量/注释), ...]。

    为什么要切：关键字与表名改写只能落在**代码**上。整条 SQL 统一 re.sub 会把
    字符串里的值也当成标识符 —— 账号池状态值 `status='registered'` 会被
    `\\bregistered\\b → invitation_v2_registered` 改坏（拆分账号池时踩到的真坑）。
    反引号包起来的是标识符，不算字面量（里面的表名仍要改写）。
    """
    out=[];i=0;start=0;length=len(sql)
    while i<length:
        char=sql[i]
        if char in ("'",'"'):
            if i>start:out.append((sql[start:i],False))
            j=i+1
            while j<length:
                if sql[j]=='\\' and j+1<length:j+=2;continue
                if sql[j]==char:
                    if j+1<length and sql[j+1]==char:j+=2;continue
                    j+=1;break
                j+=1
            out.append((sql[i:j],True));i=j;start=j;continue
        if char=='`':
            i+=1
            while i<length and sql[i]!='`':i+=1
            i+=1;continue
        if sql[i:i+2] in ('--','/*') or char=='#':
            if i>start:out.append((sql[start:i],False))
            if sql[i:i+2]=='/*':
                end=sql.find('*/',i+2);end=length if end<0 else end+2
            else:
                end=sql.find('\n',i);end=length if end<0 else end
            out.append((sql[i:end],True));i=end;start=end;continue
        i+=1
    if start<length:out.append((sql[start:],False))
    return out


def rewrite_identifiers(sql):
    """只改写代码段里的一处 SQL 方言差异。"""
    out=[]
    for part,literal in sql_segments(sql):
        if literal:out.append(part);continue
        part=re.sub(r'\bkey\b','`key`',part,flags=re.I)
        for name in sorted(TABLES,key=len,reverse=True):
            part=re.sub(r'\b'+name+r'\b','invitation_v2_'+name,part)
        out.append(part)
    return ''.join(out)


def translate(sql,params=()):
    params=list(params);fields=_parameter_fields(sql)
    setting_key=next((params[i] for i,field in enumerate(fields) if field=='key' and i<len(params)),None)
    for i,field in enumerate(fields):
        if i<len(params) and field in SECRET_FIELDS:params[i]=(marker(params[i]) if sql.lstrip().upper().startswith(('SELECT','WITH','DELETE')) else protected(params[i],sensitive=is_secret_setting(setting_key) if field=='value' else True))
    sql=re.sub(r'\blength\((\w+\.)?(access_token|session_token|refresh_token)\)',lambda m:'COALESCE((SELECT plaintext_length FROM invitation_v2_secret_values WHERE fingerprint=SUBSTRING('+m.group(0)[7:-1]+',5)),0)',sql,flags=re.I)
    sql=rewrite_identifiers(sql)
    sql=re.sub(r'INSERT OR IGNORE','INSERT IGNORE',sql,flags=re.I)
    sql=re.sub(r'INSERT OR REPLACE','REPLACE',sql,flags=re.I)
    sql=re.sub(r'ON CONFLICT\s*\([^)]*\)\s*DO UPDATE SET','ON DUPLICATE KEY UPDATE',sql,flags=re.I)
    sql=re.sub(r'excluded\.(`?\w+`?)',lambda m:'VALUES('+m.group(1)+')',sql,flags=re.I)
    sql=re.sub(r'\bRANDOM\s*\(\s*\)','RAND()',sql,flags=re.I)
    if re.search(r'\b(?:CREATE|ALTER|DROP|PRAGMA|ATTACH|DETACH)\b',sql,re.I):raise ValueError('DDL is not allowed in the invitation runtime')
    return sql,params


class Connection:
    def execute(self,sql,params=()):
        value_key=next((params[i] for i,field in enumerate(_parameter_fields(sql)) if field=='key' and i<len(params)),None)
        sql,params=translate(sql,params)
        if sql.lstrip().upper().startswith(('SELECT','WITH')):
            # SQLite's serialized repository reads had no stale snapshot after
            # waiting for a writer. MySQL RR needs a current read for the locked
            # card/state lookup, and row-locked mailbox selection for allocation.
            compact=' '.join(sql.split()).lower()
            if services().db.in_transaction:
                if compact.startswith('select * from invitation_v2_cdk_jobs where code=?'):
                    # Enqueue locks the parent card before its jobs. Status reads
                    # must use the same order, including the empty-job gap, or
                    # concurrent enqueue/status requests can deadlock.
                    services().db.one('SELECT code FROM invitation_v2_cdk_codes WHERE code=? FOR UPDATE',params[0])
                if compact=='select * from invitation_v2_cdk_codes where code=?' or compact.startswith('select * from invitation_v2_cdk_jobs where code=?'):
                    sql+=' FOR UPDATE'
                elif compact.startswith("select * from invitation_v2_outlook_accounts where status='registered'") and compact.endswith('limit 1'):
                    sql+=' FOR UPDATE SKIP LOCKED'
            return Cursor(services().db.all(sql,*params),value_key=value_key)
        try:result=services().db.execute(sql,*params)
        except IntegrityError as exc:raise sqlite3.IntegrityError("Invitation unique constraint failed") from exc
        return Cursor(rowcount=result.rowcount,lastrowid=result.lastrowid)
    def executemany(self,sql,rows):
        count=0
        for row in rows:count+=self.execute(sql,row).rowcount
        return Cursor(rowcount=count)
    def commit(self):pass # Explicit transaction owner decides commit/rollback.
    def rollback(self):raise RuntimeError('Use explicit transaction rollback through exceptions')
    def close(self):pass
    def __enter__(self):return self
    def __exit__(self,*args):return False


def connection():return Connection()


def transactional(fn):
    completion_write = fn.__name__ in {'set_cdk_status', 'update_cdk_fields'}
    @functools.wraps(fn)
    def call(*args,**kwargs):
        bound = services()
        with bound.db.transaction():
            result = fn(*args,**kwargs)
            status = args[1] if fn.__name__ == 'set_cdk_status' and len(args) > 1 else kwargs.get('status')
            if completion_write and result and status == 'done':
                # Automatic authorization and manual reconciliation write done
                # through this boundary. Outbox rows share that transaction and
                # become visible to the notification worker only after commit.
                invitation = getattr(bound, 'invitation', None)
                if invitation is not None:
                    invitation.notify_card_redeemed(args[0] if args else kwargs.get('code'))
            return result
    return call
