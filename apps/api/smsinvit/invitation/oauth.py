"""State and callback identity checks before any authorization-code exchange."""
import hmac
from urllib.parse import urlsplit,parse_qs

def valid_callback(callback,redirect,state):
    if not isinstance(callback,str) or len(callback)>8192 or not state:return False
    try:
        actual=urlsplit(callback);expected=urlsplit(redirect)
        identity=lambda u:(u.scheme.lower(),u.hostname,u.port,u.path.rstrip('/'))
        if actual.username or actual.password or actual.fragment or identity(actual)!=identity(expected):return False
        query=parse_qs(actual.query,keep_blank_values=True)
        codes=query.get('code',[]);states=query.get('state',[])
        return len(codes)==len(states)==1 and bool(codes[0].strip()) and hmac.compare_digest(str(state).encode(),states[0].encode())
    except (ValueError,TypeError,AttributeError):return False
