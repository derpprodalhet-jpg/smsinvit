"""Wire-compatible envelopes; internal exceptions never expose SQL or credentials."""
from __future__ import annotations
from datetime import datetime, date, timezone
from decimal import Decimal
from uuid import UUID
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

class Error(Exception):
    def __init__(self, code: str | int, message: str, status: int | str = 400, details=None):
        # Also accept the legacy Java ordering (status, code, message).
        if isinstance(code, int):
            code, message, status = message, str(status), code
        self.code, self.message, self.status, self.details = str(code), message, int(status), details
        super().__init__(message)

def ok(data=None, meta=None):
    return {"success": True, "data": data, "meta": meta}

def page_meta(page: int, size: int, total: int):
    return {"page": page, "pageSize": size, "total": total, "totalPages": max(1, (total + size - 1) // size)}

def error_body(error: Error, request_id: str | None = None):
    return {"success": False, "error": {"code": error.code, "message": error.message, "details": error.details, "requestId": request_id}}

def response(data, status=200):
    return JSONResponse(jsonable_encoder(data), status_code=status)

def bad(message="请求参数无效。"):
    return Error("VALIDATION_ERROR", message, 422)
