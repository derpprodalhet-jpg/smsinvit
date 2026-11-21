"""
HTTP 客户端 - 使用 curl_cffi 实现 TLS 指纹模拟
支持 Cloudflare 绕过，降级到 requests
"""
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# 尝试使用 curl_cffi（推荐，自带 TLS 指纹模拟）
try:
    from smsinvit.invitation.safety import GuardedCffiSession as CffiSession

    _HAS_CFFI = True
    logger.debug("curl_cffi 可用，使用 TLS 指纹模拟")
except ImportError:
    _HAS_CFFI = False
    logger.debug("curl_cffi 不可用，降级到 requests")

from smsinvit.invitation.safety import guarded_requests as requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 通用 UA（fallback，优先使用 fingerprint.generate_fingerprint() 生成的值）
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"
)

# TLS 握手瞬断的识别标记（与 AuthFlow._is_tls_error 保持同一套口径）
_TLS_ERROR_MARKERS = ("curl: (35)", "tls connect error", "openssl_internal", "sslerror")

_PROXY_AUTH_RE = re.compile(
    r"^(?P<user>[^:@/\s]+):(?P<password>[^@/\s]+)@(?P<host>[^:/\s]+):(?P<port>\d+)$"
)
_PROXY_LEGACY_RE = re.compile(
    r"^(?P<host>[^:@/\s]+):(?P<port>\d+):(?P<user>[^:@/\s]+):(?P<password>[^@/\s]+)$"
)
_PROXY_HOSTPORT_RE = re.compile(r"^(?P<host>[^:/\s]+):(?P<port>\d+)$")


def _is_tls_handshake_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _TLS_ERROR_MARKERS)




def normalize_proxy(proxy: Optional[str]) -> Optional[str]:
    """Normalize proxy strings pasted by users.

    Accepted forms:
    - socks5://user:pass@host:port -> socks5h://user:pass@host:port
    - user:pass@host:port -> http://user:pass@host:port
    - host:port:user:pass -> http://user:pass@host:port
    - host:port -> http://host:port
    """
    raw = str(proxy or "").strip()
    if not raw:
        return None

    scheme = ""
    rest = raw
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
        scheme = scheme.lower()
        if scheme == "socks5":
            scheme = "socks5h"

    authority = None
    if _PROXY_AUTH_RE.fullmatch(rest):
        authority = rest
    else:
        m = _PROXY_LEGACY_RE.fullmatch(rest)
        if m:
            authority = (
                f"{m.group('user')}:{m.group('password')}"
                f"@{m.group('host')}:{m.group('port')}"
            )
        elif _PROXY_HOSTPORT_RE.fullmatch(rest):
            authority = rest

    if authority is None:
        return raw
    if scheme:
        return f"{scheme}://{authority}"
    return f"http://{authority}"

