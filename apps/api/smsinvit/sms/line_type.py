"""Optional free-quota classifiers with automatic fallback; no paid upgrades."""
from datetime import datetime,timedelta,timezone
from dataclasses import dataclass,field
import threading,time
import httpx

@dataclass
class LineResult:
    provider:str|None=None
    line_type:str|None=None
    skipped:bool=False
    reasons:dict=field(default_factory=dict)

class FreeLineChecker:
    def __init__(self,settings,http=None,monitor=None):
        self.settings=settings;self.http=http or httpx.Client(timeout=4,trust_env=False,follow_redirects=False)
        self.monitor=monitor
        self.owns_http=http is None;self.cooldowns={};self.lock=threading.Lock()
    def close(self):
        if self.owns_http:self.http.close()
    def observe(self,provider,key,**values):
        if self.monitor:
            try:self.monitor.observe(provider,key,values)
            except Exception:pass  # Monitoring must never block paid-order recovery.
    def observed_quota(self,provider,key,response):
        if provider!='numlookupapi':return
        values={}
        for header,field in [('X-RateLimit-Remaining-Quota-Month','remaining'),('X-RateLimit-Limit-Quota-Month','total')]:
            raw=response.headers.get(header)
            if raw is not None and raw.isdigit():values[field]=int(raw)
        if values:
            self.observe(provider,key,**values,state='checking',quotaSource='response_header',quotaUpdatedAt=datetime.now(timezone.utc).isoformat())
    def check(self,phone):
        reasons={};number='+'+str(phone).strip().lstrip('+')
        for provider,key_name in [('numlookupapi','numlookupApiKey'),('abstractapi','abstractApiKey')]:
            try:key=self.settings.secret(key_name)
            except Exception:reasons[provider]='unavailable';continue
            if not key:reasons[provider]='not_configured';continue
            with self.lock:cached=self.cooldowns.get((provider,key))
            if cached and cached[0]>time.monotonic():reasons[provider]=cached[1];continue
            try:
                if provider=='numlookupapi':
                    response=self.http.get('https://api.numlookupapi.com/v1/validate/'+number,headers={'apikey':key},timeout=4,follow_redirects=False)
                else:
                    response=self.http.get('https://phonevalidation.abstractapi.com/v1/',params={'api_key':key,'phone':number.lstrip('+')},timeout=4,follow_redirects=False)
                self.observed_quota(provider,key,response)
                data=response.json()
                if response.status_code!=200:
                    reason='invalid_credentials' if response.status_code in (401,403) else 'rate_limited' if response.status_code==429 else 'service_error'
                    # A 429 can mean either per-minute throttling or a depleted monthly quota.
                    if provider=='numlookupapi' and response.status_code==429:
                        status=self.http.get('https://api.numlookupapi.com/v1/status',headers={'apikey':key},timeout=4,follow_redirects=False)
                        if status.status_code==200:
                            month=status.json().get('quotas',{}).get('month',{})
                            if isinstance(month.get('remaining'),int):
                                self.observe(provider,key,remaining=month['remaining'],total=month.get('total'),quotaSource='status_response',quotaUpdatedAt=datetime.now(timezone.utc).isoformat())
                            if month.get('remaining')==0:reason='quota_exhausted'
                    elif provider=='abstractapi':
                        error=data.get('error',{})
                        message=str(error.get('message','')).lower() if isinstance(error,dict) else ''
                        if any(term in message for term in ('quota','monthly limit','usage limit','credits exhausted')) or ('monthly' in message and 'limit' in message):reason='quota_exhausted'
                    reasons[provider]=reason
                    seconds=3600 if reason=='quota_exhausted' else 60
                    extra={'remaining':0,'quotaSource':'quota_error','quotaUpdatedAt':datetime.now(timezone.utc).isoformat()} if reason=='quota_exhausted' else {}
                    self.observe(provider,key,state=reason,reason=reason,cooldownUntil=(datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat(),**extra)
                    with self.lock:self.cooldowns[(provider,key)]=(time.monotonic()+(3600 if reason=='quota_exhausted' else 60),reason)
                    continue
                returned=data.get('international_format') or data.get('format',{}).get('international') or data.get('phone_format',{}).get('international') or data.get('phone') or data.get('phone_number') or data.get('number')
                if '+'+str(returned).lstrip('+')!=number:
                    reasons[provider]='invalid_response';self.observe(provider,key,state='invalid_response',reason='invalid_response');continue
                valid=data.get('valid',data.get('phone_validation',{}).get('is_valid'))
                if not isinstance(valid,bool):
                    reasons[provider]='invalid_response';self.observe(provider,key,state='invalid_response',reason='invalid_response');continue
                kind=data.get('line_type') or data.get('type') or data.get('phone_carrier',{}).get('line_type')
                kind=str(kind or 'unknown').lower().replace('_','').replace(' ','')
                if data.get('number_type')=='voip' or kind in ('voip','fixedvoip','nonfixedvoip'):kind='voip'
                elif kind in ('mobile','wireless'):kind='mobile'
                if valid is False:kind='invalid'
                self.observe(provider,key,state='available',reason=None,cooldownUntil=None)
                return LineResult(provider,kind,reasons=reasons)
            except Exception:
                reasons[provider]='unavailable'
                self.observe(provider,key,state='unavailable',reason='unavailable',cooldownUntil=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat())
                with self.lock:self.cooldowns[(provider,key)]=(time.monotonic()+60,'unavailable')
        return LineResult(skipped=True,reasons=reasons)
