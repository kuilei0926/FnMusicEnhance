#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
酷狗音乐搜索/封面/歌词（移植自 musicdl modules/sources/kugou.py 老接口，
搜索无签名，歌词走 lyrics.kugou.com 两步，fmt=lrc 返回 base64 明文）。

- 搜索：GET https://songsearch.kugou.com/song_search_v2
- 歌词：lyrics.kugou.com/search → download（fmt=lrc，base64 解码）
- 封面搜索：mobilecdn.kugou.com/api/v3/search/{singer,album}（无签名）
"""

import base64
import hashlib
import json
import re
import time
import zlib

from .. import net

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    )
}

SEARCH_URL = "https://songsearch.kugou.com/song_search_v2"
LYRIC_SEARCH_URL = "http://lyrics.kugou.com/search"
LYRIC_DOWNLOAD_URL = "http://lyrics.kugou.com/download"
LYRIC_V1_SEARCH_URL = "https://lyrics.kugou.com/v1/search"
LYRIC_V1_DOWNLOAD_URL = "https://lyrics.kugou.com/download"
SINGER_SEARCH_URL = "https://mobilecdn.kugou.com/api/v3/search/singer"
SINGER_INFO_URL = "https://mobilecdn.kugou.com/api/v3/singer/info"
ALBUM_SEARCH_URL = "https://mobilecdn.kugou.com/api/v3/search/album"

# KRC 解密（Lyrico kugou/lib/02_krc.js）
_KRC_KEY = [64, 71, 97, 119, 94, 50, 116, 71, 81, 54, 49, 45, 206, 210, 110, 105]
_KRC_SALT = "LnT6xpN3khm36zse0QzvmgTZ3waWdRSA"
_DEVICE_MID = hashlib.md5(str(int(time.time() * 1000)).encode()).hexdigest()
_KRC_LINE_RE = re.compile(r"\[(\d+),(\d+)\]")
_KRC_WORD_RE = re.compile(r"<(\d+),(\d+),\d+>([^<]*)")
_KRC_TAG_RE = re.compile(r"<[^>]*>")


def _kg_sign(custom, module="Lyric"):
    """酷狗签名（md5(SALT + 排序拼接 + SALT)），Lyrico kugou 逻辑。"""
    if module == "Lyric":
        base = {"appid": "3116", "clientver": "11070"}
    else:
        base = {
            "userid": "0", "appid": "3116", "token": "", "clienttime": str(int(time.time())),
            "iscorrection": "1", "uuid": "-", "mid": _DEVICE_MID, "dfid": "-",
            "clientver": "11070", "platform": "AndroidFilter",
        }
    params = {**base, **custom}
    s = "".join(k + "=" + str(params[k]) for k in sorted(params))
    params["signature"] = hashlib.md5((_KRC_SALT + s + _KRC_SALT).encode()).hexdigest()
    return params


def _decrypt_krc(content):
    """base64 → 丢前 4 字节 → XOR KRC_KEY → inflate。返回 KRC 文本或空。"""
    try:
        raw = base64.b64decode(content)
    except Exception:
        return ""
    if len(raw) <= 4:
        return ""
    raw = raw[4:]
    xored = bytes(raw[i] ^ _KRC_KEY[i % 16] for i in range(len(raw)))
    try:
        return zlib.decompress(xored).decode("utf-8", errors="replace")
    except zlib.error:
        try:
            return zlib.decompress(xored, -zlib.MAX_WBITS).decode("utf-8", errors="replace")
        except zlib.error:
            return ""


def _parse_krc(krc_text):
    """KRC 文本 → (original 行数组, 翻译行数组, 罗马音行数组)。

    词绝对开始 = lineStart + offset；行结束 = lineStart + duration。
    翻译/罗马音来自 [language:base64] tag（type 0=罗马音，type 1=翻译）。
    """
    original = []
    translated = []
    romanization = []
    for raw_line in krc_text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        # [language:base64] 翻译/罗马音
        if line.startswith("[language:"):
            try:
                b64 = line[len("[language:"):-1]
                lang = json.loads(base64.b64decode(b64).decode("utf-8", errors="replace"))
                for item in (lang.get("content") or []):
                    if not isinstance(item, dict):
                        continue
                    content = item.get("lyricContent") or []
                    rows = []
                    for row in content:
                        if isinstance(row, list) and row:
                            rows.append([0, 0, str(row[0])])
                        elif isinstance(row, str):
                            rows.append([0, 0, row])
                    if item.get("type") == 0:
                        romanization = rows
                    elif item.get("type") == 1:
                        translated = rows
            except Exception:
                pass
            continue
        # 元数据 tag [ti:..] [ar:..] 跳过
        if line.startswith("[") and line.endswith("]") and "]" in line[1:]:
            tag = line[1:line.index("]")]
            if ":" in tag and not tag[0].isdigit():
                continue
        m = _KRC_LINE_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        dur = int(m.group(2))
        end = start + dur if dur else start + 2000
        body = line[m.end():]
        words = _KRC_WORD_RE.findall(body)
        if words:
            word_list = []
            for off, wd, text in words:
                ws = start + int(off)
                we = ws + int(wd) if int(wd) else end
                if text:
                    word_list.append([ws, we, text])
            if not word_list:
                continue
            payload = word_list
        else:
            text = _KRC_TAG_RE.sub("", body).strip()
            if not text:
                continue
            payload = text
        original.append([start, end, payload])
    return original, translated, romanization


def _first(data, keys, default=""):
    for key in keys:
        if isinstance(data, dict) and data.get(key) not in (None, ""):
            return data[key]
    return default


def _normalize_image(url):
    if not url:
        return ""
    url = url.replace("{size}", "480")
    if url.startswith("http:"):
        url = url.replace("http:", "https:")
    return url


def _map_song(item):
    """把 song_search_v2 的列表项映射为统一结果 dict。"""
    trans = item.get("trans_param") if isinstance(item.get("trans_param"), dict) else {}
    cover = (
        trans.get("union_cover")
        or item.get("cover_url")
        or item.get("Image")
        or item.get("imgurl")
        or ""
    )
    singers = item.get("singerinfo") if isinstance(item.get("singerinfo"), list) else []
    artist = _first(item, ["singername", "SingerName"], "") or "/".join(
        str(s.get("name")) for s in singers if isinstance(s, dict) and s.get("name")
    )
    title = _first(item, ["songname", "SongName", "name", "filename"], "")
    file_hash = str(_first(item, ["FileHash", "hash"], ""))
    duration_ms = 0
    try:
        duration_ms = int(_first(item, ["timelen", "DurationMs"], 0) or 0)
    except (ValueError, TypeError):
        duration_ms = 0
    if not duration_ms:
        try:
            duration_ms = int(float(_first(item, ["duration", "Duration"], 0) or 0)) * 1000
        except (ValueError, TypeError):
            duration_ms = 0
    return {
        "id": str(_first(item, ["ID", "id", "AudioId"], "")),
        "title": title,
        "artist": artist,
        "album": _first(item, ["AlbumName", "album_name", "albumname"], ""),
        "duration": duration_ms,
        "date": _first(item, ["PublishDate", "publish_date", "release_date"], ""),
        "trackNumber": str(_first(item, ["album_audio_id", "AudioId"], "")),
        "discNumber": "",
        "picUrl": _normalize_image(cover),
        "fields": {},
        "internal": {"hash": file_hash, "filename": _first(item, ["filename", "FileName"], "")},
    }


def search_songs(keyword, page=1, page_size=20, timeout=None):
    data = net.get_json(
        SEARCH_URL,
        params={
            "format": "json",
            "keyword": keyword,
            "platform": "WebFilter",
            "page": page,
            "pagesize": page_size,
        },
        headers=HEADERS,
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return []
    lists = (data.get("data") or {}).get("lists")
    if not isinstance(lists, list):
        return []
    return [_map_song(i) for i in lists if isinstance(i, dict)]


def search_covers(keyword, search_type=0, page=1, page_size=5, timeout=None):
    """封面搜索（0=歌曲 1=歌手 2=专辑）。返回 list[dict] 带 picUrl。"""
    if search_type == 1:
        # 歌手：搜索歌手 → 逐个取头像
        data = net.get_json(
            SINGER_SEARCH_URL,
            params={"keyword": keyword, "page": page, "pagesize": page_size},
            headers=HEADERS,
            timeout=timeout,
        )
        items = (data or {}).get("data")
        if not isinstance(items, list):
            return []
        out = []
        for singer in items:
            sid = str(singer.get("singerid") or "")
            name = str(singer.get("singername") or "")
            info = net.get_json(
                SINGER_INFO_URL,
                params={"singerid": sid},
                headers=HEADERS,
                timeout=timeout,
            )
            pic = ""
            if isinstance(info, dict):
                sdata = info.get("data")
                if isinstance(sdata, dict):
                    pic = sdata.get("imgurl") or sdata.get("img") or ""
            out.append({
                "id": sid,
                "title": name,
                "artist": name,
                "album": "",
                "duration": 0,
                "date": "",
                "trackNumber": "",
                "discNumber": "",
                "picUrl": _normalize_image(pic),
                "fields": {},
                "internal": {},
            })
        return out
    if search_type == 2:
        data = net.get_json(
            ALBUM_SEARCH_URL,
            params={"keyword": keyword, "page": page, "pagesize": page_size},
            headers=HEADERS,
            timeout=timeout,
        )
        info = (data or {}).get("data") or {}
        items = info.get("info")
        if not isinstance(items, list):
            return []
        out = []
        for al in items:
            out.append({
                "id": str(al.get("albumid") or ""),
                "title": str(al.get("albumname") or ""),
                "artist": str(al.get("singername") or ""),
                "album": str(al.get("albumname") or ""),
                "duration": 0,
                "date": str(al.get("publishtime") or ""),
                "trackNumber": "",
                "discNumber": "",
                "picUrl": _normalize_image(al.get("imgurl") or ""),
                "fields": {},
                "internal": {},
            })
        return out
    return [s for s in search_songs(keyword, page, page_size, timeout) if s["picUrl"]]


def _lrc_to_structured(lrc_text):
    """行级 LRC 文本 → structured 行数组（每行一个整行文本）。"""
    lines = []
    if not lrc_text:
        return lines
    re_lrc = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\](.*)")
    for raw in lrc_text.split("\n"):
        m = re_lrc.match(raw.strip())
        if not m:
            continue
        minutes = int(m.group(1))
        seconds = int(m.group(2))
        start = (minutes * 60 + seconds) * 1000
        frac = m.group(3)
        if frac:
            scale = 100 if len(frac) == 1 else 10 if len(frac) == 2 else 1
            start += int(frac) * scale
        text = m.group(4).strip()
        if not text:
            continue
        lines.append([start, start, text])
    return lines


def get_lyrics(song, timeout=None):
    """获取歌词。song: dict（含 internal.hash / id）。KRC 签名接口优先，老接口降级。"""
    internal = song.get("internal") if isinstance(song.get("internal"), dict) else {}
    file_hash = str(song.get("hash") or internal.get("hash") or "")
    if not file_hash:
        return {"original": []}

    # 优先：KRC 签名接口（逐字 + 翻译/罗马音）
    try:
        search = net.get_json(
            LYRIC_V1_SEARCH_URL,
            params=_kg_sign({
                "album_audio_id": str(song.get("id") or ""),
                "duration": int(song.get("duration") or 0),
                "hash": file_hash,
                "keyword": "%s - %s" % (song.get("artist") or "", song.get("title") or ""),
                "lrctxt": 1,
                "man": "no",
            }),
            headers=HEADERS,
            timeout=timeout,
        )
        candidates = (search or {}).get("candidates")
        if isinstance(candidates, list) and candidates:
            cand = candidates[0]
            if isinstance(cand, dict) and cand.get("id") and cand.get("accesskey"):
                dl = net.get_json(
                    LYRIC_V1_DOWNLOAD_URL,
                    params=_kg_sign({
                        "accesskey": cand["accesskey"], "charset": "utf8",
                        "client": "mobi", "fmt": "krc", "id": cand["id"], "ver": 1,
                    }),
                    headers=HEADERS,
                    timeout=timeout,
                )
                content = (dl or {}).get("content")
                if content:
                    krc = _decrypt_krc(content)
                    if krc:
                        original, translated, romanization = _parse_krc(krc)
                        if original:
                            return {
                                "original": original,
                                "translated": translated,
                                "romanization": romanization,
                            }
    except Exception:
        pass

    # 降级：老接口 lyrics.kugou.com（fmt=lrc，base64 明文）
    try:
        filename = str(internal.get("filename") or song.get("title") or "")
        search = net.get_json(
            LYRIC_SEARCH_URL,
            params={"keyword": filename, "duration": "-1", "hash": file_hash},
            headers=HEADERS,
            timeout=timeout,
        )
        candidates = (search or {}).get("candidates")
        if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
            cand = candidates[0]
            if cand.get("id") and cand.get("accesskey"):
                dl = net.get_json(
                    LYRIC_DOWNLOAD_URL,
                    params={
                        "ver": "1", "client": "pc", "id": cand["id"],
                        "accesskey": cand["accesskey"], "fmt": "lrc", "charset": "utf8",
                    },
                    headers=HEADERS,
                    timeout=timeout,
                )
                content = (dl or {}).get("content")
                if content:
                    raw = base64.b64decode(content).decode("utf-8", errors="replace")
                    return {"original": _lrc_to_structured(raw)}
    except Exception:
        pass
    return {"original": []}
