"""Single Python API with explicit, once-only worker startup and legacy routes."""
from __future__ import annotations
from contextlib import asynccontextmanager
import importlib
import logging
import re
import secrets
import threading
from types import SimpleNamespace
from uuid import uuid4
from fastapi import FastAPI,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool
from .config import Config,normalize_cors_origin,normalize_cors_origins
from .db import Database,start_lock_keepalive
from .errors import Error,error_body,response
from .frontend import admin_path,is_admin_path,register_frontend
from .security import SecretCodec,TokenService,PermissionService,TrustedProxyResolver,SupplierEndpointPolicy,SupplierClientGuard
from .common.auth import RateLimiter,Captcha,AdminAuth,AdminIpGuard
from .common.audit import Audit,request_id_context
from .common.catalog import Plans
from .common.inventory import Inventory
from .common.settings import Settings
from .common.staff import Staff
from .common.jobs import DurableJobs,Scheduler
from .common.notifications import Notifications
from .common.ops import Ops
from .public_limits import flood_rate,METADATA_IP_LIMIT

log=logging.getLogger(__name__)
class Services(SimpleNamespace):
    def __init__(self,db,config,codec=None):
        super().__init__(db=db,config=config,codec=codec or SecretCodec(config))
        self.tokens=TokenService(config);self.permissions=PermissionService(db);self.limiter=RateLimiter(db,self.codec);self.audit=Audit(db);self.captcha=Captcha(self.codec,self.tokens,config,db=db);self.auth=AdminAuth(db,self.codec,self.tokens,config,self.limiter,self.captcha);self.ipguard=AdminIpGuard(db,self.codec);self.policy=SupplierEndpointPolicy(config);self.http=SupplierClientGuard(config,self.policy)
        self.jobs=DurableJobs(self);self.scheduler=Scheduler(self);self.notifications=Notifications(self);self.outbox=self.notifications;self.plans=Plans(self);self.inventory=Inventory(self);self.settings=Settings(self);self.staff=Staff(self);self.ops=Ops(self);self.common=self
    def attach_domains(self):
        if getattr(self,'_domains_attached',False):return
        from .sms.service import SmsService
        from .invitation.service import InvitationService
        args=dict(tokens=self.tokens,jobs=self.jobs,outbox=self.outbox,policy=self.policy,http=self.http)
        self.sms=SmsService(self.db,self.codec,self.config,**args);self.invitation=InvitationService(self)
        self.scheduler.register('worker.process',self.jobs.tick,self.config.int('runtime.worker_poll_ms',1000),'持久任务处理')
        self.scheduler.register('worker.heartbeat',self.scheduler.heartbeat,self.config.int('runtime.worker_heartbeat_ms',10000),'Worker 心跳')
        self.scheduler.register('worker.recovery',self.recovery,self.config.int('runtime.worker_recovery_ms',30000),'任务恢复')
        self.scheduler.register('worker.sms_sweep',self.sms.sweep,self.config.int('runtime.worker_sms_sweep_ms',15000),'接码清扫',description='恢复接码状态、取消补偿及卡密到期')
        self.scheduler.register('notification.poll',self.notifications.tick,self.config.int('runtime.worker_notification_poll_ms',2000),'通知发送轮询','notification')
        self._domains_attached=True
    def recovery(self):
        from .common.mapper import Mapper
        with self.db.transaction():
            self.jobs.mapper.recoverExpired();m=Mapper(self.db,'JobsWorkerMapper');m.deleteStaleHeartbeats();m.deleteExpiredRateLimits();m.deleteExpiredChallenges();m.deleteExpiredAdminIpBans()
        self.config.load_runtime(self.db,self.codec)

AUTH_OPEN={'/api/v1/admin/auth/captcha','/api/v1/admin/auth/login','/api/v1/admin/auth/login/totp','/api/v1/admin/auth/refresh'}
ALLOWED_HEADERS='Content-Type,Authorization,X-CSRF-Token,X-Request-Id,Idempotency-Key,X-Session-Secret,Cache-Control,Pragma'

def csrf_required(request):
    if request.method in ('GET','HEAD','OPTIONS'):return False
    path=request.url.path
    if request.method=='POST' and path.startswith('/api/v1/sms/webhook/'):return False
    has_refresh=any(request.cookies.get(name) for name in ('lc_refresh','lc_admin_refresh'))
    has_access=any(request.cookies.get(name) for name in ('lc_access','lc_admin_access'))
    return has_refresh or has_access and not request.headers.get('authorization','').startswith('Bearer ')

