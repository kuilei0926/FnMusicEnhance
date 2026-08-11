#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Apple Music 搜索/封面/歌词（移植自 Lyrico Lyrico-Plugins/apple/source.js
与 musicdl modules/sources/apple.py）。

- developer token：从 Apple 首页 JS bundle 里刮取苹果预签的 WebPlay token
  （无需 Apple Developer 账号 / 私钥），解析 exp 缓存
- 搜索：GET amp-api.music.apple.com/v1/catalog/{storefront}/search
- 歌词：官方接口需 media-user-token（无），改用第三方 lyrics.paxsenix.org
  （Apple Music 歌词，无需 token）
"""

import base64
import json
import re
import threading
import time

from .. import net

# 语言/区域映射 → storefront（2 字母）
REGION_TO_STOREFRONT = {
    "cn": "cn", "zh-cn": "cn", "zh-hans": "cn", "zh-hans-cn": "cn",
    "us": "us", "en-us": "us", "en": "us",
    "jp": "jp", "ja-jp": "jp", "ja": "jp",
    "kr": "kr", "ko-kr": "kr", "ko": "kr",
    "tr": "tr", "tr-tr": "tr",
    "hk": "hk", "zh-hk": "hk",
    "tw": "tw", "zh-tw": "tw", "zh-hant": "tw", "zh-hant-tw": "tw",
}
DEFAULT_REGION = "cn"
DEFAULT_STOREFRONT = "cn"

HOME_URL = "https://music.apple.com/"
API_HOST = "https://amp-api.music.apple.com"
THIRD_LYRIC_URL = "https://lyrics.paxsenix.org/apple-music/lyrics"

_TOKEN_RE = re.compile(
    r"(?:^|[^A-Za-z0-9_-])"
    r"([A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,})"
    r"(?![A-Za-z0-9_-])"
)
_INDEX_JS_RES = [
    re.compile(r"/assets/index-legacy[~-][^\"']+\.js"),
    re.compile(r"/assets/index[~-][^\"']+\.js"),
    re.compile(r"/assets/index~[^\"']+\.js"),
]

_token_lock = threading.Lock()
_cached_token = ""
_cached_token_exp = 0


def _b64url_decode(seg):
    padded = seg + "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _parse_jwt(token):
    """解析 JWT 为 (header, payload) dict；失败返回 (None, None)。"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None, None
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        return header, payload
    except Exception:
        return None, None


def _is_webplay_token(token):
    header, payload = _parse_jwt(token)
    if not header or not payload:
        return False
    if header.get("kid") != "WebPlayKid" or header.get("alg") != "ES256":
        return False
    if payload.get("iss") != "AMPWebPlay":
        return False
    exp = payload.get("exp")
    return isinstance(exp, (int, float)) and exp > time.time()


def _scrape_token(language, timeout):
    """从 Apple 首页 JS 刮取 WebPlay developer token。返回 token 或空串。"""
    home = net.get_text(
        HOME_URL, params={"l": language},
        headers={"Accept": "text/html,application/xhtml+xml"},
        timeout=timeout,
    )
    if not home:
        return ""
    index_path = None
    for pattern in _INDEX_JS_RES:
        m = pattern.search(home)
        if m:
            index_path = m.group(0)
            break
    if not index_path:
        return ""
    js = net.get_text(
        HOME_URL + index_path.lstrip("/"),
        params={"l": language},
        headers={"Referer": HOME_URL},
        timeout=timeout,
    )
    if not js:
        return ""
    best = ""
    best_exp = 0
    for m in _TOKEN_RE.finditer(js):
        token = m.group(1)
        header, payload = _parse_jwt(token)
        if not header or not payload:
            continue
        if (header.get("kid") == "WebPlayKid"
                and header.get("alg") == "ES256"
                and payload.get("iss") == "AMPWebPlay"):
            exp = payload.get("exp") or 0
            if exp > best_exp:
                best, best_exp = token, exp
    return best


def _get_token(language, timeout):
    """带缓存的 developer token。"""
    global _cached_token, _cached_token_exp
    now = time.time()
    with _token_lock:
        if _cached_token and _cached_token_exp > now + 60:
            return _cached_token
        token = _scrape_token(language, timeout)
        if token:
            header, payload = _parse_jwt(token)
            _cached_token = token
            _cached_token_exp = payload.get("exp") or 0
        return token


