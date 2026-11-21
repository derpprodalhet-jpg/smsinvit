from __future__ import annotations
import json
import re
import secrets
import threading
import time
from datetime import datetime,timezone
from urllib.parse import quote
from uuid import uuid4
from fastapi import APIRouter,Request,Response,Body,Query
from .mapper import Mapper
from ..errors import Error,ok,bad
from ..security import ctx,principal,client_ip,require_permission,Principal,b64url,unb64url,totp_secret,totp_verify

router=APIRouter()

def bounded(value,maxlen,required=False,minlen=0):
    if value is None:
        if required:raise bad('缺少必填参数。')
        return value
    if not isinstance(value,str) or len(value)>maxlen or len(value)<minlen or required and not value.strip():raise bad('参数格式或长度不合法。')
    return value

def object_body(value):
    if not isinstance(value,dict):raise bad('请求体必须为对象。')
    return value

def instant(value):
    if value is None:return None
    if hasattr(value,'isoformat'):return value.isoformat()+'Z' if getattr(value,'tzinfo',None) is None else value.isoformat().replace('+00:00','Z')
    return str(value)

def utc_instant(value):
    """把 MySQL 的 DATETIME 或 SQLite 的字符串统一成带时区的 UTC 时间。"""
    if value is None:return None
    if isinstance(value,str):
        text=value.strip().replace(' ','T')
        if text.endswith('Z'):text=text[:-1]
        try:value=datetime.fromisoformat(text)
        except ValueError:return None
    if not hasattr(value,'tzinfo'):return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

def remaining_seconds(value):
    until=utc_instant(value)
    if until is None:return 0
    return max(0,int((until-datetime.now(timezone.utc)).total_seconds()))

class RateLimiter:
    def __init__(self,db,codec):self.db,self.codec=db,codec;self.mapper=Mapper(db,'RateLimitMapper')
    def check(self,bucket,limit,window_seconds=None):
        hashed=self.codec.sha256(bucket)
        remaining=None
        with self.db.transaction(requires_new=True):
            if window_seconds is None:self.mapper.bumpAuth(hashed);attempts=self.mapper.authAttempts(hashed)
            else:
                self.mapper.bump(hashed,max(1,window_seconds));attempts=self.mapper.attempts(hashed)
                # 复核窗口的真实剩余时间：桶的窗口从**首次**请求起算，所以固定回一个整窗口
                # 会让客户端多等（典型是 900 秒里只用了 80 秒，却被告知再等 900 秒）。
                row=self.mapper.window(hashed)
                if row and row.get('remainingSeconds') is not None:remaining=int(row['remainingSeconds'])
        if attempts is not None and attempts>limit:
            error=Error('RATE_LIMIT_EXCEEDED','操作过于频繁，请稍后重试。',429)
            fallback=max(1,window_seconds or 900)
            error.retry_after=max(1,remaining) if remaining is not None else fallback
            raise error

