from __future__ import annotations
import json, re, time, threading
from decimal import Decimal, ROUND_CEILING
from dataclasses import dataclass
import httpx
from smsinvit.errors import Error
from .support import text,now,mask

@dataclass(frozen=True)
class NumberOrder:
    activation_id:str
    phone:str
    cost:Decimal|None
@dataclass(frozen=True)
class StatusResult:
    kind:str
    code:str|None=None
    raw:str|None=None

class SmsbowerClient:
    def __init__(self,settings,transport=None,policy=None):
        self.settings=settings; self.policy=policy
        self.http=transport or httpx.Client(timeout=30,follow_redirects=False,trust_env=False)
    def configured(self):return bool(self.settings.secret())
    def call(self,action,params=None):
        key=self.settings.secret()
        if not key:raise Error('SMS_UNAVAILABLE',self.settings.get('unavailableMessage'),503)
        url=self.settings.get('baseUrl')
        if self.policy:self.policy.require_allowed(url)
        try:
            r=self.http.request('GET',url,params={'api_key':key,'action':action,**(params or {})})
            if not 200<=r.status_code<300:raise Error('SMS_PROVIDER_ERROR',f'接码上游响应异常（HTTP {r.status_code}），请稍后查询。',502)
            return r.text
        except Error:raise
        except Exception:raise Error('SMS_PROVIDER_ERROR','接码上游请求失败。',502) from None
    def get_balance(self):return self.call('getBalance')
    def get_prices(self,service,country):
        raw=self.call('getPricesV3',{'service':service,'country':str(country)})
        try:
            data=json.loads(raw)
            if isinstance(data,dict):return data
        except (TypeError,ValueError):pass
        raise Error('SMS_PROVIDER_ERROR','价格表响应无法解析。',502)
    def directory(self,kind):
        raw=self.call('getCountries' if kind=='countries' else 'getServicesList')
        try:
            data=json.loads(raw)
            if isinstance(data,dict):data=data.get(kind) or [v for v in data.values() if isinstance(v,dict)]
            if isinstance(data,list):return [v for v in data if isinstance(v,dict)]
        except (TypeError,ValueError):pass
        raise Error('SMS_PROVIDER_ERROR','目录响应无法解析。',502)
    def get_number(self,service,country,min_price,max_price,provider_ids,exception):
        params={'service':service,'country':str(country),'minPrice':format(Decimal(str(min_price)).normalize(),'f'),'maxPrice':format(Decimal(str(max_price)).normalize(),'f')}
        if provider_ids:params['providerIds']=provider_ids
        if exception:params['phoneException']=exception
        raw=text(self.call('getNumberV2',params))
        if raw.startswith('{'):
            try:
                d=json.loads(raw); aid=text(d.get('activationId'))
                if not aid:raise Error('SMS_PROVIDER_ERROR','取号响应缺少激活编号。',502)
                try:cost=Decimal(str(d['activationCost'])); cost=cost if cost.is_finite() and cost>=0 else None
                except (KeyError,ValueError,ArithmeticError):cost=None
                return NumberOrder(aid,text(d.get('phoneNumber')),cost)
            except Error:raise
            except (ValueError,TypeError):raise Error('SMS_PROVIDER_ERROR','取号响应无法解析。',502)
        if raw.startswith('ACCESS_NUMBER:'):
            p=raw.split(':',2)
            if len(p)==3 and p[1]:return NumberOrder(p[1],p[2],None)
            raise Error('SMS_PROVIDER_ERROR','取号响应格式错误。',502)
        if raw in {'NO_NUMBERS','WRONG_MAX_PRICE','NO_BALANCE','BAD_KEY','BAD_ACTION','BAD_SERVICE','BAD_COUNTRY'}:raise Error('SMS_UNAVAILABLE',self.settings.get('unavailableMessage'),503)
        if raw=='WRONG_EXCEPTION_PHONE':raise Error('SMS_WRONG_EXCEPTION_PHONE','排除号码前缀无效。',502)
        raise Error('SMS_PROVIDER_ERROR','取号结果暂时无法确认，请稍后查询或联系客服。',502)
    def get_status(self,aid):
        raw=text(self.call('getStatus',{'id':aid}))
        if raw.startswith('{'):
            try:
                data=json.loads(raw);code=text(data.get('code') or data.get('smsCode')).strip("'\"");status=text(data.get('status'))
                if code:return StatusResult('OK',code,raw)
                if status.upper() in ('STATUS_CANCEL','CANCEL'):return StatusResult('CANCEL',None,raw)
            except (ValueError,TypeError):pass
        for prefix,kind in [('STATUS_OK:','OK'),('STATUS_WAIT_RETRY:','WAIT_RETRY')]:
            if raw.startswith(prefix):return StatusResult(kind,raw[len(prefix):].strip().strip("'\""),raw)
        if raw=='STATUS_WAIT_CODE':return StatusResult('WAIT',None,raw)
        if raw=='STATUS_CANCEL':return StatusResult('CANCEL',None,raw)
        return StatusResult('ERROR',None,raw)
    def set_status(self,aid,status):return self.call('setStatus',{'id':aid,'status':str(status)})
    def close(self):
        if hasattr(self.http,'close'):self.http.close()

