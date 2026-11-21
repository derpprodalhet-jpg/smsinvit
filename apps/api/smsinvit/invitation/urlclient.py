"""urllib-compatible response/errors through the verified outbound boundary.

The copied mail providers use urllib as a fallback. They must have exactly the
same transport policy as their primary client rather than bypass it on failure.
"""
from io import BytesIO
from urllib.error import HTTPError,URLError
from urllib.request import Request,ProxyHandler
from .safety import GuardedRequestsSession

class Response(BytesIO):
    def __init__(self,response):
        super().__init__(response.content)
        self.status=response.status_code;self.code=self.status;self.headers=response.headers;self.url=response.url;self.reason=response.reason
    def getcode(self):return self.status
    def geturl(self):return self.url
    def getheader(self,name,default=None):return self.headers.get(name,default)
    def info(self):return self.headers

class Opener:
    def __init__(self,proxies=None):self.proxies=proxies or {}
    def open(self,url,data=None,timeout=30,**kwargs):
        if kwargs:raise ValueError('Unmanaged urllib transport options are forbidden')
        request=url if isinstance(url,Request) else Request(str(url),data=data)
        if data is not None:request.data=data
        try:
            with GuardedRequestsSession() as session:
                response=session.request(request.get_method(),request.full_url,data=request.data,headers=dict(request.header_items()),proxies=self.proxies,timeout=timeout)
        except ValueError:raise
        except Exception as exc:raise URLError(type(exc).__name__) from exc
        result=Response(response)
        if result.status>=400:raise HTTPError(request.full_url,result.status,response.reason,result.headers,result)
        return result

def urlopen(url,data=None,timeout=30,**kwargs):return Opener().open(url,data,timeout,**kwargs)
def build_opener(*handlers):
    proxies={}
    for handler in handlers:
        if not isinstance(handler,ProxyHandler):raise ValueError('Only explicit proxy handlers are supported')
        proxies.update(handler.proxies)
    return Opener(proxies)
def getproxies():return {} # Provider settings, never inherited application env.