class AdminIpGuard:
    """后台登录爆破防护：同一 IP 连续 5 次密码错误，封禁 24 小时。

    计数只包含密码错误（`AUTH_INVALID_CREDENTIALS`，账号不存在返回同款报错，一并计入）；
    验证码错误、TOTP 错误与限流 429 都不计入，否则把「验证码输错」当爆破会误伤真管理员。
    登录成功即清空该 IP 的计数行，语义是「连续」而不是「累计」。

    `assert_allowed` 位于每个 `/api/v1/admin/*` 请求的鉴权之前，所以被封的 IP 连登录页
    发出的请求也拿不到 401/403 之外的任何东西。命中封禁的 IP 会被内存缓存 15 秒，
    避免攻击者用高频请求把主键查询放大成数据库压力；缓存在解封时立即失效。
    """

    FAILURE_LIMIT = 5
    BAN_HOURS = 24
    WINDOW_SECONDS = 24 * 60 * 60
    CACHE_SECONDS = 15

    def __init__(self, db, codec):
        self.db, self.codec = db, codec
        self.mapper = Mapper(db, 'AdminIpBanMapper')
        self._banned = {}
        self._lock = threading.RLock()

    def key(self, ip):
        return self.codec.sha256('admin-ip-ban:' + str(ip or 'unknown'))

    def _cached(self, key):
        """命中缓存的封禁：返回真实剩余秒数，而不是缓存本身的存活时间。"""
        now = time.monotonic()
        with self._lock:
            entry = self._banned.get(key)
            if entry is None: return None
            if entry[0] <= now: self._banned.pop(key, None); return None
            return max(0, int(entry[1] - time.time()))

    def _remember(self, key, seconds):
        # 同时记住「缓存何时过期」与「封禁何时到期」，否则命中缓存的那次请求会把
        # 15 秒的缓存寿命当成剩余封禁时间报给客户端。
        seconds = max(0, int(seconds))
        with self._lock: self._banned[key] = (time.monotonic() + self.CACHE_SECONDS, time.time() + seconds)

    def forget(self, key=None):
        with self._lock:
            if key: self._banned.pop(key, None)
            else: self._banned.clear()

    def state(self, ip):
        key = self.key(ip)
        cached = self._cached(key)
        if cached is not None:
            return {'key': key, 'banned': cached > 0, 'secondsRemaining': cached, 'failureCount': None}
        # 只读判定不包显式事务：单条主键 SELECT 不需要事务，而且这样任何只提供 one()
        # 的数据库替身都能跑（测试里的桩就没有 transaction()）。
        row = self.mapper.find(key) or {}
        seconds = remaining_seconds(row.get('bannedUntil'))
        if seconds > 0: self._remember(key, seconds)
        return {'key': key, 'banned': seconds > 0, 'secondsRemaining': seconds, 'failureCount': int(row.get('failureCount') or 0)}

    def assert_allowed(self, ip):
        state = self.state(ip)
        if state['banned']:
            error = Error('IP_BANNED', '该 IP 因多次登录失败已被临时封禁，请稍后再试。', 403)
            error.retry_after = max(1, state['secondsRemaining'])
            raise error

    def record_failure(self, ip, identifier=None):
        key = self.key(ip)
        address = str(ip or 'unknown')[:64]
        account = (str(identifier).strip().lower()[:255] or None) if identifier else None
        with self.db.transaction():
            self.mapper.bumpFailure(key, address, account, self.WINDOW_SECONDS)
            row = self.mapper.find(key) or {}
            count = int(row.get('failureCount') or 0)
            if count >= self.FAILURE_LIMIT and remaining_seconds(row.get('bannedUntil')) <= 0:
                # 满了阈值且当前没有生效中的封禁（重复触发不会把到期时间一再推后）。
                self.mapper.ban(key, self.BAN_HOURS, 'PASSWORD_FAILURES')
                banned = self.mapper.find(key) or {}
                self._remember(key, remaining_seconds(banned.get('bannedUntil')))
                return {'banned': True, 'failureCount': count, 'banHours': self.BAN_HOURS}
        return {'banned': False, 'failureCount': count, 'banHours': self.BAN_HOURS}

    def clear(self, ip):
        key = self.key(ip); self.forget(key)
        with self.db.transaction(): self.mapper.clear(key)

    def entries(self, limit=100):
        rows = self.mapper.list(max(1, min(int(limit), 500))) or []
        items = []
        for row in rows:
            items.append({'ipHash': row.get('ipHash'), 'ipAddress': row.get('ipAddress'), 'failureCount': int(row.get('failureCount') or 0),
                          'lastFailureAt': instant(row.get('lastFailureAt')), 'lastIdentifier': row.get('lastIdentifier'),
                          'banned': remaining_seconds(row.get('bannedUntil')) > 0, 'bannedUntil': instant(row.get('bannedUntil')),
                          'banReason': row.get('banReason')})
        # 封禁中的排前面，其余按最后失败时间倒序 —— 在没有 MySQL 时间函数的替换库上也一致。
        items.sort(key=lambda item: (item['banned'], item['lastFailureAt'] or ''), reverse=True)
        return items

    def unban(self, key):
        key = str(key or '').strip().lower()
        if not re.fullmatch('[0-9a-f]{64}', key): raise bad('封禁标识无效。')
        self.forget(key)
        with self.db.transaction(): removed = self.mapper.clear(key)
        return {'removed': int(removed or 0)}

