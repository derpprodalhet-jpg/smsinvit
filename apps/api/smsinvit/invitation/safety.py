"""Invitation outbound, process and logging boundaries."""
from __future__ import annotations
import ipaddress
import logging
import os
from pathlib import Path
import re
import socket
import ssl
import threading
from urllib.parse import urlsplit,urljoin,urlunsplit,unquote_plus
from curl_cffi import CurlOpt
from curl_cffi.curl import ffi as _curl_ffi,lib as _curl_lib
from requests.adapters import HTTPAdapter
from requests.utils import select_proxy
from urllib3.util import connection as _socket_connection
import requests as _requests
from curl_cffi import requests as _cffi

OFFICIAL_HOSTS=frozenset({'auth.openai.com','api.openai.com','chatgpt.com','sentinel.openai.com','login.microsoftonline.com','login.live.com','graph.microsoft.com','outlook.office.com','smsbower.page','hero-sms.com','api.smspool.net','api.ipify.org'})
MAX_RESPONSE=4*1024*1024
SECRET_LABEL=r'password|access[_-]?token|refresh[_-]?token|session[_-]?token|id[_-]?token|cookie(?:_header)?|authorization|api[_-]?key|totp[_-]?secret|session[_-]?secret|client[_-]?secret|secret|token|smsinvit[_-]?cdk|cdk[_-]?code|cdk|zc|redeem[_-]?code|redemption[_-]?code|card[_-]?key|key[_-]?plaintext|plaintext[_-]?key|raw[_-]?key|key|auth|code'
QUERY_SECRET_LABELS=frozenset({'cookie','cookieheader','authorization','smsinvitcdk','cdkcode','cdk','zc','redeemcode','redemptioncode','cardkey','keyplaintext','plaintextkey','rawkey','key','auth','code'})

def require_url(url):
    parsed=urlsplit(str(url));host=(parsed.hostname or '').lower();port=parsed.port or (443 if parsed.scheme=='https' else 80)
    extra={s.strip().lower() for s in os.getenv('SMSINVIT_INVITATION_ALLOWED_HOSTS','').split(',') if s.strip()}
    local={s.strip() for s in os.getenv('SMSINVIT_INVITATION_TEST_URLS','').split(',') if s.strip()}
    if os.getenv('SMSINVIT_ENV')=='test' and str(url).split('?',1)[0] in local:
        if parsed.scheme not in ('http','https') or host not in ('127.0.0.1','localhost','::1') or not parsed.port or parsed.username or parsed.fragment:raise ValueError('Invalid invitation test endpoint')
        return host,port,(_literal_target(host,port),)
    if parsed.scheme!='https' or parsed.username is not None or parsed.password is not None or parsed.fragment or not host or host.endswith('.'):
        raise ValueError('Invitation outbound URL is not allowed')
    if not (host in OFFICIAL_HOSTS and port==443 or f'{host}:{port}' in extra):raise ValueError('Invitation outbound host is not authorized by deployment')
    addresses=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise ValueError('Invitation outbound address is not public')
    return host,port,tuple(sorted({a[4][0] for a in addresses}))


def require_proxy(proxy):
    if not proxy:return
    p=urlsplit(proxy)
    if p.scheme not in {'http','https','socks5','socks5h'} or not p.hostname or not p.port or p.path not in {'','/'} or p.query or p.fragment:raise ValueError('Invalid proxy')
    allowed={v.strip().lower() for v in os.getenv('SMSINVIT_INVITATION_PROXY_HOSTS','').split(',') if v.strip()}
    explicitly_allowed=f'{p.hostname.lower()}:{p.port}' in allowed
    addresses=socket.getaddrinfo(p.hostname,p.port,type=socket.SOCK_STREAM)
    if not addresses or not explicitly_allowed and any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise ValueError('Private proxy requires a deployment allowlist entry')
    return p.hostname.lower(),p.port,tuple(sorted({a[4][0] for a in addresses}))


