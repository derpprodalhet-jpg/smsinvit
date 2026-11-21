"""Persist sanitized observations; displaying status never calls a supplier."""
from datetime import datetime,timezone
from .support import dumps,parse_json,uid,now,instant

PROVIDERS={'numlookupapi':'numlookupApiKey','abstractapi':'abstractApiKey'}
class LineCheckMonitor:
    def __init__(self,db,codec):self.db=db;self.codec=codec
    def observe(self,provider,key,values):
        # Never persist keys, phone numbers, raw errors or provider response bodies.
        allowed={'state','reason','remaining','total','quotaSource','quotaUpdatedAt','checkedAt','cooldownUntil'}
        values={k:v for k,v in values.items() if k in allowed}
        values.update(credentialTag=self.codec.digest(key),checkedAt=instant(now()))
        name='sms.line_monitor.'+provider
        with self.db.transaction(requires_new=True):
            row=self.db.one('SELECT CAST(value_json AS CHAR) AS valueJson FROM settings WHERE `key`=? FOR UPDATE',name)
            previous=parse_json(row['valueJson'],{}) if row else {}
            if previous.get('credentialTag')!=values['credentialTag']:previous={}
            if previous.get('state')=='quota_exhausted' and values.get('state')=='available':
                previous.update(remaining=None,quotaSource='not_reported',quotaUpdatedAt=None)
            payload=previous|values
            self.db.execute("INSERT INTO settings(id,`key`,category,value_json,visibility,is_secret,updated_at) VALUES(?,?, 'sms',?,'PRIVATE',true,NOW(3)) ON DUPLICATE KEY UPDATE value_json=VALUES(value_json),updated_at=NOW(3)",uid(),name,dumps(payload))
    def snapshot(self,settings):
        providers=[]
        for provider,field in PROVIDERS.items():
            key=settings.secret(field)
            row=self.db.one('SELECT CAST(value_json AS CHAR) AS valueJson FROM settings WHERE `key`=?','sms.line_monitor.'+provider)
            value=parse_json(row['valueJson'],{}) if row else {}
            if not key or value.get('credentialTag')!=self.codec.digest(key):value={}
            value.pop('credentialTag',None)
            value.setdefault('state','not_checked' if key else 'not_configured')
            # An expired cooldown is historical evidence, not an assertion of current failure.
            until=value.get('cooldownUntil')
            if until and datetime.fromisoformat(until.replace('Z','+00:00'))<=datetime.now(timezone.utc):value['state']='awaiting_retry'
            providers.append({'provider':provider,'configured':bool(key),'remaining':None,'total':None,**value})
        rows=self.db.all("SELECT event_type AS type,CAST(detail_json AS CHAR) AS detailJson,created_at AS createdAt FROM sms_events WHERE event_type IN ('LINE_TYPE_SKIPPED','LINE_TYPE_VERIFIED','LINE_TYPE_REJECTED') ORDER BY created_at DESC,id DESC LIMIT 20")
        history=[]
        for row in rows:
            detail=parse_json(row['detailJson'],{})
            history.append({'type':row['type'],'provider':detail.get('provider'),'lineType':detail.get('type'),'reasons':detail.get('reasons',{}),'createdAt':instant(row['createdAt'])})
        return {'providers':providers,'history':history,'lastDecision':history[0] if history else None,'snapshotAt':instant(now())}
