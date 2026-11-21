from __future__ import annotations
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter,Request
from .mapper import Mapper
from ..errors import ok,response
from ..security import ctx,require_permission

router=APIRouter()
# V19 上线日：新增的成本列只从这一天开始采集，之前的历史没有可信拆分，
# 统计里如实标 availableSince，而不是把旧值当成本补出来。
COST_AVAILABLE_SINCE='2026-10-11'
class Ops:
    def __init__(self,services):self.s=services;self.mapper=Mapper(services.db,'OpsStatsMapper');self.stats=Mapper(services.db,'StatsMapper');self.health=Mapper(services.db,'HealthMapper')
    def window(self):
        zone=ZoneInfo(self.s.config.get('runtime.business_time_zone','Asia/Shanghai'));now=datetime.now(zone);start=now.replace(hour=0,minute=0,second=0,microsecond=0);return start.astimezone(timezone.utc),(start+timedelta(days=1)).astimezone(timezone.utc),zone
    @staticmethod
    def cost_rows(rows,default_currency=None):
        """把成本 SQL 的行收敛成契约字段；空币种的行绝不并入别的币种。

        币种码统一大写（X 的 proof_json 里存的是小写 `bdt`，直接透出会显示成
        「600.00 bdt」这种不合规范的写法）。
        """
        out=[]
        for row in rows or []:
            currency=str(row.get('currency') or '').strip().upper()
            if not currency:continue
            out.append({'currency':currency,'totalMinor':int(row.get('totalMinor') or 0),'todayMinor':int(row.get('todayMinor') or 0),'unpricedCount':int(row.get('unpricedCount') or 0)})
        if default_currency and not out:out=[{'currency':str(default_currency).upper(),'totalMinor':0,'todayMinor':0,'unpricedCount':0}]
        return out
    def stats_overview(self):
        start,end,zone=self.window();m=self.stats
        def count(name,*args):return int(m.__getattr__(name)(*args) or 0)
        start_epoch,end_epoch=start.timestamp(),end.timestamp()
        modules=[
            {'module':'sms','label':'接码','generatedTotal':count('smsGeneratedTotal'),'generatedToday':count('smsGeneratedToday',start,end),
             'redeemedTotal':count('smsRedeemedTotal'),'redeemedToday':count('smsRedeemedToday',start,end),'expired':count('smsExpired'),'expiredApplicable':True},
            {'module':'invitation','label':'Codex 邀请','generatedTotal':count('cdkGeneratedTotal'),'generatedToday':count('cdkGeneratedToday',start_epoch,end_epoch),
             'redeemedTotal':count('cdkRedeemedTotal'),'redeemedToday':count('cdkRedeemedToday',start_epoch,end_epoch),'expired':0,'expiredApplicable':False}]
        totals={key:sum(row[key] for row in modules) for key in ('generatedTotal','generatedToday','redeemedTotal','redeemedToday','expired')}
        def entries(key,label,rows,basis,detail,since,unpriced=0):
            return [{'key':key,'label':label,'currency':row['currency'],'totalMinor':row['totalMinor'],'todayMinor':row['todayMinor'],
                     'basis':basis,'available':True,'availableSince':since,'unpricedCount':row['unpricedCount']+(unpriced if index==0 else 0),'detailPath':detail}
                    for index,row in enumerate(rows)]
        costs=[]
        costs+=entries('sms','接码（美区 GPT）',self.cost_rows(m.smsCostByCurrency(start,end),'USD'),'成功会话已确认成本','/sms/redeems',None)
        costs+=entries('codex_phone','codex 邮箱接码',self.cost_rows(m.codexPhoneCostByCurrency(start_epoch,end_epoch),'USD'),'成功租号已确认成本','/invitation/runs',COST_AVAILABLE_SINCE)
        pending=[
            {'key':'sms_uncertain','label':'接码待确认','count':count('smsUncertainSessions'),'detailPath':'/sms/redeems'},
            {'key':'codex_settlement_review','label':'结算待复核','count':count('codexSettlementReview'),'detailPath':'/invitation/runs'}]
        return {'businessTimeZone':str(zone),'generatedAt':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),'cards':{'modules':modules,'totals':totals},'costs':costs,'pending':pending}
    def summary(self):
        start,end,zone=self.window();m=self.mapper;stats=self.stats
        statuses=lambda rows:{row['status']:int(row['cnt']) for row in rows}
        # 与 /admin/stats/overview 复用同一批 SQL，字段名与单位保持旧响应不变。
        sms_rows=self.cost_rows(stats.smsCostByCurrency(start,end),'USD')
        sms_usd=next((row for row in sms_rows if row['currency']=='USD'),sms_rows[0])
        out={'businessTimeZone':str(zone),'generatedTotal':m.generatedTotal(),'generatedToday':m.generatedBetween(start,end),'smsKeyCounts':statuses(m.smsKeyCountsByStatus()),'invitationRunCounts':statuses(m.invitationRunCountsByStatus()),'invitationConfirmedToday':m.invitationConfirmedBetween(start,end),'smsUsedToday':stats.smsRedeemedToday(start,end),'smsFailedToday':m.smsFailedBetween(start,end),'smsSupplierCostToday':round(sms_usd['todayMinor']/100,4),'smsStickySessions':stats.smsUncertainSessions(),'jobCounts':statuses(m.jobCountsByStatus())}
        invitation=getattr(self.s,'invitation',None)
        if invitation and hasattr(invitation,'dashboard_summary'):out.update(invitation.dashboard_summary())
        return out
    def dashboard_health(self):
        database=worker=False
        try:self.health.ping();database=True;worker=bool(self.health.workerAlive())
        except Exception:pass
        return {'status':'ok' if database and worker else 'degraded','timestamp':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),'service':'api','checks':{'database':'up' if database else 'down','worker':'up' if worker else 'down'}}

@router.get('/api/v1/admin/dashboard/summary')
def summary(request:Request):require_permission(request,'inventory.read');return ok(ctx(request).ops.summary())
@router.get('/api/v1/admin/stats/overview')
def stats_overview(request:Request):require_permission(request,'inventory.read');return ok(ctx(request).ops.stats_overview())
@router.get('/api/v1/health')
def dashboard_health(request:Request):
    # 这是**公开存活探针**（/api/v1/health），刻意不加鉴权（加过一版会让监控 401，已回退）。
    # 仍需处理的是它的信息量：返回值里的 workerId/调度器细节应收敛为 up/down。
    return ok(ctx(request).ops.dashboard_health())
@router.get('/health')
def health(request:Request):
    try:ctx(request).ops.health.ping();return {'status':'ok','service':'smsinvit-python-api'}
    except Exception:return response({'status':'unavailable'},503)
