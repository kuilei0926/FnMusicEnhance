#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QQ音乐搜索/封面/歌词（移植自 musicdl modules/sources/qq.py 与 Lyrico
Lyrico-Plugins/qq/source.js）。

- 搜索/封面：POST https://u.y.qq.com/cgi-bin/musicu.fcg（musicu.fcg，
  module DoSearchForQQMusicMobile）
- 歌词：老接口 c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg
  （base64 原文 + trans 译文；逐字 QRC 需 3DES，暂不启用）
"""

import base64
import json
import random
import re
import zlib

from .. import net
from ..crypto import triple_des_decrypt
from ..lyric_tools import lrc_to_structured

QRC_KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"
_QRC_XML_RE = re.compile(r'<Lyric_1 LyricType="1" LyricContent="([\s\S]*?)"/>')
_QRC_LINE_RE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_QRC_WORD_RE = re.compile(r"(?:^\[\d+,\d+\])?((?:(?!\(\d+,\d+\)).)*)\((\d+),(\d+)\)")
_LRC_LINE_RE2 = re.compile(r"^\[(\d+):(\d+\.\d+)\](.*)$")
_TAG_RE = re.compile(r"^\[(\w+):([^\]]*)\]$")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Referer": "https://y.qq.com/",
    "Origin": "https://y.qq.com/",
}

MUSICU_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"
LYRIC_URL = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
LYRIC_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Referer": "https://y.qq.com/portal/player.html",
}

COMM = {
    "cv": 13020508,
    "v": 13020508,
    "QIMEI36": "6c9d3cd110abca9b16311cee10001e717614",
    "ct": "11",
    "tmeAppID": "qqmusic",
    "format": "json",
    "inCharset": "utf-8",
    "outCharset": "utf-8",
    "uid": "3931641530",
}

# 类型码：0=歌曲 1=歌手 2=专辑（App 层规范）→ QQ search_type
SEARCH_TYPE_MAP = {0: 0, 1: 1, 2: 2}


def _searchid():
    return str(10000000000000000 + random.randint(0, 80000000000000000))


def _build_cover_url(albummid, size=800):
    if not albummid:
        return ""
    return "https://y.gtimg.cn/music/photo_new/T002R%dx%dM000%s.jpg" % (size, size, albummid)


def _musicu_request(keyword, search_type, page, page_size, timeout):
    """调用 musicu.fcg 搜索，返回响应 JSON 或 None。"""
    body = {
        "comm": dict(COMM),
        "music.search.SearchCgiService.DoSearchForQQMusicMobile": {
            "module": "music.search.SearchCgiService",
            "method": "DoSearchForQQMusicMobile",
            "param": {
                "searchid": _searchid(),
                "query": keyword,
                "search_type": search_type,
                "num_per_page": page_size,
                "page_num": page,
                "highlight": 1,
                "grp": 1,
            },
        },
    }
    data = net.post_json_parsed(
        MUSICU_URL, json_body=body, headers=HEADERS, timeout=timeout
    )
    if not isinstance(data, dict):
        return None
    key = "music.search.SearchCgiService.DoSearchForQQMusicMobile"
    module = data.get(key)
    return module if isinstance(module, dict) else None


def _map_song(item):
    singer = item.get("singer") or []
    artists = [
        str(s.get("name")) for s in singer if isinstance(s, dict) and s.get("name")
    ]
    album = item.get("album") or {}
    albummid = album.get("mid") or ""
    duration_s = 0
    try:
        duration_s = int(item.get("interval") or 0)
    except (ValueError, TypeError):
        duration_s = 0
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "artist": "/".join(artists),
        "album": str(album.get("name") or ""),
        "duration": duration_s * 1000,
        "date": str(item.get("time_public") or ""),
        "trackNumber": str(item.get("index_album") or ""),
        "discNumber": "",
        "picUrl": _build_cover_url(albummid),
        "fields": {},
        "internal": {
            "qq_id": str(item.get("id") or ""),
            "songmid": str(item.get("mid") or ""),
            "albummid": albummid,
        },
    }


def _songs_from_body(body):
    items = body.get("item_song")
    if not isinstance(items, list):
        return []
    return [_map_song(i) for i in items if isinstance(i, dict)]


def search_songs(keyword, page=1, page_size=20, timeout=None):
    module = _musicu_request(keyword, 0, page, page_size, timeout)
    if not module:
        return []
    body = (module.get("data") or {}).get("body")
    if not isinstance(body, dict):
        return []
    return _songs_from_body(body)


def search_covers(keyword, search_type=0, page=1, page_size=5, timeout=None):
    """封面搜索（0=歌曲 1=歌手 2=专辑）。返回 list[dict] 带 picUrl。"""
    stype = SEARCH_TYPE_MAP.get(search_type, 0)
    module = _musicu_request(keyword, stype, page, page_size, timeout)
    if not module:
        return []
    body = (module.get("data") or {}).get("body")
    if not isinstance(body, dict):
        return []
    if stype == 1:
        singers = body.get("singer")
        if not isinstance(singers, list):
            return []
        out = []
        for s in singers:
            mid = s.get("singerMID") or ""
            out.append({
                "id": str(s.get("singerID") or ""),
                "title": str(s.get("singerName") or ""),
                "artist": str(s.get("singerName") or ""),
                "album": "",
                "duration": 0,
                "date": "",
                "trackNumber": "",
                "discNumber": "",
                "picUrl": s.get("singerPic") or _build_cover_url(mid),
                "fields": {},
                "internal": {},
            })
        return out
    if stype == 2:
        albums = body.get("item_album")
        if not isinstance(albums, list):
            return []
        out = []
        for al in albums:
            mid = al.get("albummid") or ""
            out.append({
                "id": str(al.get("id") or ""),
                "title": str(al.get("name") or ""),
                "artist": "",
                "album": str(al.get("name") or ""),
                "duration": 0,
                "date": "",
                "trackNumber": "",
                "discNumber": "",
                "picUrl": al.get("pic") or _build_cover_url(mid),
                "fields": {},
                "internal": {},
            })
        return out
    return _songs_from_body(body)


def _parse_qrc(text):
    """QRC 逐字文本 → structured 行数组（词级）。

    格式：`[lineStart,lineDur]文本(wordStart,wordDur)...`，可能包在
    XML `<Lyric_1 LyricContent="..."/>` 内。词绝对开始 = (wordStart)；
    词结束 = 下一个词开始或行结束（Lyrico 对齐逻辑）。
    """
    if not text:
        return []
    content = str(text)
    xml = _QRC_XML_RE.search(content)
    if xml:
        content = (xml.group(1)
                   .replace("&quot;", '"').replace("&apos;", "'")
                   .replace("&lt;", "<").replace("&gt;", ">")
                   .replace("&amp;", "&"))
    lines = []
    for raw_line in content.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if _TAG_RE.match(line):  # [ti:..] 等 tag 行
            continue
        m = _QRC_LINE_RE.match(line)
        if not m:
            continue
        line_start = int(m.group(1))
        line_dur = int(m.group(2))
        line_end = line_start + line_dur
        body = m.group(3)
        words_raw = [
            [int(wm.group(2)), wm.group(1)]
            for wm in _QRC_WORD_RE.finditer(body)
        ]
        words = []
        for i, (ws, wt) in enumerate(words_raw):
            we = words_raw[i + 1][0] if i < len(words_raw) - 1 else line_end
            words.append([ws, we, wt])
        if not words:
            words = [[line_start, line_end, body]]
        lines.append([line_start, line_end, words])
    return lines


def _parse_lrc_lines(text):
    """普通 LRC 文本 → [[startMs, endMs, "text"]] 列表。"""
    temp = []
    if not text:
        return []
    for raw in text.split("\n"):
        line = raw.strip()
        m = _LRC_LINE_RE2.match(line)
        if not m:
            continue
        minutes = int(m.group(1))
        seconds = float(m.group(2))
        start = int(minutes * 60 * 1000 + seconds * 1000)
        temp.append([start, m.group(3)])
    temp.sort(key=lambda x: x[0])
    return [
        [t[0], temp[i + 1][0] if i < len(temp) - 1 else t[0] + 2000, t[1]]
        for i, t in enumerate(temp)
    ]


def _merge_lines(original, trans_lines):
    """把翻译/罗马音行（[start,end,"text"]）按时间窗对齐到 original 行。"""
    if not trans_lines:
        return []
    trans_sorted = sorted(trans_lines, key=lambda x: x[0])
    out = []
    trans_idx = 0
    for i, orig in enumerate(original):
        win_start = orig[0]
        win_end = original[i + 1][0] if i < len(original) - 1 else float("inf")
        matched = ""
        while trans_idx < len(trans_sorted):
            ts = trans_sorted[trans_idx][0]
            if ts < win_start - 500:
                trans_idx += 1
                continue
            if ts >= win_end:
                break
            payload = trans_sorted[trans_idx][2]
            if isinstance(payload, list):
                matched = "".join(w[2] for w in payload if isinstance(w, list) and len(w) > 2)
            else:
                matched = str(payload)
            trans_idx += 1
            break
        out.append([win_start, orig[1], matched])
    return out


def _decode_qrc_payload(value):
    """QQ lyric/trans/roma 字段解密：hex → 3DES → inflate(zlib)；失败退 base64。"""
    raw = str(value or "")
    if not raw:
        return ""
    hex_str = "".join(c for c in raw if c in "0123456789abcdefABCDEF")
    if hex_str:
        try:
            data = bytes.fromhex(hex_str)
            if data and len(data) % 8 == 0:
                dec = triple_des_decrypt(data, QRC_KEY)
                try:
                    return zlib.decompress(dec).decode("utf-8", errors="replace")
                except zlib.error:
                    pass
        except ValueError:
            pass
    try:
        return base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:
        return raw


def _play_lyric_info(song, timeout):
    """GetPlayLyricInfo（qrc:1 逐字 + trans + roma）。失败返回 None。"""
    song_id = str(song.get("songId") or song.get("id") or "")
    if not song_id:
        return None

    def enc(t):
        return base64.b64encode(str(t or "").encode("utf-8")).decode()

    body = {
        "comm": {
            "ct": "11", "cv": "1003006", "v": "1003006", "os_ver": "15",
            "phonetype": "24122RKC7C", "tmeAppID": "qqmusiclight",
            "nettype": "NETWORK_WIFI",
        },
        "req_0": {
            "method": "GetPlayLyricInfo",
            "module": "music.musichallSong.PlayLyricInfo",
            "param": {
                "songID": int(song_id),
                "songName": enc(song.get("title")),
                "albumName": enc(song.get("album")),
                "singerName": enc(song.get("artist")),
                "crypt": 1, "qrc": 1, "trans": 1, "roma": 1,
                "cv": 2111, "ct": 19,
                "lrc_t": 0, "qrc_t": 0, "roma_t": 0, "trans_t": 0,
                "type": 0,
                "interval": max(1, int(song.get("duration") or 0) // 1000),
            },
        },
    }
    data = net.post_json_parsed(MUSICU_URL, json_body=body, headers=HEADERS,
                                timeout=timeout)
    req0 = (data or {}).get("req_0") or {}
    d = req0.get("data")
    if not isinstance(d, dict):
        return None
    qrc = _decode_qrc_payload(d.get("lyric"))
    trans = _decode_qrc_payload(d.get("trans"))
    roma = _decode_qrc_payload(d.get("roma"))
    original = _parse_qrc(qrc)
    if not original:
        # QRC 不可用时 lyric 可能是 base64 普通 LRC
        lrc = _decode_qrc_payload(d.get("lyric"))
        original = _parse_lrc_lines(lrc)
    if not original:
        return None
    return {
        "original": original,
        "translated": _merge_lines(original, _parse_lrc_lines(trans)),
        "romanization": _merge_lines(original, _parse_qrc(roma)),
    }


def get_lyrics(song, timeout=None):
    """获取歌词。song: dict（含 songId/id）。GetPlayLyricInfo（QRC 逐字）优先，老接口降级。"""
    internal = song.get("internal") if isinstance(song.get("internal"), dict) else {}
    songmid = str(song.get("songmid") or internal.get("songmid") or "")

    # 优先：GetPlayLyricInfo（QRC 逐字 + 翻译 + 罗马音）
    try:
        qrc_data = _play_lyric_info(song, timeout)
        if qrc_data:
            return qrc_data
    except Exception:
        pass

    # 降级：老接口 fcg_query_lyric_new（base64 原文 + 译文，行级）
    if songmid:
        data = net.get_json(
            LYRIC_URL,
            params={
                "songmid": songmid,
                "g_tk": 5381,
                "loginUin": 0,
                "hostUin": 0,
                "format": "json",
                "inCharset": "utf8",
                "outCharset": "utf-8",
                "platform": "yqq",
            },
            headers=LYRIC_HEADERS,
            timeout=timeout,
        )
        if isinstance(data, dict):
            def _decode(field):
                raw = data.get(field)
                if not raw:
                    return ""
                try:
                    return base64.b64decode(raw).decode("utf-8", errors="replace")
                except Exception:
                    return ""

            return {
                "original": lrc_to_structured(_decode("lyric")),
                "translated": lrc_to_structured(_decode("trans")),
                "romanization": [],
            }
    return {"original": []}
