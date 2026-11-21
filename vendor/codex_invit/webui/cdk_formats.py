"""卡密导出格式注册表（仿 export_formats.py：加格式只改这一个文件）。

mode:
  - `text`     一行一条，前端弹预览 + 复制 + 下载
  - `download` 整份文档（csv），前端直接落盘
"""
from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class CdkFormat:
    id: str
    label: str
    filename: str
    mode: str = "text"                              # text | download
    mime: str = "text/plain; charset=utf-8"
    render: Optional[Callable[[dict], str]] = None
    render_all: Optional[Callable[[list], bytes]] = None
    note: str = ""


def _s(row: dict, key: str) -> str:
    v = (row or {}).get(key)
    return "" if v is None else str(v).strip()


def _t(ts) -> str:
    try:
        if not ts:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:  # noqa: BLE001
        return ""


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return ("\ufeff" + buf.getvalue()).encode("utf-8")   # BOM: Excel 打开中文不乱码


def _codes_csv(rows: list) -> bytes:
    return _csv_bytes(
        ["卡密", "状态", "邮箱", "批次", "邀请方邮箱", "创建时间", "兑换时间", "完成时间", "失败原因"],
        [[
            _s(r, "code"), _s(r, "status"), _s(r, "email"), _s(r, "batch"),
            _s(r, "inviter_email"), _t(r.get("created_at")), _t(r.get("redeemed_at")),
            _t(r.get("finished_at")), _s(r, "error"),
        ] for r in rows or []],
    )


def _usage_csv(rows: list) -> bytes:
    return _csv_bytes(
        ["卡密", "邮箱", "状态", "尝试次数", "失败次数", "实际代理", "邀请方邮箱",
         "邀请信主题", "邀请信收到时间", "完成时间", "最近错误"],
        [[
            _s(r, "code"), _s(r, "email"), _s(r, "status"), _s(r, "attempts"),
            _s(r, "fail_count"), _s(r, "proxy_used"), _s(r, "inviter_email"),
            _s(r, "inviter_subject"), _t(r.get("invite_received_at")),
            _t(r.get("finished_at")), _s(r, "error"),
        ] for r in rows or []],
    )


FORMATS: list[CdkFormat] = [
    CdkFormat(
        id="codes_txt",
        label="卡密（每行一条）",
        filename="卡密.txt",
        render=lambda r: _s(r, "code"),
        note="一整批复制进独角等发卡平台用",
    ),
    CdkFormat(
        id="codes_full_txt",
        label="卡密----邮箱----状态",
        filename="卡密明细.txt",
        render=lambda r: f'{_s(r, "code")}----{_s(r, "email")}----{_s(r, "status")}',
    ),
    CdkFormat(
        id="codes_csv",
        label="卡密明细 CSV",
        filename="卡密明细.csv",
        mode="download",
        mime="text/csv; charset=utf-8",
        render_all=_codes_csv,
        note="含状态/邮箱/邀请方/时间，Excel 可直接开",
    ),
    CdkFormat(
        id="usage_csv",
        label="使用记录 CSV",
        filename="卡密使用记录.csv",
        mode="download",
        mime="text/csv; charset=utf-8",
        render_all=_usage_csv,
        note="核对用：邀请方邮箱、实际代理、尝试与失败次数",
    ),
]

_BY_ID = {f.id: f for f in FORMATS}


def list_formats() -> list[dict]:
    return [
        {"id": f.id, "label": f.label, "filename": f.filename, "mode": f.mode,
         "mime": f.mime, "note": f.note}
        for f in FORMATS
    ]


def get_format(fmt_id: str) -> Optional[CdkFormat]:
    return _BY_ID.get(str(fmt_id or "").strip())


def render_text(rows: list, fmt) -> str:
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None:
        raise KeyError(f"未知导出格式: {fmt}")
    if not f.render:
        raise RuntimeError(f"格式 {f.id} 不是文本格式")
    out = []
    for r in rows or []:
        try:
            out.append(f.render(r))
        except Exception:  # noqa: BLE001
            out.append("")
    return "\n".join(out)


def render_bytes(rows: list, fmt) -> bytes:
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None:
        raise KeyError(f"未知导出格式: {fmt}")
    if not f.render_all:
        raise RuntimeError(f"格式 {f.id} 不是下载格式")
    return f.render_all(rows or [])