class Captcha:
    """Server-selected arithmetic challenge, consumed durably on its first attempt."""
    def __init__(self,codec,tokens,config,db=None):
        self.codec,self.tokens,self.config,self.db=codec,tokens,config,db
        self.mapper=Mapper(db,'AdminAuthMapper') if db is not None else None
    def issue(self,kind=None):
        if kind not in (None,'math'):raise bad('仅支持计算验证码。')
        if self.db is None:raise Error('CAPTCHA_UNAVAILABLE','验证码暂时不可用，请稍后重试。',503)
        a,b=secrets.randbelow(20)+1,secrets.randbelow(20)+1
        if secrets.randbelow(2):answer=a+b;prompt=f'{a} + {b} = ?'
        else:answer=abs(a-b);prompt=f'{max(a,b)} − {min(a,b)} = ?'
        payload={'exp':int(time.time())+300,'ans':self.codec.encrypt(str(answer)),'typ':'math','nonce':self.codec.random_token(16)}
        raw=b64url(json.dumps(payload,separators=(',',':')).encode())
        signed=b64url(__import__('hmac').new(self.tokens.key,raw.encode(),__import__('hashlib').sha256).digest())
        with self.db.transaction(requires_new=True):
            self.mapper.deleteExpiredCaptchaChallenges()
            self.mapper.insertCaptchaChallenge(self.codec.sha256('captcha-login:'+payload['nonce']))
        return {'captchaToken':raw+'.'+signed,'expiresInSeconds':300,'type':'math','prompt':prompt,'image':None}
    def verify_local(self,token,answer):
        import hmac,hashlib
        if not token:raise Error('CAPTCHA_REQUIRED','请完成验证码。',422)
        if self.db is None:raise Error('CAPTCHA_UNAVAILABLE','验证码暂时不可用，请稍后重试。',503)
        try:
            if not isinstance(token,str) or len(token)>512:raise ValueError()
            raw,signature=token.split('.')
            if not hmac.compare_digest(b64url(hmac.new(self.tokens.key,raw.encode(),hashlib.sha256).digest()),signature):raise ValueError()
            payload=json.loads(unb64url(raw))
            if not isinstance(payload,dict) or payload.get('typ')!='math' or not re.fullmatch('[0-9a-f]{32}',str(payload.get('nonce',''))):raise ValueError()
        except (ValueError,TypeError,KeyError,UnicodeError):raise Error('CAPTCHA_INVALID','验证码无效或已过期，请刷新后重试。',401)
        if type(payload.get('exp')) is not int or payload['exp']<=int(time.time()):raise Error('CAPTCHA_EXPIRED','验证码已过期，请刷新后重试。',401)
        # Commit before checking the answer/password. A failed login must not
        # roll this consumption back or let concurrent requests reuse the token.
        with self.db.transaction(requires_new=True):
            consumed=self.mapper.consumeCaptchaChallenge(self.codec.sha256('captcha-login:'+payload['nonce']))
        if consumed!=1:raise Error('CAPTCHA_INVALID','验证码已使用或已过期，请刷新后重试。',401)
        try:
            if not isinstance(answer,str) or not re.fullmatch(r'\s*[0-9]{1,2}\s*',answer):raise ValueError()
            expected=int(self.codec.decrypt(payload['ans']));provided=int(answer.strip())
        except (ValueError,TypeError,KeyError):raise Error('CAPTCHA_INVALID','验证码错误，请刷新后重试。',401)
        if expected!=provided:raise Error('CAPTCHA_INVALID','验证码错误，请刷新后重试。',401)
    def turnstile_enabled(self):return False
    def verify(self,body,ip):
        self.verify_local(body.get('captchaToken'),body.get('captchaAnswer'))

