"""Read-only compatibility for successful phone bindings predating durable details.

Account status remains authoritative. Old logs can only fill missing details when
the same rental, SMS, business success and account binding all agree.
"""
from __future__ import annotations

import logging
import os
import re
import stat
from collections import OrderedDict
from pathlib import Path
from threading import Lock
from time import monotonic

TAIL_BYTES = 128 * 1024
_CACHE_LIMIT = 512
_cache: OrderedDict[tuple, tuple[str, str, str, bool]] = OrderedDict()
_cache_lock = Lock()
_logger = logging.getLogger(__name__)
_last_history_warning = 0.0
_EMPTY = ("", "", "", False)
_LINE = re.compile(r"^\d{2}:\d{2}:\d{2} \[(INFO|WARNING|ERROR)\] ([\w.]+): (.*)$")
_PHONE = r"\+?[0-9]{7,20}"
_RENTAL = re.compile(r"^\[sms\] 已租到新号码，.*?号码=(" + _PHONE + r")(?:，.*)?$")
_SMS = re.compile(r"^\[sms\] 已收到验证码：([0-9]{4,10})（号码=(" + _PHONE + r")(?:，[^）]*)?）$")
_BOUND = re.compile(r"^\[register\] 注册阶段手机绑定成功(?:（协议登录时已完成）)?: ([^\s（]+)(?:（兑换时不再需要 add-phone）)?$")
_INVITE_BOUND = re.compile(r"^\[invite\] ([^\s]+) (?:兑换阶段|手动授权)已绑手机 → 回写接码状态$")


def _parse(text: str) -> tuple[str, str, str, bool]:
    phone = code = bound_email = bound_phone = bound_code = ""
    settled = attempted = False
    for line in text.splitlines():
        match = _LINE.fullmatch(line)
        if not match:
            continue
        level, logger, message = match.groups()
        if logger == "registrar" and message.startswith("[sms]"):
            if message.startswith("[sms] 已租到新号码"):
                attempted = True
                rental = _RENTAL.fullmatch(message) if level == "INFO" else None
                phone = rental.group(1) if rental else ""
                code = bound_email = bound_phone = bound_code = ""
                settled = False
            elif message.startswith("[sms] 已收到验证码"):
                attempted = True
                sms = _SMS.fullmatch(message) if level == "INFO" else None
                code = sms.group(1) if sms and phone == sms.group(2) else ""
                bound_email = bound_phone = bound_code = ""
                settled = False
            elif message.startswith("[sms] 业务验证成功，平台结算"):
                # Supplier settlement can remain pending after business success.
                attempted = True
                settled = level == "INFO" and bool(phone and code)
            elif re.search(r"失败|取消|退款|换新|超时|验证码错误", message):
                phone = code = bound_email = bound_phone = bound_code = ""
                settled = False
        elif logger == "registrar" and message.startswith("[register] 注册阶段手机绑定失败"):
            attempted = True
            phone = code = bound_email = bound_phone = bound_code = ""
            settled = False
        elif logger == "auth_flow" and (
                message.startswith("add-phone") or message.startswith("SMS 第")) and "失败" in message:
            attempted = True
            phone = code = bound_email = bound_phone = bound_code = ""
            settled = False
        elif level == "INFO":
            binding = _BOUND.fullmatch(message) if logger == "registrar" else (
                _INVITE_BOUND.fullmatch(message) if logger == "codex_auth" else None)
            if binding:
                attempted = True
                if settled:
                    bound_email = binding.group(1).strip().lower()
                    bound_phone, bound_code = phone, code
                else:
                    bound_email = bound_phone = bound_code = ""
    return bound_email, bound_phone, bound_code, attempted


def _log_evidence(log_path: object, log_dir: Path) -> tuple[str, str, str, bool]:
    """Open each path component without symlinks, then cache a bounded tail read."""
    if not isinstance(log_path, str) or not log_path or "\0" in log_path:
        return _EMPTY
    descriptors = []
    try:
        root = Path(log_dir).resolve(strict=True)
        path = Path(log_path)
        if not path.is_absolute():
            path = root / path
        relative = path.relative_to(root)
        if not relative.parts or ".." in relative.parts:
            return _EMPTY
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory = os.open(root, directory_flags)
        descriptors.append(directory)
        for part in relative.parts[:-1]:
            directory = os.open(part, directory_flags, dir_fd=directory)
            descriptors.append(directory)
        descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return _EMPTY
        key = (str(root), str(relative), before.st_dev, before.st_ino,
               before.st_mtime_ns, before.st_size)
        with _cache_lock:
            if key in _cache:
                _cache.move_to_end(key)
                return _cache[key]
        offset = max(0, before.st_size - TAIL_BYTES)
        os.lseek(descriptor, offset, os.SEEK_SET)
        data = os.read(descriptor, min(before.st_size, TAIL_BYTES))
        after = os.fstat(descriptor)
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            return _EMPTY
        if offset:
            # A tail starting in a log line cannot supply a complete proof.
            data = data.partition(b"\n")[2]
        evidence = _parse(data.decode("utf-8", "replace"))
        with _cache_lock:
            _cache[key] = evidence
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_LIMIT:
                _cache.popitem(last=False)
        return evidence
    except (OSError, ValueError, RuntimeError):
        return _EMPTY
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def attach_phone_details(result, repo, log_dir: Path):
    """Preserve response shape and fill old successful bindings without DB writes."""
    targets = set()

    def collect(value):
        if isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, dict):
            if str(value.get("phone_bound_ok")) == "1" and (
                    not value.get("phone_number") or not value.get("phone_code")):
                email = str(value.get("email") or "").strip().lower()
                if email:
                    targets.add(email)
            for child in value.values():
                collect(child)

    collect(result)
    if not targets:
        return result
    evidence = {}
    examined = set()
    try:
        runs = repo.list_runs_for_emails(sorted(targets), limit=5)
    except Exception:
        # This optional compatibility lookup must never prevent the admin list
        # from displaying durable status/credentials when its history is absent.
        global _last_history_warning
        now = monotonic()
        with _cache_lock:
            if now - _last_history_warning >= 60:
                _last_history_warning = now
                _logger.warning("Historical phone details lookup is unavailable")
        return result
    for run in runs:
        email = str(run.get("email") or "").strip().lower()
        if email not in targets or email in examined or run.get("kind") not in ("register", "register_pool", "codex_auth"):
            continue
        bound_email, phone, code, attempted = _log_evidence(run.get("log_path"), log_dir)
        if bound_email == email and phone and code:
            evidence[email] = (phone, code)
            examined.add(email)
        elif attempted:
            # A more recent incomplete attempt cannot inherit an older rental.
            examined.add(email)

    def attach(value):
        if isinstance(value, list):
            return [attach(child) for child in value]
        if not isinstance(value, dict):
            return value
        out = {key: attach(child) for key, child in value.items()}
        email = str(value.get("email") or "").strip().lower()
        if str(value.get("phone_bound_ok")) == "1" and email in evidence:
            phone, code = evidence[email]
            if value.get("phone_number") not in (None, "", phone) or value.get("phone_code") not in (None, "", code):
                return out
            out["phone_number"] = value.get("phone_number") or phone
            out["phone_code"] = value.get("phone_code") or code
        return out

    return attach(result)