def redact(value):
    value=str(value)
    try:
        from .mysql import secret_literals
        for secret in sorted(secret_literals(),key=len,reverse=True):value=value.replace(secret,'[REDACTED]')
    except RuntimeError:pass
    value=re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?','[REDACTED]',value)
    value=re.sub(r'(https?://)[^\s/@:]+:[^\s/@]+@',r'\1[REDACTED]@',value)
    def query_mask(match):
        name=re.sub('[-_]','',unquote_plus(match.group(2))).lower()
        if name in QUERY_SECRET_LABELS or name.endswith(('token','secret','password','apikey')):return match.group(1)+match.group(2)+'=[REDACTED]'
        return match.group(0)
    # Mask raw encoded values as well as percent-encoded parameter names,
    # retaining the original URL spelling and unrelated query parameters.
    value=re.sub(r'''([?&])([^&=\s"']+)=([^&\s"']*)''',query_mask,value)
    pattern=r'''(?i)((?:["']?)(?:'''+SECRET_LABEL+r''')(?:["']?)\s*[=:]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[REDACTED\]|[^\s,&;}\]\["']+)'''
    def mask(match):
        raw=match.group(2);quote=raw[0] if raw.startswith(('"',"'")) else ''
        return match.group(1)+quote+'[REDACTED]'+quote
    value=re.sub(pattern,mask,value)
    return value

class RedactingFormatter(logging.Formatter):
    def __init__(self,original):self.original=original
    def format(self,record):return redact(self.original.format(record))
    def formatTime(self,record,datefmt=None):return self.original.formatTime(record,datefmt)

def install_log_redaction():
    # Source console handlers are separate from its per-run queue handler. Both
    # must redact tracebacks as well as formatted messages before writing bytes.
    loggers=[logging.getLogger(),*[v for v in logging.Logger.manager.loggerDict.values() if isinstance(v,logging.Logger)]]
    for logger in loggers:
        for handler in logger.handlers:
            if not isinstance(handler.formatter,RedactingFormatter):handler.setFormatter(RedactingFormatter(handler.formatter or logging.Formatter()))


def child_environment():
    names=('PATH','LANG','LC_ALL','TZ','SSL_CERT_FILE','SSL_CERT_DIR','REQUESTS_CA_BUNDLE','CURL_CA_BUNDLE','TMPDIR')
    env={k:os.environ[k] for k in names if k in os.environ}
    env['PYTHONIOENCODING']='utf-8'
    return env


def require_binary(binary):
    path=Path(binary).resolve(strict=True)
    roots=[Path(s).resolve() for s in os.getenv('SMSINVIT_INVITATION_BINARY_ROOTS','/app/runtime/bin').split(',') if s]
    if not path.is_file() or not os.access(path,os.X_OK) or not any(path.is_relative_to(r) for r in roots):raise ValueError('CLI binary is outside the deployment-approved installation roots')


def _literal_target(host,port):
    addresses=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
    if not addresses:raise ValueError('Cannot resolve test endpoint')
    return sorted({row[4][0] for row in addresses})[0]


def _brackets(host):return '['+host+']' if ':' in host else host