class AdminAuth:
    def __init__(self,db,codec,tokens,config,limiter,captcha):self.db,self.codec,self.tokens,self.config,self.limiter,self.captcha=db,codec,tokens,config,limiter,captcha;self.mapper=Mapper(db,'AdminAuthMapper')
    def roles(self,id):return self.mapper.findRoleCodes(str(id))
    def active(self,user):
        if user['status']!='ACTIVE':raise Error('USER_BANNED' if user['status']=='BANNED' else 'USER_DISABLED','账号不可用，请联系管理员。',403)
    def get(self,id,lock=False):
        user=self.mapper.findByIdForUpdate(str(id)) if lock else self.mapper.findById(str(id))
        if not user:raise Error('AUTH_INVALID_TOKEN','访问凭证无效。',401)
        self.active(user);return user
    def profile(self,user):
        enabled=user.get('totpEnabledAt') is not None and user.get('totpSecretEncrypted') is not None
        return {'id':user['id'],'username':user['username'],'email':user['email'],'displayName':user['displayName'],'status':user['status'],'roles':self.roles(user['id']),'totpEnabled':enabled,'totpSetupRequired':self.config.flag('runtime.totp_enabled',True) and not enabled,'createdAt':instant(user.get('createdAt'))}
    def authenticate(self,token):
        claims=self.tokens.verify(token)
        if not self.mapper.sessionExists(claims['sessionId'],claims['sub']):raise Error('AUTH_SESSION_EXPIRED','登录凭证已失效，请重新登录。',401)
        user=self.get(claims['sub']);roles=self.roles(claims['sub'])
        if not set(roles)&{'ADMIN','SUPER_ADMIN'}:raise Error('PERMISSION_DENIED','当前账号没有管理后台访问权限。',403)
        return Principal(str(user['id']),claims['sessionId'],tuple(roles),self.profile(user)['totpSetupRequired'])
    def issue(self,user,ip,agent):
        session=str(uuid4());refresh=self.codec.random_token(32)
        self.mapper.insertSession(session,str(user['id']),self.codec.sha256(refresh),ip,agent)
        self.mapper.updateLastLoginAt(str(user['id']))
        access=self.tokens.sign(user['id'],user['email'],user['username'],self.roles(user['id']),session)
        return {'accessToken':access,'refreshToken':refresh,'user':self.profile(user)}
    def login(self,body,ip,agent):
        identifier=bounded(body.get('identifier'),255,True).strip().lower();password=bounded(body.get('password'),128,True)
        for field,maxlen in [('captchaToken',512),('captchaAnswer',32),('turnstileToken',2048)]:bounded(body.get(field),maxlen)
        self.limiter.check('admin-login-ip:'+ip,50);self.limiter.check('admin-login-id:'+identifier,5)
        self.captcha.verify(body,ip)
        with self.db.transaction():
            user=self.mapper.findByUsernameOrEmailForUpdate(identifier)
            if not user or not self.codec.verify_password(user['passwordHash'],password):raise Error('AUTH_INVALID_CREDENTIALS','账号或密码错误。',401)
            self.active(user)
            if not set(self.roles(user['id']))&{'ADMIN','SUPER_ADMIN'}:raise Error('PERMISSION_DENIED','当前账号没有管理后台访问权限。',403)
            if user.get('totpEnabledAt') is not None:
                challenge=self.codec.random_token(24);self.mapper.insertTotpLoginChallenge(self.codec.sha256(challenge),str(user['id']))
                return {'user':self.profile(user),'needTotp':True,'challengeToken':challenge}
            return dict(self.issue(user,ip,agent),needTotp=False)
    def refresh(self,refresh,ip,agent):
        if not refresh:raise Error('AUTH_REQUIRED','缺少刷新凭证。',401)
        hashed=self.codec.sha256(bounded(refresh,256,True))
        with self.db.transaction():
            references=self.mapper.findAdminUserIdsByRefreshHash(hashed)
            if len(references)!=1:raise Error('AUTH_SESSION_EXPIRED','登录凭证已失效。',401)
            user=self.get(references[0],True);session=self.mapper.findSessionByRefreshHashForUpdate(hashed)
            if not session:raise Error('AUTH_SESSION_EXPIRED','登录凭证已失效。',401)
            self.mapper.revokeSession(str(session['id']));out=self.issue(user,ip,agent);out.pop('user');return out
    def backup_hashes(self,user):return list(json.loads(user.get('totpBackupCodesHash') or '[]'))
    def verify_second_factor(self,user,code):
        if not user.get('totpEnabledAt') or not user.get('totpSecretEncrypted'):raise Error('AUTH_TOTP_INVALID','验证码错误或已失效。',401)
        try:
            if totp_verify(self.codec.decrypt(user['totpSecretEncrypted']),code):return
        except ValueError:pass
        hashes=self.backup_hashes(user);normalized=re.sub('[^A-Z0-9]','',str(code).strip().upper())
        for i,hashed in enumerate(hashes):
            if self.codec.verify_password(hashed,normalized):
                hashes.pop(i);self.mapper.updateTotpBackupCodes(str(user['id']),json.dumps(hashes));return
        raise Error('AUTH_TOTP_INVALID','验证码错误或已失效。',401)
    def login_totp(self,challenge,code,ip,agent):
        bounded(challenge,128,True);bounded(code,32,True)
        self.limiter.check('admin-totp-ip:'+ip,30);self.limiter.check('admin-totp-challenge:'+challenge,10)
        hashed=self.codec.sha256(challenge)
        with self.db.transaction():
            rows=self.mapper.findTotpLoginChallenges(hashed)
            if not rows:raise Error('AUTH_CHALLENGE_EXPIRED','登录验证已过期。',401)
            user=self.get(rows[0]['adminUserId'],True)
            if self.mapper.consumeTotpLoginChallenge(hashed)!=1:raise Error('AUTH_CHALLENGE_EXPIRED','登录验证已过期。',401)
            self.verify_second_factor(user,code);return self.issue(user,ip,agent)
    def logout(self,actor):self.mapper.revokeSessionForAdmin(actor.session_id,actor.user_id)
    def totp_status(self,id):
        user=self.get(id);profile=self.profile(user)
        return {'totpEnabled':profile['totpEnabled'],'totpSetupRequired':profile['totpSetupRequired'],'enabledAt':instant(user.get('totpEnabledAt')),'backupCodesRemaining':len(self.backup_hashes(user))}
    def setup(self,id):
        with self.db.transaction():
            user=self.get(id,True)
            if user.get('totpEnabledAt') is not None:raise Error('AUTH_TOTP_ALREADY_ENABLED','双重验证已启用。',409)
            secret=totp_secret();key=self.codec.sha256('admin-totp-setup:'+id)
            self.mapper.upsertTotpSetupChallenge(key,id,self.codec.encrypt(secret))
            return {'secret':secret,'otpauthUrl':'otpauth://totp/'+quote('SmsInvit Admin:'+str(user['email']),safe='')+'?secret='+secret+'&issuer=SmsInvit%20Admin&algorithm=SHA1&digits=6&period=30','expiresInSeconds':600}
    def enable(self,actor,code):
        bounded(code,32,True);self.limiter.check('admin-totp-enable:'+actor.user_id,10)
        with self.db.transaction():
            user=self.get(actor.user_id,True)
            if user.get('totpEnabledAt') is not None:raise Error('AUTH_TOTP_ALREADY_ENABLED','双重验证已启用。',409)
            key=self.codec.sha256('admin-totp-setup:'+actor.user_id);rows=self.mapper.findChallengePayloadForUpdate(key)
            if not rows:raise Error('AUTH_CHALLENGE_EXPIRED','绑定会话已过期。',400)
            encrypted=rows[0]['payloadEncrypted']
            if not totp_verify(self.codec.decrypt(encrypted),code):raise Error('AUTH_TOTP_INVALID','验证码错误或已失效。',401)
            raw=[self.codec.random_token(4).upper() for _ in range(8)]
            self.mapper.enableTotp(actor.user_id,encrypted,json.dumps([self.codec.hash_password(c) for c in raw]));self.mapper.consumeChallengeByHash(key)
            self.mapper.revokeOtherSessions(actor.user_id,actor.session_id)
            return {'totpEnabled':True,'backupCodes':[c[:4]+'-'+c[4:] for c in raw]}
    def disable(self,id,password,code):
        bounded(password,128,True);bounded(code,32,True);self.limiter.check('admin-totp-disable:'+id,10)
        with self.db.transaction():
            user=self.get(id,True)
            if not self.codec.verify_password(user['passwordHash'],password):raise Error('AUTH_INVALID_CREDENTIALS','账号或密码错误。',401)
            self.verify_second_factor(user,code);self.mapper.disableTotp(id);self.mapper.revokeAllSessions(id)
            return {'totpEnabled':False}
    def change_password(self,actor,current,new):
        self.limiter.check('admin-change-password:'+actor.user_id,10)
        if not isinstance(new,str) or not 8<=len(new)<=128:raise Error('PASSWORD_TOO_WEAK','密码长度需为 8 至 128 位。',422)
        if current==new:raise Error('PASSWORD_UNCHANGED','新密码不能与当前密码相同。',422)
        with self.db.transaction():
            user=self.get(actor.user_id,True)
            if not self.codec.verify_password(user['passwordHash'],current):raise Error('AUTH_INVALID_CREDENTIALS','当前密码不正确。',401)
            self.mapper.updatePasswordHash(actor.user_id,self.codec.hash_password(new));self.mapper.revokeOtherSessions(actor.user_id,actor.session_id)
            return {'changed':True}

