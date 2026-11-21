"""Authenticated adapter around the copied invitation application's complete API."""
from __future__ import annotations
import functools
import inspect
import json
import re
from pathlib import Path
from uuid import UUID
from fastapi import APIRouter, Body, Depends, Request, Response
from fastapi.routing import APIRoute
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from ..errors import Error, bad, ok
from ..public_limits import cards_rate,invitation_action_rate,invitation_recovery_rate
from . import mysql
from .permits import INVITE_SLOTS
from .safety import redact, require_binary, require_proxy
from .redeem_attempts import public_invitation_error
from .phone_details import attach_phone_details
# 邀请并发上限与工位区间必须同一个来源，否则配置能存进一个跑不起来的值。
INVITE_SLOTS_MAX=INVITE_SLOTS[1]

PREFIX='/api/v1/admin/invitation/native'
SENSITIVE={'password','refresh_token','access_token','session_token','id_token','cookie_header','csrf_token','totp_secret','auth_json','proxy','proxy_used','url','relay_url','code','cdk_code','token','secret','api_key','phone_number','phone_code'}
LOG_FIELDS={'text','error','last_error','detail','fail_reason','message','line','stdout','stderr','stdout_tail'}
# 授权凭据的文本/文件导出就把内容装在 `text` 里；它和"日志文本"同名，所以必须
# 单独认出端点，否则有权限的管理员导出的也是被脱敏过的占位文本。
CREDENTIAL_EXPORT_SUFFIXES=('/export','/export_to_panel')
# These controls affect a process that must read its own account credentials.
# Arbitrary model instructions cannot be made safe by response redaction.
CLI_CONTROL_DEFAULTS={'sayhi_prompt':'你好','sayhi_model':'gpt-5.6-luna','codex_binary':'codex','codex_home_root':'','codex_originator':'codex_work_desktop'}
CLI_UNSUPPORTED_ALIASES={'prompt','model','command','args','argv','cli_args','codex_bin','codex_redeem_prompt','codex_redeem_model','codex_redeem_originator','env'}

def reject_cli_overrides(payload,allow_controls=False,depth=0):
    if depth>10:raise bad('请求参数嵌套过深。')
    for key,value in payload.items() if isinstance(payload,dict) else enumerate(payload):
        name=str(key).lower()
        if name in CLI_UNSUPPORTED_ALIASES or name in CLI_CONTROL_DEFAULTS and not allow_controls:raise bad('此接口不接受 CLI 指令或执行参数。')
        if isinstance(value,(dict,list)):reject_cli_overrides(value,depth=depth+1)

def cleaned(value,credentials,credential_export=False):
    if isinstance(value,dict):
        return {k:('***' if v and (k in SENSITIVE or re.search(r'password|secret|token|api[_-]?key',k,re.I)) and not credentials else redact(v) if k in LOG_FIELDS and isinstance(v,str) and not credential_export else cleaned(v,credentials,credential_export)) for k,v in value.items()}
    if isinstance(value,list):return [cleaned(v,credentials,credential_export) for v in value]
    if isinstance(value,str):return redact(value) if not credentials else value
    return value

def masked_setting(value):return isinstance(value,str) and value.strip() in ('***','[REDACTED]','<masked>','•••')


def public_operation(action, *args):
    try:
        return action(*args)
    except Error as exc:
        raise public_invitation_error(exc) from None

def sanitize_frame(frame):
    lines=[]
    for line in frame.split('\n'):
        if line.startswith('data: '):
            try:line='data: '+json.dumps(cleaned(json.loads(line[6:]),False),ensure_ascii=False)
            except (ValueError,TypeError):line=redact(line)
        else:line=redact(line)
        lines.append(line)
    return '\n'.join(lines)

async def redacted_stream(iterator):
    pending=''
    async for chunk in iterator:
        pending+=chunk.decode('utf-8','replace') if isinstance(chunk,bytes) else str(chunk)
        if len(pending)>1048576:raise RuntimeError('Native event frame exceeds the response boundary')
        while '\n\n' in pending:
            frame,pending=pending.split('\n\n',1)
            yield sanitize_frame(frame)+'\n\n'
    if pending:yield sanitize_frame(pending)