class _PinnedAdapter(HTTPAdapter):
    """Preserve the URL/TLS identity while pinning socket and proxy destinations."""
    def __init__(self,target,proxy_pins,retries=0):
        self.target=target;self.proxy_pins=proxy_pins
        super().__init__(max_retries=retries,pool_connections=1,pool_maxsize=1)
    def proxy_manager_for(self,proxy,**proxy_kwargs):
        if urlsplit(proxy).scheme=='https':
            context=ssl.create_default_context(cafile=os.getenv('SMSINVIT_INVITATION_CA_BUNDLE') or None)
            proxy_kwargs['proxy_ssl_context']=context
        return super().proxy_manager_for(proxy,**proxy_kwargs)
    def get_connection_with_tls_context(self,request,verify,proxies=None,cert=None):
        conn=super().get_connection_with_tls_context(request,verify,proxies,cert)
        host,port,addresses=self.target;proxy=select_proxy(request.url,proxies or {})
        if proxy and urlsplit(proxy).scheme.startswith('socks'):
            # SOCKS creates its socket internally. Both proxy host and SOCKS
            # destination are literal checked IPs, so even socks5h cannot resolve
            # the hostname a second time. TLS still verifies the original name.
            conn.host=addresses[0]
            conn.conn_kw['server_hostname']=host
            conn.conn_kw['assert_hostname']=host
            return conn
        base=conn.ConnectionCls;pins={host:addresses[0],**self.proxy_pins};allowed_host=host
        class PinnedConnection(base):
            def _new_conn(connection):
                checked=pins.get(connection._dns_host.lower())
                if not checked:raise ValueError('Socket destination was not pre-authorized')
                return _socket_connection.create_connection((checked,connection.port),connection.timeout,source_address=connection.source_address,socket_options=connection.socket_options)
            def set_tunnel(connection,host,port=None,headers=None,**kwargs):
                if host.lower()!=allowed_host:raise ValueError('Unexpected proxy CONNECT destination')
                connection.server_hostname=allowed_host;connection.assert_hostname=allowed_host
                return super().set_tunnel(addresses[0],port,headers,**kwargs)
        conn.ConnectionCls=PinnedConnection
        return conn


