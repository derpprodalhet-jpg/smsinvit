from contextvars import ContextVar
credentials_context=ContextVar('invitation_response_credentials',default=False)
# 只有"明确授权的授权凭据导出端点"才豁免日志字段脱敏（导出结果就装在 `text`
# 这样的日志字段里）。日志、实时日志流、错误信息与普通页面一律继续脱敏。
credential_export_context=ContextVar('invitation_credential_export',default=False)
