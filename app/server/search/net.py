#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small stdlib HTTP helpers with QQ-specific diagnostics.

Normal callers keep the old ``None on failure`` contract. QQ callers use the
``*_detailed`` variants so HTTP, network and parse failures remain visible.
"""

import gzip
import json
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 8
MAX_BODY_BYTES = 8 * 1024 * 1024
_QQ_LOGGER = None
_QQ_RAW_LOGGER = None
_QQ_LOG_LOCK = threading.RLock()
_QQ_LOG_WARNED = False
_QQ_HOSTS = ("qq.com", "gtimg.cn", "tencentmusic.com")


def _quote(value):
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
    if not text or any(ch.isspace() or ch in "=\"" for ch in text):
        return json.dumps(text, ensure_ascii=False)
    return text


def _human_value(value, limit=140):
    import unicodedata
    text = " ".join(str(value or "").split())
    out, width = [], 0
    for ch in text:
        if unicodedata.category(ch) == "Cc":
            continue
        step = 0 if unicodedata.combining(ch) else (
            2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1)
        if width + step > limit:
            return "".join(out[:-1]) + "…"
        out.append(ch)
        width += step
    return "".join(out)


def _qq_detail(label, value):
    import unicodedata
    text = label + _human_value(value)
    lines, current, width = [], [], 0
    for ch in text:
        step = 0 if unicodedata.combining(ch) else (
            2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1)
        if width + step > 62:
            lines.append("  " + "".join(current))
            current, width = [], 0
        current.append(ch)
        width += step
    if current:
        lines.append("  " + "".join(current))
    return "\n".join(lines)


def _qq_timing(milliseconds):
    if milliseconds is None:
        return ""
    value = float(milliseconds)
    return ("%.1f秒" % (value / 1000.0)) if value >= 1000 else ("%.0f毫秒" % value)


def _format_human_event(event, fields):
    """Keep the outcome first and send repetitive HTTP events to raw only."""
    if event == "REQUEST":
        return None
    timing = _qq_timing(fields.get("elapsedMs"))
    if event == "RESPONSE":
        if fields.get("keyword") or fields.get("operation") == "GetQimei":
            return None
        operation = fields.get("operation")
        if operation in ("GetPlayLyricInfo", "LegacyLyric"):
            return "[QQ歌词] 收到响应 · %s" % timing
        if str(fields.get("contentType", "")).startswith("image/"):
            return "[QQ封面] 收到图片 · %s" % timing
        return None
    if event == "SEARCH":
        status = fields.get("status")
        if status == "MATCHED":
            header = "[QQ搜索] 成功 · %s个候选" % fields.get("itemCount", "?")
        elif status in ("NO_RESULT", "NO_MATCH"):
            header = "[QQ搜索] 无结果"
        else:
            header = "[QQ搜索] 源异常"
        if timing:
            header += " · " + timing
        line = header + "\n" + _qq_detail("关键词：", fields.get("keyword", ""))
        if fields.get("errorType"):
            line += "\n" + _qq_detail("错误：", fields["errorType"])
        return line
    if event == "QIMEI":
        source = fields.get("source")
        message = {"tencent": "已获取设备标识", "cache": "使用缓存设备标识", "fallback": "使用备用设备标识"}.get(source, "设备标识已就绪")
        return "[QQ设备] " + message
    if event == "BACKOFF":
        return "[QQ限速] 连续异常%s次，等待%s秒再请求" % (
            fields.get("consecutiveFailures", "?"), fields.get("pauseSeconds", "?"))
    if event in ("HTTP_ERROR", "NETWORK_ERROR", "PARSE_ERROR"):
        kind = {"HTTP_ERROR": "请求失败", "NETWORK_ERROR": "网络异常", "PARSE_ERROR": "响应解析失败"}[event]
        line = "[QQ请求] " + kind
        status = fields.get("status")
        if status is not None:
            line += " · HTTP %s" % status
        if timing:
            line += " · " + timing
        if fields.get("keyword"):
            line += "\n" + _qq_detail("关键词：", fields["keyword"])
        reason = fields.get("reason") or fields.get("errorType")
        if reason:
            line += "\n" + _qq_detail("原因：", reason)
        return line
    if event == "VERIFY":
        return "[日志检查] QQ摘要格式已加载"
    return "[QQ] " + _human_value(fields.get("message") or event)


def _warn_qq_log_once(error):
    global _QQ_LOG_WARNED
    try:
        with _QQ_LOG_LOCK:
            if _QQ_LOG_WARNED:
                return
            _QQ_LOG_WARNED = True
        print("[search.net] WARNING: qq-http.log 写入失败: %s" % error,
              file=__import__("sys").stderr, flush=True)
    except Exception:
        pass


class _QQLogHandler(RotatingFileHandler):
    def handleError(self, record):
        _warn_qq_log_once(__import__("sys").exc_info()[1])


def _get_qq_logger(raw=False):
    global _QQ_LOGGER, _QQ_RAW_LOGGER
    with _QQ_LOG_LOCK:
        logger_attr = "_QQ_RAW_LOGGER" if raw else "_QQ_LOGGER"
        logger = globals()[logger_attr]
        if logger is None:
            logger = logging.getLogger(
                "FnMusicEnhance.qq-http.raw" if raw else "FnMusicEnhance.qq-http")
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
            marker = "_fnmusic_qq_http_raw" if raw else "_fnmusic_qq_http"
            if not any(getattr(h, marker, False) for h in logger.handlers):
                app_log = os.environ.get(
                    "LOG_FILE", "/var/apps/FnMusicEnhance/var/app.log")
                default_name = "qq-http.raw.log" if raw else "qq-http.log"
                env_name = "QQ_HTTP_RAW_LOG_FILE" if raw else "QQ_HTTP_LOG_FILE"
                path = os.environ.get(env_name) or os.path.join(
                    os.path.dirname(app_log), default_name)
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
                    handler = _QQLogHandler(
                        path, maxBytes=10 * 1024 * 1024, backupCount=3,
                        encoding="utf-8", delay=True)
                    handler.setFormatter(logging.Formatter(
                        "%(message)s" if raw else "%(asctime)s %(message)s",
                        datefmt="%H:%M:%S"))
                    setattr(handler, marker, True)
                    logger.addHandler(handler)
                except Exception as error:
                    _warn_qq_log_once(error)
            globals()[logger_attr] = logger
        return logger


_SECRET_KEYS = {"cookie", "authorization", "x-api-key", "qqmusic_key", "musickey",
                "authst", "access_token", "refresh_token"}
_SECRET_PATTERN = re.compile(
    r"(?i)(cookie|authorization|x-api-key|qqmusic_key|musickey|authst|access_token|refresh_token)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'<>}]+)")


def _redact(value):
    if isinstance(value, dict):
        return {key: "<redacted>" if str(key).lower() in _SECRET_KEYS else _redact(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_PATTERN.sub(lambda match: match.group(1) + match.group(2) + "<redacted>", value)
    return value


def log_qq_event(event, level=logging.INFO, **fields):
    """Write a readable summary and a complete JSONL diagnostic entry."""
    try:
        fields = _redact(fields)
        if str(os.environ.get("QQ_DEBUG", "1")).lower() in ("0", "false", "off") \
                and level < logging.WARNING:
            return
        try:
            human = _format_human_event(event, fields)
            if human:
                _get_qq_logger().log(level, human)
        except Exception as error:
            _warn_qq_log_once(error)
        raw = {"event": event, **fields,
               "timestamp": datetime.now().isoformat(timespec="milliseconds")}
        _get_qq_logger(raw=True).log(
            level, json.dumps(raw, ensure_ascii=False, separators=(",", ":"), default=str))
    except Exception as error:
        _warn_qq_log_once(error)


def _is_qq_url(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in _QQ_HOSTS)


def _safe_url(url):
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if parsed.port is not None:
        host += ":%s" % parsed.port
    return "%s://%s%s" % (parsed.scheme, host, parsed.path)


def _decode_body(resp):
    """读取响应体并按 Content-Encoding 解压，返回 bytes。"""
    raw = resp.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("response too large")
    encoding = (resp.headers.get("Content-Encoding", "") or "").lower()
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def _to_text(raw):
    """bytes 按常见编码解码为文本。"""
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _preview(text, limit=800):
    if not text:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ")
    return text[:limit]


def _classify_reason(reason):
    text = str(reason or "").lower()
    return "TIMEOUT" if "timed out" in text or "timeout" in text else "NETWORK_ERROR"


def request_detailed(method, url, params=None, data=None, json_body=None,
                     headers=None, timeout=DEFAULT_TIMEOUT, context=None):
    """Return a diagnostic dict while keeping request bodies out of logs."""
    context = dict(context or {})
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params)

    req_headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if headers:
        req_headers.update(headers)

    body = None
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    elif data is not None:
        if isinstance(data, str):
            body = data.encode("utf-8")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
        else:
            body = data
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    qq = _is_qq_url(url)
    request_fields = {"method": method.upper(), "url": _safe_url(url)}
    request_fields.update(context)
    if body is not None:
        request_fields["requestBytes"] = len(body)
    if qq:
        log_qq_event("REQUEST", **request_fields)
    started = time.monotonic()
    try:
        req = urllib.request.Request(url, data=body, headers=req_headers,
                                     method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = _decode_body(resp)
            text = _to_text(raw)
            elapsed = round((time.monotonic() - started) * 1000, 1)
            meta = {
                "ok": True,
                "text": text,
                "httpStatus": int(getattr(resp, "status", 200) or 200),
                "contentType": resp.headers.get("Content-Type", ""),
                "elapsedMs": elapsed,
                "bytes": len(raw),
            }
            if qq:
                log_qq_event("RESPONSE", status=meta["httpStatus"],
                             elapsedMs=elapsed, contentType=meta["contentType"],
                             bytes=len(raw), **context)
            return meta
    except urllib.error.HTTPError as error:
        elapsed = round((time.monotonic() - started) * 1000, 1)
        raw = b""
        try:
            raw = error.read(MAX_BODY_BYTES + 1)
        except Exception:
            pass
        body_text = _to_text(raw) if raw else ""
        error_type = "RATE_LIMITED" if error.code == 429 else "HTTP_ERROR"
        meta = {
            "ok": False,
            "text": None,
            "errorType": error_type,
            "httpStatus": int(error.code),
            "reason": str(error.reason or ""),
            "elapsedMs": elapsed,
            "bodyPreview": _preview(body_text),
        }
        if qq:
            log_qq_event("HTTP_ERROR", level=logging.WARNING,
                         status=error.code, errorType=error_type,
                         reason=error.reason, elapsedMs=elapsed,
                         body=meta["bodyPreview"], **context)
        return meta
    except (TimeoutError, socket.timeout) as error:
        elapsed = round((time.monotonic() - started) * 1000, 1)
        meta = {"ok": False, "text": None, "errorType": "TIMEOUT",
                "httpStatus": None, "reason": str(error), "elapsedMs": elapsed}
        if qq:
            log_qq_event("NETWORK_ERROR", level=logging.WARNING,
                         errorType="TIMEOUT", reason=error, elapsedMs=elapsed,
                         **context)
        return meta
    except urllib.error.URLError as error:
        elapsed = round((time.monotonic() - started) * 1000, 1)
        error_type = _classify_reason(error.reason)
        meta = {"ok": False, "text": None, "errorType": error_type,
                "httpStatus": None, "reason": str(error.reason), "elapsedMs": elapsed}
        if qq:
            log_qq_event("NETWORK_ERROR", level=logging.WARNING,
                         errorType=error_type, reason=error.reason,
                         elapsedMs=elapsed, **context)
        return meta
    except Exception as error:
        elapsed = round((time.monotonic() - started) * 1000, 1)
        meta = {"ok": False, "text": None, "errorType": "REQUEST_ERROR",
                "httpStatus": None, "reason": "%s: %s" % (type(error).__name__, error),
                "elapsedMs": elapsed}
        if qq:
            log_qq_event("NETWORK_ERROR", level=logging.WARNING,
                         errorType="REQUEST_ERROR", reason=meta["reason"],
                         elapsedMs=elapsed, **context)
        return meta


def _parse_detailed(result, context=None):
    if not result.get("ok"):
        return None, result
    text = result.get("text") or ""
    try:
        return json.loads(text), result
    except (TypeError, ValueError) as error:
        result = dict(result)
        result.update({"ok": False, "errorType": "PARSE_ERROR",
                       "reason": str(error), "bodyPreview": _preview(text)})
        if _is_qq_url((context or {}).get("url", "")):
            log_qq_event("PARSE_ERROR", level=logging.WARNING,
                         status=result.get("httpStatus"),
                         contentType=result.get("contentType"),
                         elapsedMs=result.get("elapsedMs"),
                         reason=error, body=result.get("bodyPreview"),
                         **{k: v for k, v in (context or {}).items() if k != "url"})
        return None, result


def request(method, url, params=None, data=None, json_body=None,
            headers=None, timeout=DEFAULT_TIMEOUT):
    result = request_detailed(method, url, params=params, data=data,
                              json_body=json_body, headers=headers, timeout=timeout)
    return result.get("text") if result.get("ok") else None


def get_json(url, params=None, headers=None, timeout=DEFAULT_TIMEOUT):
    text = request("GET", url, params=params, headers=headers, timeout=timeout)
    return _parse_json(text) if text is not None else None


def get_json_detailed(url, params=None, headers=None, timeout=DEFAULT_TIMEOUT,
                      context=None):
    context = dict(context or {})
    context["url"] = url
    result = request_detailed("GET", url, params=params, headers=headers,
                              timeout=timeout, context={k: v for k, v in context.items() if k != "url"})
    return _parse_detailed(result, context=context)


def get_text(url, params=None, headers=None, timeout=DEFAULT_TIMEOUT):
    return request("GET", url, params=params, headers=headers, timeout=timeout)


def get_final_url(url, headers=None, timeout=DEFAULT_TIMEOUT, method="GET"):
    req_headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if headers:
        req_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=req_headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl()
    except Exception:
        return url


def post_form(url, data=None, headers=None, timeout=DEFAULT_TIMEOUT):
    return request("POST", url, data=data, headers=headers, timeout=timeout)


def post_form_json(url, data=None, headers=None, timeout=DEFAULT_TIMEOUT):
    text = request("POST", url, data=data, headers=headers, timeout=timeout)
    return _parse_json(text) if text is not None else None


def post_json(url, json_body=None, headers=None, timeout=DEFAULT_TIMEOUT):
    return request("POST", url, json_body=json_body, headers=headers,
                   timeout=timeout)


def post_json_parsed(url, json_body=None, headers=None, timeout=DEFAULT_TIMEOUT):
    text = request("POST", url, json_body=json_body, headers=headers,
                   timeout=timeout)
    return _parse_json(text) if text is not None else None


def post_json_parsed_detailed(url, json_body=None, headers=None,
                              timeout=DEFAULT_TIMEOUT, context=None):
    context = dict(context or {})
    context["url"] = url
    result = request_detailed("POST", url, json_body=json_body, headers=headers,
                              timeout=timeout,
                              context={k: v for k, v in context.items() if k != "url"})
    return _parse_detailed(result, context=context)


def _parse_json(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None