def issue_cookies(response,result,config):
    response.headers['Cache-Control']='no-store'
    if not result.get('accessToken'):return
    for name,value,httponly,age in [('lc_admin_access',result['accessToken'],True,900),('lc_admin_refresh',result['refreshToken'],True,2592000),('lc_admin_session','1',False,2592000)]:response.set_cookie(name,value,max_age=age,httponly=httponly,secure=config.flag('runtime.secure_cookies',True),samesite='lax',path='/')
def clear_cookies(response,config):
    for name in ('lc_admin_access','lc_admin_refresh','lc_admin_session'):response.delete_cookie(name,path='/',secure=config.flag('runtime.secure_cookies',True),httponly=name!='lc_admin_session',samesite='lax')

@router.get('/api/v1/admin/auth/captcha')
def captcha(request:Request,type:str|None=Query(None,max_length=32)):return ok(ctx(request).captcha.issue(type))
@router.post('/api/v1/admin/auth/login')
def login(request:Request,response:Response,body:dict=Body(...)):
    services=ctx(request);ip=client_ip(request)
    try:out=services.auth.login(body,ip,request.headers.get('user-agent','')[:2048])
    except Error as error:
        # 只有密码错误才计入爆破；验证码错误、权限不足和限流都在别处，不计入。
        # 计数写入走独立事务，不会因为登录事务回滚而丢失。
        if error.code=='AUTH_INVALID_CREDENTIALS':services.ipguard.record_failure(ip,(body or {}).get('identifier'))
        raise
    services.ipguard.clear(ip)
    issue_cookies(response,out,services.config);return ok(out)
