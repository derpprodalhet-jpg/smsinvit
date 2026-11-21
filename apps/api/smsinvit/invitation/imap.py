"""Verified and pinned TLS IMAP used only by managed invitation mailboxes."""
import imaplib,ipaddress,os,socket,ssl
class SecureIMAP4_SSL(imaplib.IMAP4_SSL):
    def __init__(self,host='',port=993,ssl_context=None,timeout=30):
        host=host.lower()
        allowed={'outlook.office365.com:993','imap-mail.outlook.com:993'}|{v.strip().lower() for v in os.getenv('SMSINVIT_INVITATION_IMAP_HOSTS','').split(',') if v.strip()}
        if f'{host}:{port}' not in allowed:raise ValueError('IMAP server requires deployment authorization')
        addresses=[r[4][0] for r in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)]
        if not addresses or any(not ipaddress.ip_address(v).is_global for v in addresses):raise ValueError('IMAP server resolves to a private address')
        self._verified_ip=addresses[0];self._verified_host=host
        context=ssl.create_default_context(cafile=os.getenv('SMSINVIT_INVITATION_CA_BUNDLE') or None)
        super().__init__(host,port,ssl_context=context,timeout=timeout)
    def _create_socket(self,timeout):
        raw=socket.create_connection((self._verified_ip,self.port),timeout)
        return self.ssl_context.wrap_socket(raw,server_hostname=self._verified_host)