def service_map(prices,service,country):
    by_country=prices.get(str(country))
    if isinstance(by_country,dict) and isinstance(by_country.get(service),dict):return by_country[service]
    direct=prices.get(service)
    if isinstance(direct,dict):
        sample=next(iter(direct.values()),None)
        if isinstance(sample,dict) and ('count' in sample or 'price' in sample):return direct
        if isinstance(direct.get(str(country)),dict):return direct[str(country)]
    for v in prices.values():
        if isinstance(v,dict) and isinstance(v.get(service),dict):return v[service]
    return {}

def cost_usd_to_cny_minor(cost,rate):return int((Decimal(str(cost))*Decimal(str(rate))*100).to_integral_value(rounding=ROUND_CEILING))

class SmsCatalog:
    def __init__(self,client,settings,fx_transport=None):
        self.client=client; self.settings=settings; self.cache={};self.lock=threading.Lock();self.fx_cache=None
        self.fx_http=fx_transport or httpx.Client(timeout=10,trust_env=False,follow_redirects=False)
    def directory(self,kind):
        with self.lock:
            hit=self.cache.get(kind)
            if hit and time.monotonic()-hit[0]<21600:return hit[1]
            rows=self.client.directory(kind);out=[]
            for r in rows:
                if kind=='countries':
                    try:i=int(r.get('id',-1))
                    except (ValueError,TypeError):continue
                    if i>=0:out.append({'id':i,**{k:text(r.get(k)) for k in ('eng','chn','rus')}})
                else:
                    code=text(r.get('code') or r.get('id'))
                    if code:out.append({'code':code,'name':text(r.get('name')) or code})
            out.sort(key=lambda r:r['id' if kind=='countries' else 'code']);self.cache[kind]=(time.monotonic(),out);return out
    def name(self,kind,identifier):
        try:
            for r in self.directory(kind):
                if str(r['id' if kind=='countries' else 'code']).lower()==str(identifier).lower():return (r.get('chn') or r.get('eng') or str(identifier)) if kind=='countries' else r['name']
        except Exception:pass
        return str(identifier)
    def fx(self):
        hit=self.fx_cache
        if hit and time.monotonic()-hit[0]<900:return {'usdCnyRate':hit[1],'rateSource':'cache','rateFetchedAt':hit[2]}
        try:
            r=self.fx_http.get('https://api.frankfurter.dev/v1/latest',params={'base':'USD','symbols':'CNY'})
            rate=float(r.json()['rates']['CNY'])
            if r.status_code==200 and rate>0:
                fetched=now().isoformat()+'Z';self.fx_cache=(time.monotonic(),rate,fetched);return {'usdCnyRate':rate,'rateSource':'live','rateFetchedAt':fetched}
        except Exception:pass
        if hit:return {'usdCnyRate':hit[1],'rateSource':'cache','rateFetchedAt':hit[2]}
        return {'usdCnyRate':self.settings.runtime_float('sms_usd_cny_rate_fallback',7.2),'rateSource':'fallback','rateFetchedAt':now().isoformat()+'Z'}
    def prices_board(self,service,country):
        rows=[]
        for k,v in service_map(self.client.get_prices(service,country),service,country).items():
            if not isinstance(v,dict) or v.get('count') is None or v.get('price') is None:continue
            rows.append({'providerId':str(v.get('provider_id',k)),'count':int(Decimal(str(v['count']))),'priceUsd':Decimal(str(v['price']))})
        rows.sort(key=lambda r:r['priceUsd']);minimum=min((r['priceUsd'] for r in rows if r['count']>0),default=None);fx=self.fx()
        out={'service':service,'serviceName':self.name('services',service),'country':country,'countryName':self.name('countries',country),'providers':rows,'minCostUsd':minimum,'totalCount':sum(max(0,r['count']) for r in rows),**fx}
        if minimum is not None:out['minCostCnyMinor']=cost_usd_to_cny_minor(minimum,fx['usdCnyRate'])
        return out