@router.post('/api/v1/admin/auth/login/totp')
def login_totp(request:Request,response:Response,body:dict=Body(...)):
    out=ctx(request).auth.login_totp(body.get('challengeToken'),body.get('code'),client_ip(request),request.headers.get('user-agent','')[:2048]);issue_cookies(response,out,ctx(request).config);return ok(out)
@router.post('/api/v1/admin/auth/refresh')
def refresh(request:Request,response:Response,body:dict|None=Body(None)):
    out=ctx(request).auth.refresh((body or {}).get('refreshToken') or request.cookies.get('lc_admin_refresh'),client_ip(request),request.headers.get('user-agent','')[:2048]);issue_cookies(response,out,ctx(request).config);return ok(out)
@router.post('/api/v1/admin/auth/logout')
def logout(request:Request,response:Response):ctx(request).auth.logout(principal(request));clear_cookies(response,ctx(request).config);return ok(None)
@router.get('/api/v1/admin/auth/me')
def me(request:Request):return ok(ctx(request).auth.profile(ctx(request).auth.get(principal(request).user_id)))
@router.get('/api/v1/admin/auth/totp')
def totp_status(request:Request):return ok(ctx(request).auth.totp_status(principal(request).user_id))
@router.post('/api/v1/admin/auth/totp/setup')
def totp_setup(request:Request):return ok(ctx(request).auth.setup(principal(request).user_id))
@router.post('/api/v1/admin/auth/totp/enable')
def totp_enable(request:Request,body:dict=Body(...)):return ok(ctx(request).auth.enable(principal(request),body.get('code')))
@router.post('/api/v1/admin/auth/totp/disable')
def totp_disable(request:Request,response:Response,body:dict=Body(...)):
    out=ctx(request).auth.disable(principal(request).user_id,body.get('password'),body.get('code'));clear_cookies(response,ctx(request).config);return ok(out)
@router.post('/api/v1/admin/auth/change-password')
def change_password(request:Request,body:dict=Body(...)):return ok(ctx(request).auth.change_password(principal(request),bounded(body.get('currentPassword'),128,True),body.get('newPassword')))
@router.get('/api/v1/security/turnstile')
def turnstile(request:Request):
    return ok({'enabled':False,'siteKey':'','fallbackSeconds':0,'missing':[]})

@router.get('/api/v1/admin/security/ip-bans')
def list_ip_bans(request:Request,limit:int=Query(100,ge=1,le=500)):
    require_permission(request,'security.ipban')
    guard=ctx(request).ipguard
    return ok({'items':guard.entries(limit),'failureLimit':guard.FAILURE_LIMIT,'banHours':guard.BAN_HOURS,'windowHours':guard.WINDOW_SECONDS//3600})

@router.post('/api/v1/admin/security/ip-bans/unban')
def unban_ip(request:Request,body:dict=Body(...)):
    actor=require_permission(request,'security.ipban')
    # 只接受 ip_hash：它是主键，也不需要把明文 IP 交给客户端再回传。
    key=(body or {}).get('ipHash')
    out=ctx(request).ipguard.unban(key)
    ctx(request).audit.record(actor.user_id,'ADMIN_IP_BAN_UNBAN','admin_ip_ban',str(key or '')[:64],{'removed':out['removed']})
    return ok(out)
