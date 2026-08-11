#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网易云音乐搜索/封面/歌词（移植自 musicdl modules/sources/netease.py，
搜索与歌词走明文接口，无需加密）。

- 搜索：POST https://music.163.com/api/cloudsearch/pc
- 封面：同搜索接口，type=1 歌曲 / 10 专辑 / 100 歌手
- 歌词：POST https://interface3.music.163.com/api/song/lyric（明文 form）
"""

import hashlib
import json
import random
import re
import time

from .. import net
from ..crypto import aes_ecb_encrypt
from ..lyric_tools import lrc_to_structured

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}

SEARCH_URL = "https://music.163.com/api/cloudsearch/pc"
LYRIC_URL = "https://interface3.music.163.com/api/song/lyric"
EAPI_LYRIC_URL = "https://interface3.music.163.com/eapi/song/lyric/v1"

EAPI_KEY = b"e82ckenh8dichen8"
_EAPI_PARAM_MAGIC = "36cd479b6b5"
_EAPI_MD5_SALT = "md5forencrypt"

_YRC_LINE_RE = re.compile(r"\[(\d+),(\d+)\]")
_YRC_WORD_RE = re.compile(r"\((\d+),(\d+),\d+\)([^\(]*)")

# 类型码：0=歌曲 1=歌手 2=专辑（App 层规范）→ 网易云 search type
SEARCH_TYPE_MAP = {0: 1, 1: 100, 2: 10}


def _ts_to_date(ts):
    """毫秒时间戳 → YYYY-MM-DD；无效返回空。"""
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts) / 1000))
    except (ValueError, OSError, TypeError):
        return ""


def _safe(data, keys, default=""):
    for key in keys:
        if isinstance(data, dict) and key in data and data[key] is not None:
            return data[key]
    return default


def _map_song(song):
    """把 cloudsearch 的歌曲对象映射为统一结果 dict。"""
    al = song.get("al") or {}
    ar = song.get("ar") or song.get("artists") or []
    pic = al.get("picUrl") or ""
    if pic.startswith("http:"):
        pic = pic.replace("http:", "https:")
    artists = []
    for a in ar:
        name = a.get("name")
        if name:
            artists.append(str(name))
    return {
        "id": str(song.get("id") or ""),
        "title": str(song.get("name") or ""),
        "artist": "/".join(artists),
        "album": str(al.get("name") or ""),
        "duration": int(song.get("dt") or song.get("duration") or 0),
        "date": _ts_to_date(
            song.get("publishTime")
            or song.get("publishTimeMs")
            or (al.get("publishTime") if isinstance(al, dict) else None)
        ),
        "trackNumber": str(song.get("no") or song.get("trackNumber") or ""),
        "discNumber": str(song.get("cd") or ""),
        "picUrl": pic,
        "fields": {},
        "internal": {"netease_id": str(song.get("id") or "")},
    }


def _search_raw(keyword, stype, page, page_size, timeout):
    """调用 cloudsearch 明文接口，返回响应 JSON 或 None。"""
    data = net.post_form_json(
        SEARCH_URL,
        data={
            "s": keyword,
            "type": stype,
            "offset": (page - 1) * page_size,
            "limit": page_size,
        },
        headers=HEADERS,
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return None
    result = data.get("result")
    return result if isinstance(result, dict) else None


def search_songs(keyword, page=1, page_size=20, timeout=None):
    """搜索歌曲。返回 list[dict]（字段对齐 SongMatchResult）。"""
    result = _search_raw(keyword, 1, page, page_size, timeout)
    if not result:
        return []
    songs = result.get("songs")
    if not isinstance(songs, list):
        return []
    return [_map_song(s) for s in songs if isinstance(s, dict)]


def search_covers(keyword, search_type=0, page=1, page_size=5, timeout=None):
    """搜索封面候选（0=歌曲 1=歌手 2=专辑）。返回 list[dict] 带 picUrl。"""
    stype = SEARCH_TYPE_MAP.get(search_type, 1)
    result = _search_raw(keyword, stype, page, page_size, timeout)
    if not result:
        return []
    if stype == 100:
        items = result.get("artists")
        if not isinstance(items, list):
            return []
        out = []
        for a in items:
            pic = _safe(a, ["picUrl"], "")
            if pic.startswith("http:"):
                pic = pic.replace("http:", "https:")
            out.append({
                "id": str(a.get("id") or ""),
                "title": str(a.get("name") or ""),
                "artist": str(a.get("name") or ""),
                "album": "",
                "duration": 0,
                "date": "",
                "trackNumber": "",
                "discNumber": "",
                "picUrl": pic,
                "fields": {},
                "internal": {},
            })
        return out
    if stype == 10:
        items = result.get("albums")
        if not isinstance(items, list):
            return []
        out = []
        for al in items:
            pic = _safe(al, ["picUrl"], "")
            if pic.startswith("http:"):
                pic = pic.replace("http:", "https:")
            artists = [
                str(a.get("name")) for a in (al.get("artists") or [])
                if isinstance(a, dict) and a.get("name")
            ]
            out.append({
                "id": str(al.get("id") or ""),
                "title": str(al.get("name") or ""),
                "artist": "/".join(artists),
                "album": str(al.get("name") or ""),
                "duration": 0,
                "date": _ts_to_date(al.get("publishTime")),
                "trackNumber": "",
                "discNumber": "",
                "picUrl": pic,
                "fields": {},
                "internal": {},
            })
        return out
    # 歌曲封面：复用歌曲搜索，过滤有封面的
    return [s for s in search_songs(keyword, page, page_size, timeout) if s["picUrl"]]


def _eapi_encrypt(api_path, payload):
    """EAPI 加密（Lyrico 01_http.js eapiRequestRaw）。返回 hex 密文。"""
    path = api_path.replace("/eapi/", "/api/")
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.md5(
        ("nobody" + path + "use" + text + _EAPI_MD5_SALT).encode("utf-8")
    ).hexdigest()
    data = (
        path + "-" + _EAPI_PARAM_MAGIC + "-" + text
        + "-" + _EAPI_PARAM_MAGIC + "-" + digest
    ).encode("utf-8")
    return aes_ecb_encrypt(data, EAPI_KEY).hex()


def _eapi_header():
    return {
        "os": "pc",
        "appver": "3.1.3.203419",
        "deviceId": "".join(random.choice("0123456789abcdef") for _ in range(32)),
        "osver": "10.0.0",
        "requestId": str(int(time.time() * 1000)),
        "clientSign": "".join(random.choice("0123456789abcdef") for _ in range(16)),
    }


def _parse_yrc(yrc_text):
    """YRC 逐字格式 `[start,dur](ws,wd,idx)text...` → structured 行/词。

    词开始 = (ws)（绝对毫秒），词结束 = ws + (wd)。
    """
    lines = []
    if not yrc_text:
        return lines
    for raw_line in yrc_text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        m = _YRC_LINE_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        dur = int(m.group(2))
        end = start + dur if dur else start + 2000
        body = line[m.end():]
        words = _YRC_WORD_RE.findall(body)
        if words:
            word_list = []
            for ws, wd, text in words:
                if text:
                    word_list.append([int(ws), int(ws) + int(wd), text])
            if not word_list:
                continue
            payload = word_list
        else:
            text = re.sub(r"\([^)]*\)", "", body).strip()
            if not text:
                continue
            payload = text
        lines.append([start, end, payload])
    return lines


def _eapi_lyric(song_id, timeout):
    """EAPI 歌词接口（lv/tv/rv/yv=-1，能拿 yrc 逐字）。失败返回 None。"""
    payload = {
        "id": song_id,
        "lv": -1, "tv": -1, "rv": -1, "yv": -1,
        "header": _eapi_header(),
    }
    params = _eapi_encrypt("/eapi/song/lyric/v1", payload)
    text = net.post_form(
        EAPI_LYRIC_URL, data={"params": params}, headers=HEADERS, timeout=timeout
    )
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("code") != 200:
        return None
    yrc = data.get("yrc") or {}
    lrc = data.get("lrc") or {}
    tlyric = data.get("tlyric") or {}
    romalrc = data.get("romalrc") or {}
    original = _parse_yrc(yrc.get("lyric") or "")
    if not original:
        original = lrc_to_structured(lrc.get("lyric") or "")
    return {
        "original": original,
        "translated": lrc_to_structured(tlyric.get("lyric") or ""),
        "romanization": lrc_to_structured(romalrc.get("lyric") or ""),
    }


def get_lyrics(song, timeout=None):
    """获取歌词。song: dict（含 id/songId）。EAPI（yrc 逐字）优先，明文接口降级。"""
    song_id = str(song.get("songId") or song.get("id") or "")
    if not song_id:
        return {"original": []}
    # 优先：EAPI 拿 yrc 逐字
    eapi = _eapi_lyric(song_id, timeout)
    if eapi:
        return eapi
    # 降级：明文接口（行级 + 翻译 + 罗马音）
    data = net.post_form_json(
        LYRIC_URL,
        data={
            "id": song_id,
            "cp": "false", "tv": "0", "lv": "0", "rv": "0",
            "kv": "0", "yv": "0", "ytv": "0", "yrv": "0",
        },
        headers=HEADERS,
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return {"original": []}
    lrc = data.get("lrc") or {}
    tlyric = data.get("tlyric") or {}
    romalrc = data.get("romalrc") or {}
    return {
        "original": lrc_to_structured(lrc.get("lyric") or ""),
        "translated": lrc_to_structured(tlyric.get("lyric") or ""),
        "romanization": lrc_to_structured(romalrc.get("lyric") or ""),
    }