def common_rate(request,services):
    path=request.url.path;method=request.method;actor=getattr(request.state,'principal',None);id=actor.user_id if actor else request.state.client_ip
    parts=path.split('/')
    domain=parts[3] if len(parts)>3 else ''
    if domain in ('sms','invitation'):
        if '/sessions/' in path or domain=='invitation' and ('/redemption-attempts/' in path or path.endswith(('/status','/start','/invite-sent'))):
            read=method=='GET' or path.endswith(('/status','/fetch-code'))
            flood_rate(request,domain,'read' if read else 'write',services);return
        if method=='POST' and path.endswith(('/redeem','/preflight')):
            flood_rate(request,domain,'entry',services);return
    bucket=limit=window=None
    if method=='GET' and path=='/api/v1/admin/auth/captcha':bucket='admin.auth.captcha';limit=30;window=60
    elif method=='POST' and path=='/api/v1/admin/auth/refresh':bucket='admin.auth.refresh';limit=60;window=60
    elif method=='POST' and path.startswith('/api/v1/sms/webhook/'):
        bucket='sms.webhook';limit=300;window=60
        # 这里曾有 `bucket+':total'` 的全站共享桶：匿名伪造 token 也能计数，300 次/分
        # 就能把真实上游回调全部打成 429（接码会话超时、用户取号次数被烧）。
        # 改为只保留下面的按 IP 桶（app.py 末尾 `f'{bucket}:{id}'`）。
    elif method=='POST' and path=='/api/v1/admin/auth/login':bucket='admin.auth.login';limit=30;window=900
    elif method=='POST' and path=='/api/v1/admin/auth/login/totp':bucket='admin.auth.login.totp';limit=30;window=900
    elif method=='POST' and path=='/api/v1/admin/auth/change-password':bucket='admin.auth.change-password';limit=10;window=3600
    elif path.startswith('/api/v1/admin/inventory'):
        if method=='GET' and (path.endswith('/secret') or path.endswith('/export')):bucket='admin.inventory.secret';limit=10 if path.endswith('/export') else 30;window=60
        elif method=='POST':bucket='admin.inventory.write';limit=20 if path.endswith('/generate') else 60;window=60
    elif path.startswith('/api/v1/admin/plans') and method!='GET':bucket='admin.plan.write';limit=60;window=60
    elif path.startswith('/api/v1/admin/staff') and method!='GET':bucket='admin.staff.secret' if path.endswith('/reset-password') else 'admin.staff.write';limit=10 if method=='DELETE' or path.endswith('/reset-password') else 20 if method=='POST' else 30;window=60
    elif path=='/api/v1/admin/settings' and method=='POST' or path.startswith('/api/v1/admin/settings/') and method=='DELETE':bucket='admin.settings.write';limit=60 if method=='POST' else 30;window=60
    elif path.startswith('/api/v1/admin/jobs/') and path.endswith('/retry'):bucket='admin.jobs.retry';limit=30;window=60
    elif path.startswith('/api/v1/admin/settings/test-'):bucket='admin.notify.'+path.rsplit('/',1)[-1];limit=10;window=60
    elif path.startswith('/api/v1/admin/notifications/outbox/') and path.endswith('/retry'):bucket='admin.notify.outbox.retry';limit=30;window=60
    elif method=='POST' and path=='/api/v1/admin/sms/settings':bucket='admin.sms.settings';limit=30;window=60
    elif path.startswith('/api/v1/admin/sms/excluded-areas') and method in ('POST','DELETE'):bucket='admin.sms.areas';limit=60;window=60
    # 会打上游 / 重查询 / 有副作用的端点原来一个桶都没有（可被反复放大）。
    elif method=='GET' and path in ('/api/v1/admin/sms/catalog/countries','/api/v1/admin/sms/catalog/services','/api/v1/admin/sms/catalog/prices','/api/v1/admin/audit-logs','/api/v1/admin/dashboard/summary','/api/v1/admin/stats/overview'):
        bucket='admin.heavy';limit=30;window=60
    elif method=='POST' and path in ('/api/v1/admin/auth/totp/setup',):bucket='admin.auth.totp.setup';limit=10;window=60
    elif path.startswith('/api/v1/admin/security/ip-bans'):bucket='admin.security.ipban';limit=30;window=60
    elif method in ('PATCH','POST') and path.startswith('/api/v1/admin/schedulers/'):bucket='admin.scheduler';limit=10;window=60
    # 公开读兜底：匿名可无限打的配置/健康端点（每请求都要读库或解密）。
    elif method=='GET' and path.startswith('/api/v1/') and not path.startswith('/api/v1/admin/'):
        bucket='public.v2.metadata.'+domain;limit=METADATA_IP_LIMIT;window=60
    if bucket:services.limiter.check(f'{bucket}:{id}',limit,window)

