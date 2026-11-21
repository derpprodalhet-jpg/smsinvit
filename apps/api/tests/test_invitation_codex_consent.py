"""Codex 授权确认页（consent）必须被"点确认"，否则登录成功也拿不到 RT。

实测现场（生产日志 ekod7yig9x）：会话过期 → 回落 /log-in → PoW ✓ → TOTP ✓ →
`未捕获 callback code final=.../sign-in-with-chatgpt/codex/consent`。原因就是这个
确认页从来没人处理：个人号没有 workspace_id，`_workspace_select` 也帮不上。

页面是 React Router SSR 表单（实测抓取）：
    <form action="/sign-in-with-chatgpt/codex/consent" method="post">
      <input type="hidden" name="workspace_id" value="da514168-065f-48d7-8e26-d6cd868db2ab"/>
      <button>Continue</button>
    </form>
"""
import os
import tempfile

os.environ.setdefault("SMSINVIT_INVITATION_LOG_DIR", tempfile.mkdtemp(prefix="invitation-logs-"))
os.environ.setdefault("CODEX_HOMES_ROOT", tempfile.mkdtemp(prefix="codex-homes-"))

from types import SimpleNamespace  # noqa: E402

from auth_flow import AuthFlow  # noqa: E402

CONSENT_HTML = (
    '<html><body><form id="_R_35H1_" novalidate="" data-discover="true" '
    'action="/sign-in-with-chatgpt/codex/consent" method="post">'
    '<input id="_R_35H1_-workspace_id" type="radio" name="workspace_id" value="da514168-065f-48d7-8e26-d6cd868db2ab"/>'
    '<input id="_R_35H1_-workspace_id" type="hidden" name="workspace_id" value="da514168-065f-48d7-8e26-d6cd868db2ab"/>'
    '<button data-dd-action-name="Continue">Continue</button></form></body></html>'
)


def make_flow(posts):
    flow = AuthFlow.__new__(AuthFlow)
    flow._ua = "ua"
    flow._trace_http = lambda *a, **k: None
    flow._common_headers = lambda referer=None: {"Referer": referer or ""}

    class Session:
        def post(self, url, headers=None, data=None, timeout=30, allow_redirects=False):
            posts.append({"url": url, "data": data, "headers": headers})
            return SimpleNamespace(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=ABC&state=S"},
                text="",
                json=lambda: {},
            )

    flow.session = Session()
    return flow


def test_consent_form_is_submitted_with_the_workspace_id():
    posts = []
    flow = make_flow(posts)
    next_url = flow._codex_consent_accept(CONSENT_HTML, "https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
    assert posts, "必须真的提交确认表单"
    assert posts[0]["url"] == "https://auth.openai.com/sign-in-with-chatgpt/codex/consent"
    assert posts[0]["data"] == {"workspace_id": "da514168-065f-48d7-8e26-d6cd868db2ab"}
    assert "code=ABC" in next_url


def test_consent_accept_ignores_pages_without_the_form():
    posts = []
    flow = make_flow(posts)
    assert flow._codex_consent_accept("<html>普通页面</html>", "https://auth.openai.com/log-in") == ""
    assert posts == []


def test_consent_accept_uses_the_page_workspace_id_as_a_fallback():
    """表单里只有 radio 没 hidden 时，用页面里的 workspace_id 兜底。"""
    posts = []
    flow = make_flow(posts)
    html = ('<form action="/sign-in-with-chatgpt/codex/consent" method="post">'
            '<button>Continue</button></form>'
            '<script>{"workspace_id":"7f3c1a92-4b6d-4f0e-9a1c-2d5e8b7c0a11"}</script>')
    flow._codex_consent_accept(html, "https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
    assert posts and posts[0]["data"]["workspace_id"] == "7f3c1a92-4b6d-4f0e-9a1c-2d5e8b7c0a11"


def test_consent_accept_survives_a_transport_error():
    flow = AuthFlow.__new__(AuthFlow)
    flow._ua = "ua"
    flow._trace_http = lambda *a, **k: None
    flow._common_headers = lambda referer=None: {}

    class Boom:
        def post(self, *a, **k):
            raise RuntimeError("connect tunnel failed")

    flow.session = Boom()
    assert flow._codex_consent_accept(CONSENT_HTML, "https://auth.openai.com/x") == ""


def test_consent_prefers_the_api_with_the_challenge_from_the_previous_step():
    """底层 API：GET /api/accounts/consent?consent_challenge=…（表单 POST 会被 500）。

    实测：/api/accounts/consent 对 POST 返回 405 "Invalid method"、对缺参数的 GET
    返回 400 "Field required: consent_challenge"，说明这就是真正的确认接口。
    challenge 不在页面 HTML 里，而在**上一步 session/select 的响应**里，所以要先记住它。
    """
    calls = []

    class Session:
        def get(self, url, headers=None, timeout=30, allow_redirects=False):
            calls.append(url)
            return SimpleNamespace(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=XYZ"},
                text="", json=lambda: {})

        def post(self, *a, **k):
            raise AssertionError("找到 challenge 时不该再去 POST 表单")

    flow = AuthFlow.__new__(AuthFlow)
    flow._ua = "ua"
    flow._trace_http = lambda *a, **k: None
    flow._common_headers = lambda referer=None: {}
    flow.session = Session()
    flow._last_auth_step_body = '{"page":{"type":"sign_in_with_chatgpt_codex_consent"},' \
                                '"consent_challenge":"chg_ABC123def456"}'

    next_url = flow._codex_consent_confirm("<html>确认页</html>", "https://auth.openai.com/x")
    assert calls and "consent_challenge=chg_ABC123def456" in calls[0]
    assert "code=XYZ" in next_url


def test_consent_reports_what_is_missing_and_falls_back_to_the_form():
    posts = []

    class Session:
        def post(self, url, headers=None, data=None, timeout=30, allow_redirects=False):
            posts.append(url)
            return SimpleNamespace(status_code=500, headers={}, text="", json=lambda: {})

        def get(self, *a, **k):
            raise AssertionError("没有 challenge 时不该调用 consent API")

    flow = AuthFlow.__new__(AuthFlow)
    flow._ua = "ua"
    flow._trace_http = lambda *a, **k: None
    flow._common_headers = lambda referer=None: {}
    flow.session = Session()
    flow._last_auth_step_body = "{}"          # 上一步响应里也没有

    flow._codex_consent_confirm(CONSENT_HTML, "https://auth.openai.com/sign-in-with-chatgpt/codex/consent")
    assert posts == ["https://auth.openai.com/sign-in-with-chatgpt/codex/consent"]   # 表单兜底