def _storefront(region):
    key = (region or "").lower()
    return REGION_TO_STOREFRONT.get(key, DEFAULT_STOREFRONT)


def _api_headers(token, region):
    lang = "zh-CN" if region in ("cn", "zh-cn") else "en-US"
    return {
        "Authorization": "Bearer %s" % token,
        "Origin": "https://music.apple.com",
        "Referer": "https://music.apple.com",
        "Accept-Language": lang,
    }


def _map_song(song):
    attrs = song.get("attributes") or {}
    artwork = attrs.get("artwork") or {}
    template = artwork.get("url") or ""
    pic = ""
    if template:
        pic = (
            template.replace("{w}", "1200")
            .replace("{h}", "1200")
            .replace("{f}", "jpg")
        )
    return {
        "id": str(song.get("id") or ""),
        "title": str(attrs.get("name") or ""),
        "artist": str(attrs.get("artistName") or ""),
        "album": str(attrs.get("albumName") or ""),
        "duration": int(attrs.get("durationInMillis") or 0),
        "date": str(attrs.get("releaseDate") or ""),
        "trackNumber": str(attrs.get("trackNumber") or ""),
        "discNumber": str(attrs.get("discNumber") or ""),
        "picUrl": pic,
        "fields": {},
        "internal": {"apple_id": str(song.get("id") or "")},
    }


def _search_raw(keyword, page, page_size, timeout, region):
    token = _get_token(region, timeout)
    if not token:
        return None
    storefront = _storefront(region)
    data = net.get_json(
        "%s/v1/catalog/%s/search" % (API_HOST, storefront),
        params={
            "term": keyword,
            "types": "songs",
            "limit": min(page_size, 25),
            "offset": (page - 1) * page_size,
            "l": "zh-CN" if region in ("cn", "zh-cn") else "en-US",
            "platform": "web",
            "format[resources]": "map",
        },
        headers=_api_headers(token, region),
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return None
    results = data.get("results") or {}
    songs = results.get("songs") or {}
    resources = data.get("resources") or {}
    song_map = resources.get("songs") or {}
    items = songs.get("data")
    if not isinstance(items, list):
        return None
    return items, song_map


def search_songs(keyword, page=1, page_size=20, timeout=None, region=DEFAULT_REGION):
    raw = _search_raw(keyword, page, page_size, timeout, region)
    if raw is None:
        return []
    items, song_map = raw
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # format[resources]=map 时 data[] 是引用，用 resources 补全
        full = song_map.get(str(item.get("id"))) or item
        if isinstance(full, dict):
            out.append(_map_song(full))
    return out


def search_covers(keyword, search_type=0, page=1, page_size=5, timeout=None,
                  region=DEFAULT_REGION):
    """封面搜索（0=歌曲；歌手/专辑 Apple 无独立接口，回退歌曲封面）。"""
    if search_type == 0:
        return [s for s in search_songs(keyword, page, page_size, timeout, region)
                if s["picUrl"]]
    # 歌手/专辑：用歌曲搜索结果当封面候选
    return [s for s in search_songs(keyword, page, page_size, timeout, region)
            if s["picUrl"]]


def get_lyrics(song, timeout=None, region=DEFAULT_REGION):
    """获取歌词。song: dict（含 songId/id）。第三方接口，返回 structured 行/词。"""
    song_id = str(song.get("songId") or song.get("id") or "")
    if not song_id:
        return {"original": []}
    data = net.get_json(
        THIRD_LYRIC_URL,
        params={"id": song_id, "ttml": "false"},
        headers={"User-Agent": net.DEFAULT_UA},
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return {"original": []}
    original = []
    content = data.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            start = int(item.get("timestamp") or 0)
            end = int(item.get("endtime") or 0) or start
            text_items = item.get("text")
            words = []
            if isinstance(text_items, list):
                for t in text_items:
                    if not isinstance(t, dict):
                        continue
                    ws = int(t.get("timestamp") or 0)
                    we = int(t.get("endtime") or 0) or ws
                    tx = str(t.get("text") or "").strip()
                    if tx:
                        words.append([ws, we, tx])
            if not words:
                continue
            if len(words) > 1:
                # 词级：保留词时间戳
                original.append([start, end, words])
            else:
                original.append([start, end, words[0][2]])
    return {"original": original}