class _Guarded:
    def request(self,method,url,*args,**kwargs):
        # This lock protects each session's per-transfer libcurl options. Source
        # jobs already own distinct sessions; the lock also fences accidental reuse.
        lock=getattr(self,'_smsinvit_request_lock',None)
        if lock is None:self._smsinvit_request_lock=lock=threading.RLock()
        with lock:return self._guarded_request(method,url,*args,**kwargs)
    def _guarded_request(self,method,url,*args,**kwargs):
        follow=kwargs.pop('allow_redirects',True)
        kwargs['allow_redirects']=False;kwargs['verify']=os.getenv('SMSINVIT_INVITATION_CA_BUNDLE') or True;kwargs.setdefault('timeout',30)
        self.trust_env=False
        original_auth=getattr(self,'auth',None)
        try:
            for _ in range(9):
                target=require_url(url);host,port,addresses=target
                # Effective explicit session proxies only; environment proxies
                # must not introduce an unvalidated private connection.
                raw_proxies=kwargs.get('proxies') or getattr(self,'proxies',{}) or {}
                if kwargs.get('proxy'):raw_proxies={'all':kwargs['proxy']}
                proxy_pins={};resolved_proxies={}
                for key,proxy in raw_proxies.items():
                    if not proxy:continue
                    proxy_host,proxy_port,proxy_addresses=require_proxy(proxy);proxy_pins[proxy_host]=proxy_addresses[0]
                    p=urlsplit(proxy)
                    if p.scheme.startswith('socks'):
                        auth=(p.username or '')+(':'+p.password if p.password is not None else '')
                        netloc=(auth+'@' if auth else '')+_brackets(proxy_addresses[0])+':'+str(proxy_port)
                        resolved_proxies[key]=urlunsplit((p.scheme,netloc,p.path,'',''))
                    else:resolved_proxies[key]=proxy
                if isinstance(self,_cffi.Session):
                    previous=dict(self.curl_options);content=bytearray();client_callback=kwargs.pop('content_callback',None)
                    def capped(chunk):
                        if len(content)+len(chunk)>MAX_RESPONSE:return 0xFFFFFFFF
                        content.extend(chunk)
                        if client_callback is not None:client_callback(chunk)
                        return len(chunk)
                    # CONNECT_TO also tunnels through HTTP proxies and preserves
                    # TLS SNI/certificate verification (libcurl documented contract).
                    connect_to=_curl_lib.curl_slist_append(_curl_ffi.NULL,f'{_brackets(host)}:{port}:{_brackets(addresses[0])}:{port}'.encode())
                    self.curl_options={**previous,CurlOpt.CONNECT_TO:connect_to,CurlOpt.RESOLVE:[f'{_brackets(h)}:{urlsplit(p).port}:{_brackets(proxy_pins[h])}'.encode() for p in resolved_proxies.values() for h in [urlsplit(p).hostname] if h in proxy_pins],CurlOpt.SSL_VERIFYPEER:1,CurlOpt.SSL_VERIFYHOST:2,CurlOpt.PROXY_SSL_VERIFYPEER:1,CurlOpt.PROXY_SSL_VERIFYHOST:2}
                    if os.getenv('SMSINVIT_INVITATION_CA_BUNDLE'):self.curl_options[CurlOpt.PROXY_CAINFO]=os.environ['SMSINVIT_INVITATION_CA_BUNDLE'].encode()
                    request_kwargs=dict(kwargs);request_kwargs['stream']=False;request_kwargs['content_callback']=capped
                    if raw_proxies:request_kwargs['proxies']=resolved_proxies;request_kwargs.pop('proxy',None)
                    try:
                        response=super().request(method,url,*args,**request_kwargs)
                        response.content=bytes(content)
                    finally:
                        self.curl_options=previous
                        _curl_lib.curl_slist_free_all(connect_to)
                else:
                    # Preserve the copied engine's configured fallback retries.
                    adapter=_PinnedAdapter(target,proxy_pins,self.get_adapter(url).max_retries)
                    prefix=f'{urlsplit(url).scheme}://{_brackets(host)}'+(':'+str(port) if port not in (80,443) else '')+'/'
                    old_adapter=self.adapters.get(prefix);self.mount(prefix,adapter)
                    request_kwargs=dict(kwargs);request_kwargs['stream']=True
                    headers=dict(request_kwargs.get('headers') or {});headers['Host']=host+(':'+str(port) if port not in (80,443) else '');request_kwargs['headers']=headers
                    if raw_proxies:request_kwargs['proxies']=resolved_proxies;request_kwargs.pop('proxy',None)
                    try:
                        response=super().request(method,url,*args,**request_kwargs)
                        content=bytearray()
                        try:
                            for chunk in response.iter_content(65536):
                                if len(content)+len(chunk)>MAX_RESPONSE:raise ValueError('Invitation outbound response too large')
                                content.extend(chunk)
                        finally:response.close()
                        response._content=bytes(content);response._content_consumed=True
                    finally:
                        if old_adapter is not None:self.mount(prefix,old_adapter)
                        else:self.adapters.pop(prefix,None)
                        adapter.close()
                if not follow or response.status_code not in (301,302,303,307,308):return response
                target_url=urljoin(url,response.headers.get('location',''));require_url(target_url)
                if urlsplit(target_url).netloc!=urlsplit(url).netloc:
                    headers=dict(kwargs.get('headers') or {});headers['Authorization']='';headers['Cookie']='';kwargs['headers']=headers
                    self.auth=None;kwargs.pop('auth',None)
                if response.status_code==303 or response.status_code in (301,302) and method.upper()=='POST':
                    method='GET'
                    for field in ('data','json','files','multipart','content'):kwargs.pop(field,None)
                url=target_url
            raise ValueError('Too many invitation outbound redirects')
        finally:self.auth=original_auth


class GuardedCffiSession(_Guarded,_cffi.Session):
    def __init__(self,*args,**kwargs):
        kwargs['trust_env']=False;super().__init__(*args,**kwargs);self._smsinvit_request_lock=threading.RLock()

class GuardedRequestsSession(_Guarded,_requests.Session):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs);self.trust_env=False;self._smsinvit_request_lock=threading.RLock()


class Module:
    def __init__(self,module,session):self._module=module;self.Session=session
    def __getattr__(self,key):return getattr(self._module,key)
    def request(self,method,url,**kwargs):
        with self.Session() as session:return session.request(method,url,**kwargs)
    def get(self,url,**kwargs):return self.request('GET',url,**kwargs)
    def post(self,url,**kwargs):return self.request('POST',url,**kwargs)
    def put(self,url,**kwargs):return self.request('PUT',url,**kwargs)
    def delete(self,url,**kwargs):return self.request('DELETE',url,**kwargs)


guarded_requests=Module(_requests,GuardedRequestsSession)
cffi_requests=Module(_cffi,GuardedCffiSession)