class _TlsRetrySession:
    """给 session 的 get/post 套一层 TLS 瞬断重试，其余属性原样透传。

    ── 为什么要有这东西 ──
    代理链路会偶发 `curl: (35) TLS connect error ... OPENSSL_internal`，
    连 HTTP 请求都没发出去就炸。2026-08-10 实测（148 轮扫描）：

        发生率            5.4%（8/148）
        与指纹的关系      无 —— chrome146/142/136、safari18_0/15_3、firefox133 都中过
        与域名的关系      无 —— chatgpt.com 3/25、auth.openai.com 1/25，
                          且见过同一轮两个域一起炸（那一路出口链路整个坏了）

    换句话说这是**链路级瞬断**，不是风控、不是指纹问题，摘掉任何一个指纹都没用。

    ── 为什么必须原 session 重试，不能重建 ──
    warmup 那处的重试是重建 session（换出口 IP），因为那时还没 cookie。
    但链路中后段（auth_oauth_init / sentinel / authorize_continue …）session 里
    已经装着 warmup 种的 oai-did 和 csrf，**一重建就全丢，直接变 409 invalid_state**
    —— 那正是上一轮刚修好的病。所以这里只重试，绝不碰 session。

    实测原 session 重试的效果（8 次 TLS35 事件全部捕获后立即重试）：

        恢复 8/8，全部**第 1 次重试就成功**，恢复后 oai-did 仍在 8/8

    ── 为什么包在 session 层，而不是逐个调用点加 try ──
    这个错能打在链上**任意一步**。主人 2026-08-10 那批 10 个号的两次失败就分别
    炸在 `[3/10] auth_oauth_init` 和 `[4/10] sentinel`（后者还被 sentinel_quickjs
    的 catch-all 吞成 "QuickJS 失败/主 token 缺失"，真因全被掩盖）。auth_flow 里
    有 35 处 session.get/post，且 sentinel.py 是直接拿 session 对象自己发请求的，
    逐点打补丁既治不完也漏得到 —— 包在出口这一层才是一次覆盖全部。

    ── 透传安全性（已实测）──
    全项目在 session 上访问的非 get/post 属性只有 cookies(20处) / trust_env(3) /
    proxies(3) / mount(2) / headers(1)，实测包装后全部行为一致：
    cookies.get_dict() / cookies.get() / cookies.jar / 迭代 / __setattr__ 透传均 OK。
    （注：迭代 session.cookies 产出的是 str 而非 Cookie 对象、拿不到 .name，
    这是 curl_cffi **原生行为**，包装前后一致，与本类无关。）
    """

    def __init__(self, inner, retries: int = 2, backoff: float = 1.5):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_retries", max(0, int(retries)))
        object.__setattr__(self, "_backoff", float(backoff))

    # 除 get/post 外的一切读写都直达真 session
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_inner"), name, value)

    def __iter__(self):
        return iter(object.__getattribute__(self, "_inner"))

    def _call_with_retry(self, method: str, *args, **kwargs):
        import time

        inner = object.__getattribute__(self, "_inner")
        retries = object.__getattribute__(self, "_retries")
        backoff = object.__getattribute__(self, "_backoff")
        fn = getattr(inner, method)

        for attempt in range(retries + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                # 只兜 TLS 瞬断：HTTP 错误码、超时、业务异常一律原样抛，
                # 免得把"服务端明确拒绝"也变成重试，反而更像异常流量。
                if not _is_tls_handshake_error(e) or attempt >= retries:
                    raise
                wait = backoff * (attempt + 1)
                url = args[0] if args else kwargs.get("url", "?")
                logger.warning(
                    "TLS 瞬断，%.1fs 后原 session 重试 (%d/%d): %s",
                    wait, attempt + 1, retries, str(url)[:80],
                )
                time.sleep(wait)

    def get(self, *args, **kwargs):
        return self._call_with_retry("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._call_with_retry("post", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self._call_with_retry("put", *args, **kwargs)


def create_http_session(
    proxy: Optional[str] = None,
    impersonate: str = "safari18_0",
    user_agent: Optional[str] = None,
):
    """
    创建 HTTP 会话。优先使用 curl_cffi 模拟浏览器 TLS 指纹，
    不可用时降级到 requests。
    """
    if _HAS_CFFI:
        session = CffiSession(impersonate=impersonate)
        # 使用显式配置，避免被系统 HTTP(S)_PROXY 隐式污染。
        session.trust_env = False
        normalized_proxy = normalize_proxy(proxy)
        if normalized_proxy:
            session.proxies = {"https": normalized_proxy, "http": normalized_proxy}
        else:
            session.proxies = {"https": "", "http": ""}

        return _TlsRetrySession(session)
    else:
        session = requests.Session()
        session.trust_env = False
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        normalized_proxy = normalize_proxy(proxy)
        if normalized_proxy:
            session.proxies = {"https": normalized_proxy, "http": normalized_proxy}

        session.headers["User-Agent"] = user_agent or USER_AGENT
        return session
