"""Retain existing redeemer IMAP mailboxes after the invitation engine migration."""
import email as mail,json,time
from email.utils import parseaddr,parsedate_to_datetime
from smsinvit.invitation.imap import SecureIMAP4_SSL
from .base import MailProvider,MailProviderError,ConfigField,register,extract_otp,validate_email
@register
class LegacyIMAP(MailProvider):
    kind='imap';display_name='通用 IMAP';pooled=True;ephemeral=False;accepts_existing_account=True;line_segments=4
    import_hint='邮箱----密码----IMAP主机----端口';config_fields=[]
    @classmethod
    def from_config(cls,settings,account=None):return cls(account or {})
    @classmethod
    def parse_line(cls,line):
        address,password,host,port=line.split('----');validate_email(address)
        return {'email':address.strip().lower(),'password':password,'client_id':json.dumps({'host':host,'port':int(port)}),'refresh_token':'','kind':'imap'}
    def __init__(self,account):
        self.email=account.get('email','');self.password=account.get('password','');self.connection=json.loads(account.get('client_id') or '{}')
    def create_mailbox(self):return self.email
    def recent_messages(self,limit=25):
        result=[]
        with SecureIMAP4_SSL(self.connection.get('host',''),int(self.connection.get('port',993))) as connection:
            connection.login(self.email,self.password);connection.select('INBOX',readonly=True)
            status,data=connection.search(None,'ALL')
            for item in reversed((data[0].split() if data and data[0] else [])[-min(limit,50):]):
                status,raw=connection.fetch(item,'(BODY.PEEK[])')
                message=mail.message_from_bytes(raw[0][1]);parts=[]
                for part in message.walk():
                    if part.get_content_type() in ('text/plain','text/html'):
                        body=part.get_payload(decode=True) or b''
                        if len(body)>1048576:raise MailProviderError('邮件内容过大',kind='imap')
                        parts.append(body.decode(part.get_content_charset() or 'utf8',errors='replace'))
                try:received=parsedate_to_datetime(message.get('Date','')).timestamp()
                except (ValueError,TypeError):received=None
                result.append({'id':item.decode(),'sender':parseaddr(message.get('From',''))[1],'subject':message.get('Subject',''),'body':'\n'.join(parts),'received':received})
        return result
    def wait_for_otp(self,email_addr,timeout=120,issued_after=None):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            for message in self.recent_messages():
                if issued_after and (not message['received'] or message['received']<issued_after):continue
                code=extract_otp(message['body'])
                if code:return code
            time.sleep(3)
        raise TimeoutError('IMAP 验证码等待超时')