def create_app(config=None,db=None,*,services=None,include_domains=True,load_runtime=True):
    config=config or Config.from_env();db=db or Database(config.database_url(),connect_args=config.database_connect_args());services=services or Services(db,config)
    admin_base=admin_path(config)
    if load_runtime:config.load_runtime(db,services.codec)
    if include_domains:services.attach_domains()
    resolver=TrustedProxyResolver(str(config.get('smsinvit.security.trusted-proxies','127.0.0.0/8,::1/128')))
    # 生产路径上 services 一定是 Services 实例，ipguard 必然存在（见 Services.__init__），
    # 这里直接取属性，缺了就是真实缺陷。测试里手工拼的 SimpleNamespace 替身可能没有这个
    # 属性，那种替身不跑真实登录流程，允许省略，避免为了一个桩去改十几个测试文件。
    ban_guard=services.ipguard if isinstance(services,Services) else getattr(services,'ipguard',None)
    worker_connection=None
    @asynccontextmanager
    async def lifespan(app):
        nonlocal worker_connection
        worker_enabled=config.flag('smsinvit.worker-enabled',config.flag('SMSINVIT_WORKER_ENABLED',False))
        invitation_default=worker_enabled or config.flag('smsinvit.invitation-worker-enabled',worker_enabled)
        invitation_enabled=services.invitation.initialize_executor_enabled(invitation_default) if include_domains else invitation_default
        # 账号工厂（补号）是独立开关：只开补号、关着邀请执行也要能启动执行进程。
        pool_default=config.flag('smsinvit.invitation-pool-enabled',config.flag('SMSINVIT_INVITATION_POOL_ENABLED',False))
        pool_enabled=services.invitation.initialize_pool_enabled(pool_default) if include_domains else pool_default
        invitation_started=False;worker_lock_keepalive=None
        try:
            if (worker_enabled or invitation_enabled or pool_enabled) and not include_domains:raise RuntimeError('Cannot start workers without business services')
            if worker_enabled:
                worker_connection=db.engine.connect()
                acquired=worker_connection.exec_driver_sql("SELECT GET_LOCK('smsinvit:python:worker',0)").scalar()
                if acquired!=1:raise RuntimeError('A SmsInvit worker is already active; refusing a second owner')
                # 命名锁随会话结束自动释放，空闲会话又会被 wait_timeout 回收；
                # 丢锁在这里的后果是"单实例"保证失效（第二个实例可以同时跑定时
                # 任务），所以这条锁连接同样要保活。
                worker_lock_keepalive=start_lock_keepalive(worker_connection,'smsinvit:python:worker')
            if invitation_enabled or pool_enabled or worker_enabled:
                services.invitation.start(background=worker_enabled);invitation_started=True
            if worker_enabled:services.scheduler.start()
            yield
        finally:
            if worker_connection is not None:services.scheduler.stop()
            invitation_stopped=services.invitation.stop() if invitation_started or include_domains and getattr(services.invitation,'_started',False) else True
            if worker_lock_keepalive is not None:
                # 先停保活再碰连接，避免两条线程同时用同一条连接。
                stop,keepalive=worker_lock_keepalive;stop.set()
                if keepalive.is_alive() and keepalive is not threading.current_thread():keepalive.join(timeout=10)
                worker_lock_keepalive=None
            if worker_connection is not None:
                if services.scheduler.thread and services.scheduler.thread.is_alive() or invitation_stopped is False:
                    log.error('Executor still active; retaining ownership until process exit')
                else:
                    worker_connection.exec_driver_sql("SELECT RELEASE_LOCK('smsinvit:python:worker')");worker_connection.close();worker_connection=None
            sms=getattr(services,'sms',None)
            if sms is not None and hasattr(sms,'close'):
                try:sms.close()
                except Exception:log.warning('SMS lock pool shutdown failed',exc_info=True)
    app=FastAPI(title='SmsInvit Redeem',version='1.0.0',lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.services=services
    @app.exception_handler(Error)
    async def expected(request,error):
        out=response(error_body(error,getattr(request.state,'request_id',None)),error.status)
        # IP 封禁是 403，但也带明确的剩余秒数，客户端据此显示还要等多久。
        retry_after=getattr(error,'retry_after',None)
        if retry_after is not None:out.headers['Retry-After']=str(retry_after)
        elif error.status==429:out.headers['Retry-After']='60'
        return out
    @app.exception_handler(RequestValidationError)
    async def validation(request,error):return response(error_body(Error('VALIDATION_ERROR','请求参数格式或长度不合法。',422),getattr(request.state,'request_id',None)),422)
    @app.exception_handler(StarletteHTTPException)
    async def http_exception(request,error):
        # Native invitation exceptions can contain URLs or credential values.
        messages={400:'请求参数无效。',401:'请先登录。',403:'没有操作权限。',404:'请求地址不存在。',409:'请求状态冲突。',422:'请求参数无效。',429:'操作过于频繁，请稍后重试。'}
        codes={401:'AUTH_REQUIRED',403:'PERMISSION_DENIED',404:'NOT_FOUND',429:'RATE_LIMIT_EXCEEDED'}
        return response(error_body(Error(codes.get(error.status_code,'VALIDATION_ERROR' if error.status_code<500 else 'INTERNAL_ERROR'),messages.get(error.status_code,'服务暂时不可用，请稍后重试。'),error.status_code),getattr(request.state,'request_id',None)),error.status_code)
    @app.exception_handler(Exception)
    async def unexpected(request,error):
        log.error('Unhandled request error type=%s requestId=%s',type(error).__name__,getattr(request.state,'request_id',None))
        return response(error_body(Error('INTERNAL_ERROR','服务暂时不可用，请稍后重试。',500),getattr(request.state,'request_id',None)),500)
    @app.middleware('http')
    async def boundary(request,call_next):
        incoming=request.headers.get('x-request-id','');request_id=incoming if re.fullmatch('[A-Za-z0-9_.:-]{1,96}',incoming) else str(uuid4());request.state.request_id=request_id;request.state.principal=None;context=request_id_context.set(request_id)
        origin=request.headers.get('origin');allowed=normalize_cors_origins(config.get('runtime.cors_origins',''))
        try:normalized_origin=normalize_cors_origin(origin) if origin else None
        except ValueError:normalized_origin=None
        try:
            # Reject proxy spoofing before body processing, authentication,
            # rate-limit writes, or any business/upstream action.
            request.state.client_ip=resolver.resolve(request)
            if request.method=='OPTIONS' and request.headers.get('access-control-request-method'):
                requested_headers={v.strip().lower() for v in request.headers.get('access-control-request-headers','').split(',') if v.strip()}
                if normalized_origin not in allowed or request.headers.get('access-control-request-method') not in ('GET','POST','PATCH','PUT','DELETE','OPTIONS') or not requested_headers.issubset({h.lower() for h in ALLOWED_HEADERS.split(',')}):raise Error('PERMISSION_DENIED','跨域请求未获授权。',403)
                out=Response(status_code=204);out.headers['Access-Control-Allow-Methods']='GET,POST,PATCH,PUT,DELETE,OPTIONS';out.headers['Access-Control-Allow-Headers']=ALLOWED_HEADERS;out.headers['Access-Control-Max-Age']='600'
            else:
                if origin and normalized_origin not in allowed:raise Error('PERMISSION_DENIED','跨域请求未获授权。',403)
                webhook=request.method=='POST' and request.url.path.startswith('/api/v1/sms/webhook/')
                body_limit=65536 if webhook else 1048576
                length=request.headers.get('content-length')
                if length:
                    try:size=int(length)
                    except ValueError:raise Error('VALIDATION_ERROR','内容长度无效。',422)
                    if size<0 or size>body_limit:raise Error('PAYLOAD_TOO_LARGE','请求内容过大。',413)
                # Buffer up to the request cap, including chunked bodies lacking length.
                if request.method not in ('GET','HEAD'):
                    chunks=[];size=0
                    async for chunk in request.stream():
                        size+=len(chunk)
                        if size>body_limit:raise Error('PAYLOAD_TOO_LARGE','请求内容过大。',413)
                        chunks.append(chunk)
                    request._body=b''.join(chunks)
                if csrf_required(request):
                    cookie=request.cookies.get('lc_csrf');header=request.headers.get('x-csrf-token')
                    if not cookie or not header or not secrets.compare_digest(cookie,header):raise Error('CSRF_INVALID','安全校验失败，请刷新页面后重试。',403)
                path=request.url.path
                if path.startswith('/api/v1/admin/'):
                    if path in AUTH_OPEN:
                        # 登录类接口本身就是爆破入口：先判封禁，再进验证码与密码校验。
                        if ban_guard is not None:await run_in_threadpool(ban_guard.assert_allowed,request.state.client_ip)
                    else:
                        auth=request.headers.get('authorization','');token=auth[7:].strip() if auth.startswith('Bearer ') else request.cookies.get('lc_admin_access','')
                        # 没带凭证就先给 401，不必为一次注定失败的请求去查封禁表。
                        if not token:raise Error('AUTH_REQUIRED','请先登录。',401)
                        # 有凭证才判封禁，且在鉴权之前：被封 IP 即使拿着有效会话也要被挡住。
                        if ban_guard is not None:await run_in_threadpool(ban_guard.assert_allowed,request.state.client_ip)
                        request.state.principal=await run_in_threadpool(services.auth.authenticate,token)
                        if request.state.principal.totp_setup_required and not (path.startswith('/api/v1/admin/auth/totp') or path in ('/api/v1/admin/auth/me','/api/v1/admin/auth/logout')):raise Error('AUTH_TOTP_REQUIRED','请先完成双重验证设置。',403)
                await run_in_threadpool(common_rate,request,services)
                out=await call_next(request)
        except Error as error:
            out=response(error_body(error,request_id),error.status)
            retry_after=getattr(error,'retry_after',None)
            if retry_after is not None:out.headers['Retry-After']=str(retry_after)
            elif error.status==429:out.headers['Retry-After']='60'
        except Exception as error:
            log.error('Unhandled request error type=%s requestId=%s',type(error).__name__,request_id)
            out=response(error_body(Error('INTERNAL_ERROR','服务暂时不可用，请稍后重试。',500),request_id),500)
        finally:request_id_context.reset(context)
        out.headers['X-Request-Id']=request_id;out.headers['X-Content-Type-Options']='nosniff';out.headers['Referrer-Policy']='no-referrer';out.headers['X-Frame-Options']='DENY';out.headers['Permissions-Policy']='camera=(), microphone=(), geolocation=()'
        # React/Ant Design use inline styles, but no inline or third-party script
        # is required by the local arithmetic login challenge or redeem pages.
        base_policy="'self'" if is_admin_path(request.url.path,admin_base) else "'none'"
        out.headers['Content-Security-Policy']="default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri "+base_policy
        if request.url.path.startswith('/api/'):
            out.headers['Cache-Control']='no-store';out.headers['Pragma']='no-cache'
        if out.status_code==429:out.headers.setdefault('Retry-After','60')
        if config.flag('runtime.security_hsts_enabled',False):out.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
        if normalized_origin in allowed:out.headers['Access-Control-Allow-Origin']=origin;out.headers['Access-Control-Allow-Credentials']='true';out.headers['Access-Control-Expose-Headers']='X-Request-Id,Retry-After';out.headers.append('Vary','Origin')
        if not request.cookies.get('lc_csrf'):out.set_cookie('lc_csrf',secrets.token_urlsafe(32),httponly=False,secure=config.flag('runtime.secure_cookies',True),samesite='lax',path='/')
        return out
    for name in ('auth','audit','catalog','inventory','settings','staff','jobs','notifications','ops'):app.include_router(importlib.import_module('.common.'+name,__package__).router)
    if include_domains:
        from .sms.routes import router as sms_router
        from .invitation.router import build_router
        for router in (sms_router,build_router(services)):app.include_router(router)
    register_frontend(app,config,admin_base)
    return app
