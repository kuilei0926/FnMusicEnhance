#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search.net - stdlib HTTP 助手（仅标准库，替代 requests）。

统一浏览器 UA、超时、响应体上限、gzip/deflate 解压；任何网络/解析失败
返回 None，由调用方静默降级（单平台失败不影响其他平台）。
"""

import gzip
import json
import urllib.parse
import urllib.request
import zlib

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 8
MAX_BODY_BYTES = 8 * 1024 * 1024


def _decode_body(resp):
    """读取响应体并按 Content-Encoding 解压，返回 bytes。"""
    raw = resp.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("response too large")
    encoding = resp.headers.get("Content-Encoding", "")
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


def _parse_json(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def request(method, url, params=None, data=None, json_body=None,
            headers=None, timeout=DEFAULT_TIMEOUT):
    """通用请求。返回响应文本（str），失败返回 None。

    - params: dict，拼接到 query string
    - data: dict/str，form 编码（或原始字符串）
    - json_body: dict，JSON 编码 + Content-Type: application/json
    """
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
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    elif data is not None:
        if isinstance(data, str):
            body = data.encode("utf-8")
        elif isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
        else:
            body = data
        req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    try:
        req = urllib.request.Request(url, data=body, headers=req_headers,
                                     method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _to_text(_decode_body(resp))
    except Exception:
        return None


def get_json(url, params=None, headers=None, timeout=DEFAULT_TIMEOUT):
    """GET 并解析 JSON（dict/list）。失败返回 None。"""
    text = request("GET", url, params=params, headers=headers, timeout=timeout)
    return _parse_json(text) if text is not None else None


def get_text(url, params=None, headers=None, timeout=DEFAULT_TIMEOUT):
    return request("GET", url, params=params, headers=headers, timeout=timeout)


def get_final_url(url, headers=None, timeout=DEFAULT_TIMEOUT, method="GET"):
    """跟随跳转，返回最终 URL（用于短链/分享链接解析）。失败返回原 url。"""
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
    """POST form 编码，返回响应文本。"""
    return request("POST", url, data=data, headers=headers, timeout=timeout)


def post_form_json(url, data=None, headers=None, timeout=DEFAULT_TIMEOUT):
    """POST form 编码，返回解析后的 JSON。失败返回 None。"""
    text = request("POST", url, data=data, headers=headers, timeout=timeout)
    return _parse_json(text) if text is not None else None


def post_json(url, json_body=None, headers=None, timeout=DEFAULT_TIMEOUT):
    """POST JSON body，返回响应文本。"""
    return request("POST", url, json_body=json_body, headers=headers,
                   timeout=timeout)


def post_json_parsed(url, json_body=None, headers=None, timeout=DEFAULT_TIMEOUT):
    """POST JSON body，返回解析后的 JSON。失败返回 None。"""
    text = request("POST", url, json_body=json_body, headers=headers,
                   timeout=timeout)
    return _parse_json(text) if text is not None else None