def build_router(services):
    router=APIRouter();inv=services.invitation
    @router.get('/api/v1/admin/invitation/executor')
    def executor_status(request:Request,response:Response):
        services.permissions.require(request,'invitation.read');response.headers['Cache-Control']='no-store'
        return ok(inv.executor_status())
    @router.put('/api/v1/admin/invitation/executor')
    def executor_update(request:Request,response:Response,body:dict=Body(...)):
        actor=services.permissions.require(request,'invitation.manage');response.headers['Cache-Control']='no-store'
        if set(body)!={'enabled'} or not isinstance(body['enabled'],bool):raise bad('请提供布尔值启用设置。')
        return ok(inv.set_executor_enabled(body['enabled'],actor))
    @router.get('/api/v1/admin/invitation/pool')
    def pool_status(request:Request,response:Response):
        services.permissions.require(request,'invitation.read');response.headers['Cache-Control']='no-store'
        return ok(inv.pool_status())
    @router.put('/api/v1/admin/invitation/pool')
    def pool_update(request:Request,response:Response,body:dict=Body(...)):
        """账号工厂开关：与邀请执行互不影响，自己一套。"""
        actor=services.permissions.require(request,'invitation.manage');response.headers['Cache-Control']='no-store'
        if set(body)!={'enabled'} or not isinstance(body['enabled'],bool):raise bad('请提供布尔值启用设置。')
        return ok(inv.set_pool_enabled(body['enabled'],actor))
    @router.post('/api/v1/admin/invitation/pool/stop')
    def pool_stop(request:Request,response:Response):
        """手动停止本轮补号（join 正在跑的注册线程）并关闭账号工厂开关。"""
        actor=services.permissions.require(request,'invitation.manage');response.headers['Cache-Control']='no-store'
        return ok(inv.stop_pool_round(actor))
    @router.post('/api/v1/admin/invitation/jobs/{job_id}/stop')
    def job_stop(request:Request,response:Response,job_id:str):
        """手动停止一个邀请作业；终态幂等返回，绝不把卡密改写成成功。"""
        actor=services.permissions.require(request,'invitation.manage');response.headers['Cache-Control']='no-store'
        if not 8<=len(job_id)<=160:raise bad('请提供有效的作业标识。')
        return ok(inv.stop_job(job_id,actor))
    for name in ('sms-monitor','sms-refund'):
        def background_status(request:Request,response:Response,_name=name):
            services.permissions.require(request,'invitation.read');response.headers['Cache-Control']='no-store'
            return ok(inv.background_worker_status(_name))
        def background_update(request:Request,response:Response,body:dict=Body(...),_name=name):
            actor=services.permissions.require(request,'invitation.manage');response.headers['Cache-Control']='no-store'
            if set(body)!={'enabled'} or not isinstance(body['enabled'],bool):raise bad('请提供布尔值启用设置。')
            return ok(inv.set_background_worker(_name,body['enabled'],actor))
        background_status.__name__='background_status_'+name.replace('-','_')
        background_update.__name__='background_update_'+name.replace('-','_')
        router.add_api_route('/api/v1/admin/invitation/'+name,background_status,methods=['GET'])
        router.add_api_route('/api/v1/admin/invitation/'+name,background_update,methods=['PUT'])
    @router.get('/api/v1/invitation/page-content')
    def page(request:Request,response:Response):
        response.headers['Cache-Control']='no-store';return ok(public_operation(inv.page_content))
    @router.post('/api/v1/invitation/redeem')
    def redeem(request:Request,response:Response,body:dict=Body(...)):
        response.headers['Cache-Control']='no-store'
        codes=body.get('codes')
        cards_rate(request,'invitation',codes if isinstance(codes,list) and 1<=len(codes)<=10 else [])
        public_operation(reject_cli_overrides,dict(request.query_params));public_operation(reject_cli_overrides,body)
        return ok(public_operation(inv.redeem,codes,body.get('attemptId'),body.get('recoverySecret')))
    @router.get('/api/v1/invitation/redemption-attempts/{attempt_id}')
    def recover_redemption(attempt_id:str,request:Request,response:Response):
        response.headers['Cache-Control']='no-store';invitation_recovery_rate(request,attempt_id,request.headers.get('x-session-secret'))
        return ok(public_operation(inv.redemption_attempt_status,attempt_id,request.headers.get('x-session-secret')))
    def public_action(action):
        def endpoint(request:Request,response:Response,body:dict=Body(...)):
            response.headers['Cache-Control']='no-store';invitation_action_rate(request,body.get('sessions',body.get('entries')),action)
            public_operation(reject_cli_overrides,dict(request.query_params))
            public_operation(reject_cli_overrides,body)
            if action=='start':public_operation(inv.require_executor_accepting)
            return ok(public_operation(inv.action,body.get('sessions',body.get('entries')),action))
        return endpoint
    for action in ('start','status','invite-sent'):
        router.add_api_route('/api/v1/invitation/'+action,public_action(action),methods=['POST'])

    async def boundary(request:Request):
        write=request.method not in ('GET','HEAD')
        actor=services.permissions.require(request,'invitation.manage' if write else 'invitation.read')
        path=request.url.path.removeprefix(PREFIX)
        needs_credentials=path.endswith(('/export','/export_to_panel','/update_credentials','/codex_auth')) or path.startswith('/api/registered/') and request.method=='GET'
        if needs_credentials:services.permissions.require(request,'invitation.credentials')
        # 读与写必须各占一个桶。后台页面每 5 秒轮询，一个页面的几个 hook 就能
        # 打到 30~70 次/分钟；以前读写共用一个计数器，轮询先把「写」的 30 次/分钟
        # 额度吃光，于是代理池「测试」这类写操作必然 429「操作过于频繁」，而且只要
        # 页面还开着就一直被拒。读额度按后台自身轮询基线（一个页面最多约 6 个
        # hook × 12 次/分钟）留出余量，写额度维持 30 次/分钟不变。
        services.limiter.check('invitation.native:'+actor.id+(':write' if write else ':read'),30 if write else 240,60)
        reject_cli_overrides(dict(request.query_params))
        for key,value in request.query_params.multi_items():
            if len(value)>1000:raise bad('查询参数过长。')
            if key in ('limit','tail_kb','stale_seconds','offset'):
                try:number=int(value)
                except ValueError:raise bad('查询参数不合法。')
                if number<0 or key!='offset' and number>5000 or number>1000000:raise bad('查询范围过大。')
        payload=await request.json() if write and request.headers.get('content-type','').startswith('application/json') else {}
        if not isinstance(payload,dict):raise bad('请求参数不合法。')
        reject_cli_overrides(payload,allow_controls=path=='/api/settings/invite')
        if path=='/api/settings/invite' and write:
            for key,default in CLI_CONTROL_DEFAULTS.items():
                if key not in payload or payload[key] is None:continue
                current=str(inv.repo.get_setting(key,default) or default).strip()
                proposed=str(payload[key]).strip() or default
                if proposed!=current:
                    services.permissions.require(request,'invitation.credentials')
                    break
        for key,value in payload.items():
            if isinstance(value,list) and len(value)>5000:raise bad('一次最多处理 5000 条记录。')
            if isinstance(value,str) and len(value)>500000:raise bad('请求参数过长。')
            if key in ('proxy','proxy_url') and value:require_proxy(value)
        if path=='/api/settings/invite' and write:
            integer_bounds={'cdk_max_concurrency':(1,INVITE_SLOTS_MAX),'invite_max_attempts':(1,20),'invite_wait_timeout':(10,3600),'invite_otp_timeout':(10,3600),'cli_timeout':(10,3600),'sentinel_behavior_ms':(0,10000),'cookie_jar_sso_max_minutes':(0,1440),'proxy_409_threshold':(1,100),'proxy_freeze_minutes':(1,1440),'pool_min_available':(0,1000),'pool_register_concurrency':(1,10),'pool_check_seconds':(10,3600),'pool_otp_timeout':(10,3600)}
            for key,(lo,hi) in integer_bounds.items():
                if key in payload:
                    try:number=int(payload[key])
                    except (ValueError,TypeError):raise bad('执行配置参数不合法。')
                    if not lo<=number<=hi:raise bad('执行配置超出允许范围。')
            if payload.get('codex_binary'):
                import shutil
                require_binary(shutil.which(payload['codex_binary']) or payload['codex_binary'])
            if payload.get('codex_home_root'):
                from .process import managed_home_directory
                try:managed_home_directory(payload['codex_home_root'])
                except (ValueError,TypeError):raise bad('执行目录必须是独立 Codex 临时目录的子目录。')
            for key in ('sayhi_model','codex_originator'):
                value=payload.get(key)
                if value is not None and (not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,99}',value)):raise bad('模型或客户端标识不合法。')
            prompt=payload.get('sayhi_prompt')
            if prompt is not None and (not isinstance(prompt,str) or not 1<=len(prompt)<=2000 or '\0' in prompt):raise bad('CLI 内容过长或不合法。')
        # Run log paths are imported data, never authority to read arbitrary files.
        run_id=request.path_params.get('run_id')
        if run_id:
            row=services.db.one('SELECT log_path FROM invitation_v2_runs WHERE run_id=?',mysql.marker(run_id))
            if not row:raise Error('NOT_FOUND','运行记录不存在。',404)
            if row.get('log_path') and not Path(row['log_path']).resolve().is_relative_to(inv.log_dir):raise Error('PERMISSION_DENIED','日志位于允许目录之外。',403)
        if path=='/api/settings/runtime_check':
            from .process import managed_home_directory
            home=inv.repo.get_setting('codex_home_root','')
            if home:
                try:managed_home_directory(home)
                except (ValueError,TypeError):raise Error('PERMISSION_DENIED','执行目录尚未迁移至独立 Codex 临时目录。',403)
        if path in ('/api/register','/api/registered/codex_auth'):inv.require_executor_accepting()
        credentials_allowed=services.permissions.allows(actor,'invitation.credentials')
        # 原来只记「写」。但大量 GET 会按 invitation.credentials 揭示邮箱密码 / 2FA 种子 /
        # Codex RT / 运行记录里的卡密明文 —— 读走这些必须留痕，否则等于无痕取走全部资产。
        if write or credentials_allowed:
            services.audit.record(actor.id,'invitation.native.'+path.removeprefix('/api/').replace('/','.'),'invitation',None,{'fields':sorted(payload),'write':bool(write)})
        request.state.invitation_credentials=credentials_allowed
        # 只有授权的**导出**端点才豁免日志字段脱敏：导出结果本身就装在 `text` 里，
        # 而日志/错误/实时流必须继续脱敏。
        request.state.invitation_credential_export=bool(
            request.state.invitation_credentials and path.endswith(CREDENTIAL_EXPORT_SUFFIXES))
        from .request_context import credential_export_context,credentials_context
        token=credentials_context.set(request.state.invitation_credentials)
        export_token=credential_export_context.set(request.state.invitation_credential_export)
        try:yield actor
        finally:
            credential_export_context.reset(export_token)
            credentials_context.reset(token)

    def wrapper(original):
        @functools.wraps(original)
        async def endpoint(*args,**kwargs):
            if original.__name__.startswith('api_save_') and 'config' in original.__name__:
                # Masked read responses are valid edit forms. A save must preserve
                # their real secrets/URLs instead of persisting the placeholder.
                for key,value in list(kwargs.items()):
                    if isinstance(value,BaseModel):kwargs[key]=value.model_copy(update={k:None for k,v in value.model_dump().items() if masked_setting(v)})
            result=await original(*args,**kwargs) if inspect.iscoroutinefunction(original) else await run_in_threadpool(original,*args,**kwargs)
            if isinstance(result,StreamingResponse):result.body_iterator=redacted_stream(result.body_iterator);return result
            if original.__name__ in ('api_runs','api_run_detail','api_run'):
                result=await run_in_threadpool(inv.attach_manual_sync_status,result)
            # 每一条运行行都要带卡密明细（`/api/runs` 与 `/api/runs/{id}/log`）。
            if original.__name__ in ('api_runs','api_run_detail','api_run','api_run_log'):
                result=await run_in_threadpool(inv.attach_cdk_details,result)
            if original.__name__ in ('api_accounts','api_registered','api_registered_one'):
                result=await run_in_threadpool(attach_phone_details,result,inv.repo,inv.log_dir)
            # Sensitive native reads require a dedicated permission. Structured
            # response masking is performed by the explicit request dependency.
            request=kwargs.get('request')
            credentials=bool(request and getattr(request.state,'invitation_credentials',False))
            credential_export=bool(request and getattr(request.state,'invitation_credential_export',False))
            # Endpoints without a Request still run in the same request context.
            from .request_context import credential_export_context,credentials_context
            return cleaned(result,credentials or credentials_context.get(),
                           credential_export or credential_export_context.get()) if isinstance(result,(dict,list)) else result
        endpoint.__signature__=inspect.signature(original,eval_str=True)
        return endpoint

    # Preserve the native issuer's custom prefix/length/count while recording
    # every code in the same platform inventory ledger and permission boundary.
    @router.get(PREFIX+'/api/cdk/skus',dependencies=[Depends(boundary)])
    def skus(request:Request):
        services.permissions.require(request,'inventory.read')
        return {'items':services.db.all("SELECT id,name FROM skus WHERE status='ACTIVE' AND JSON_UNQUOTE(JSON_EXTRACT(attributes_json,'$.productKind'))='CODEX_INVITE_REDEEM' ORDER BY created_at,id")}
    @router.post(PREFIX+'/api/cdk/generate',dependencies=[Depends(boundary)])
    def generate(request:Request,body:dict=Body(...)):
        actor=services.permissions.require(request,'inventory.manage')
        services.permissions.require(request,'inventory.export');services.permissions.require(request,'invitation.credentials')
        return inv.generate_native(body,actor)

    destructive={'/api/cdk/reset','/api/cdk/void','/api/cdk/unbind','/api/cdk/delete'}
    account_mutations={'/api/import','/api/accounts/{email}','/api/accounts/bulk_delete','/api/accounts/reset_failed','/api/accounts/reset/{email}','/api/accounts/bulk_reset','/api/accounts/release_stale','/api/registered/{email}','/api/registered/bulk_delete','/api/registered/update_credentials'}
    for route in inv.native.app.routes:
        if not isinstance(route,APIRoute) or not route.path.startswith('/api/') or route.path=='/api/cdk/generate':continue
        endpoint=wrapper(route.endpoint)
        if route.path in account_mutations and route.methods-{'GET','HEAD'}:
            def account_mutation(original):
                @functools.wraps(original)
                def guarded(*args,**kwargs):
                    values=inspect.signature(original).bind_partial(*args,**kwargs).arguments
                    data={}
                    for key,value in values.items():
                        if isinstance(value,BaseModel):data.update(value.model_dump())
                        else:data[key]=value
                    emails={str(e).strip().lower() for e in data.get('emails') or []}
                    if data.get('email'):emails.add(str(data['email']).strip().lower())
                    broad=bool(data.get('all') or data.get('status')) or not emails and 'text' not in data
                    with services.db.transaction():
                        # The allocation mutex prevents a job from claiming this
                        # mailbox between the check and the account mutation.
                        if not services.db.one('SELECT slot_id FROM invitation_v2_execution_permits WHERE slot_id=0 FOR UPDATE'):raise Error('INVITATION_EXECUTOR_UNAVAILABLE','执行保护尚未安装。',503)
                        active=services.db.all('SELECT email FROM invitation_v2_execution_permits WHERE owner_token IS NOT NULL UNION SELECT j.email FROM invitation_v2_task_holds h JOIN invitation_v2_cdk_jobs j ON j.job_id=h.job_id WHERE h.resolved_at IS NULL')
                        imported=str(data.get('text') or '').lower()
                        if any(r['email'] and (broad or r['email'] in emails or imported and r['email'] in imported) for r in active):raise Error('INVITATION_RUNNING','邮箱有执行中或结果不明的任务，不能修改。',409)
                        return original(*args,**kwargs)
                guarded.__signature__=inspect.signature(original,eval_str=True)
                return guarded
            endpoint=wrapper(account_mutation(route.endpoint))
        if route.path in destructive:
            def mutation(original):
                @functools.wraps(original)
                def guarded(req):
                    with services.db.transaction():
                        codes=req.codes if hasattr(req,'codes') else [req.code]
                        inv._lock_codes([mysql.marker(str(code).strip()) for code in codes])
                        for code in sorted(set(codes)):
                            marker=mysql.marker(str(code).strip())
                            inv._require_no_holds(marker)
                            row=services.db.one('SELECT status,email FROM invitation_v2_cdk_codes WHERE code=? FOR UPDATE',marker)
                            if row and row['status'] in ('queued','running'):raise Error('INVITATION_RUNNING','执行中的卡密不能修改。',409)
                            if row:inv._require_mailbox_idle(row.get('email'))
                            if original.__name__ in ('api_cdk_reset','api_cdk_unbind','api_cdk_delete'):services.db.execute('DELETE FROM invitation_v2_sessions WHERE code_id=?',marker)
                            if original.__name__ in ('api_cdk_void','api_cdk_delete'):
                                services.db.execute("UPDATE inventory_items i JOIN invitation_v2_platform_keys p ON p.inventory_item_id=i.id SET i.status='INVALID' WHERE p.code_id=?",marker)
                        return original(req)
                guarded.__signature__=inspect.signature(original,eval_str=True)
                return guarded
            endpoint=wrapper(mutation(route.endpoint))
        router.add_api_route(PREFIX+route.path,endpoint,methods=list(route.methods),dependencies=[Depends(boundary)],response_class=route.response_class)
    from .legacy_router import router as legacy_router
    router.include_router(legacy_router)
    from .dispatch import router as dispatch_router
    from .permits import holds_router, router as permit_router
    router.include_router(permit_router)
    router.include_router(holds_router)
    router.include_router(dispatch_router)
    return router
