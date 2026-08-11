#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
汽水音乐（抖音）搜索/封面/歌词（移植自 Lyrico Lyrico-Plugins/soda/source.js
与 musicdl modules/sources/soda.py）。

- 搜索：GET https://api.qishui.com/luna/pc/search/track（Lyrico Web 参数集）
- 歌词：track_v2 接口（逐词内容 → 行级 LRC，含中文译文）
- 封面：album.url_cover（urls[0] + uri + '~c5_xxx.jpg'）
"""

import json
import re

from .. import net

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

SEARCH_URL = "https://api.qishui.com/luna/pc/search/track"
TRACK_URL = "https://api.qishui.com/luna/pc/track_v2"

# Lyrico Web 参数集（匿名可搜，不需要 device_id）
_BASE_PARAMS = {
    "aid": "386088",
    "device_platform": "web",
    "channel": "pc_web",
}

_LINE_RE = re.compile(r"\[(\d+),(\d+)\]")
_WORD_RE = re.compile(r"<(\d+),(\d+),\d+>([^<]*)")
_BODY_TAG_RE = re.compile(r"<[^>]*>")
_UNESCAPE_RE = re.compile(r"\\u003[CE]")  # 汽水返回 </> 反转义


def _unescape(text):
    return _UNESCAPE_RE.sub(
        lambda m: "<" if m.group(0).endswith("C") else ">", text
    )


def _track_from_item(item):
    """从 result_groups[0].data[] 的一项提取 entity.track。"""
    if not isinstance(item, dict):
        return None
    entity = item.get("entity")
    if not isinstance(entity, dict):
        return None
    track = entity.get("track")
    return track if isinstance(track, dict) else None


def _build_cover(cover):
    """album.url_cover / album.cover → 完整图片 URL。"""
    if not isinstance(cover, dict):
        return ""
    urls = cover.get("urls")
    uri = cover.get("uri") or ""
    domain = urls[0] if isinstance(urls, list) and urls else ""
    if not domain or not uri:
        return ""
    if uri in domain:
        return domain
    return domain + uri + "~c5_500x500.jpg"


def _map_track(track):
    artists = [
        str(a.get("name"))
        for a in (track.get("artists") or [])
        if isinstance(a, dict) and a.get("name")
    ]
    album = track.get("album") if isinstance(track.get("album"), dict) else {}
    cover = album.get("url_cover") or album.get("cover")
    date = (
        track.get("publish_time")
        or track.get("publishTime")
        or album.get("release_date")
        or album.get("releaseDate")
        or ""
    )
    return {
        "id": str(track.get("id") or ""),
        "title": str(track.get("name") or ""),
        "artist": "/".join(artists),
        "album": str(album.get("name") or ""),
        "duration": int(track.get("duration") or 0),
        "date": str(date or ""),
        "trackNumber": "",
        "discNumber": "",
        "picUrl": _build_cover(cover),
        "fields": {},
        "internal": {"soda_id": str(track.get("id") or "")},
    }


def _search_raw(keyword, api, page, page_size, timeout):
    params = dict(_BASE_PARAMS)
    params.update({"q": keyword, "cursor": (page - 1) * page_size,
                   "search_method": "input"})
    data = net.get_json(api, params=params, headers=HEADERS, timeout=timeout)
    if not isinstance(data, dict):
        return None
    groups = data.get("result_groups")
    if not isinstance(groups, list) or not groups:
        return None
    group = groups[0]
    return group.get("data") if isinstance(group, dict) else None


def search_songs(keyword, page=1, page_size=20, timeout=None):
    items = _search_raw(keyword, SEARCH_URL, page, page_size, timeout)
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        track = _track_from_item(item)
        if track:
            out.append(_map_track(track))
    return out


def search_covers(keyword, search_type=0, page=1, page_size=5, timeout=None):
    """封面搜索（0=歌曲 1=歌手 2=专辑）。"""
    if search_type == 1:
        items = _search_raw(keyword, "https://api.qishui.com/luna/pc/search/artist",
                            page, page_size, timeout)
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
            artist = entity.get("artist") if isinstance(entity.get("artist"), dict) else {}
            avatar = artist.get("url_avatar")
            pic = ""
            if isinstance(avatar, dict):
                urls = avatar.get("urls")
                uri = avatar.get("uri") or ""
                domain = urls[0] if isinstance(urls, list) and urls else ""
                if domain and uri:
                    pic = domain if uri in domain else domain + uri + "~c5_500x500.jpg"
            name = str(artist.get("name") or "")
            out.append({
                "id": str(artist.get("id") or ""),
                "title": name,
                "artist": name,
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
    if search_type == 2:
        items = _search_raw(keyword, "https://api.qishui.com/luna/pc/search/album",
                            page, page_size, timeout)
        if not isinstance(items, list):
            return []
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            entity = item.get("entity") if isinstance(item.get("entity"), dict) else {}
            album = entity.get("album") if isinstance(entity.get("album"), dict) else {}
            out.append({
                "id": str(album.get("id") or ""),
                "title": str(album.get("name") or ""),
                "artist": "",
                "album": str(album.get("name") or ""),
                "duration": 0,
                "date": "",
                "trackNumber": "",
                "discNumber": "",
                "picUrl": _build_cover(album.get("url_cover") or album.get("cover")),
                "fields": {},
                "internal": {},
            })
        return out
    return [s for s in search_songs(keyword, page, page_size, timeout) if s["picUrl"]]


def _timed_to_structured(content):
    """汽水逐词格式 `[start,dur]<off,dur,flag>text...` → structured 行数组。

    词绝对开始 = lineStart + offset；词结束 = wordStart + duration（dur=0 兜底 +300）。
    无词标签时剥离标签取整行文本。
    """
    if not content:
        return []
    lines = []
    for raw_line in content.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        start = int(m.group(1))
        dur = int(m.group(2))
        end = start + dur if dur else start + 2000
        body = line[m.end():]
        words = _WORD_RE.findall(body)
        if words:
            word_list = []
            for off, wd, text in words:
                ws = start + int(off)
                we = ws + int(wd) if int(wd) else ws + 300
                if text:
                    word_list.append([ws, we, text])
            if not word_list:
                continue
            payload = word_list
        else:
            text = _BODY_TAG_RE.sub("", body).strip()
            if not text:
                continue
            payload = text
        lines.append([start, end, payload])
    return lines


def get_lyrics(song, timeout=None):
    """获取歌词。song: dict（含 songId/id）。track_v2 接口，返回 structured 行/词 + 译文。"""
    song_id = str(song.get("songId") or song.get("id") or "")
    if not song_id:
        return {"original": []}
    data = net.get_json(
        TRACK_URL,
        params={
            "track_id": song_id,
            "media_type": "track",
            **_BASE_PARAMS,
        },
        headers=HEADERS,
        timeout=timeout,
    )
    if not isinstance(data, dict):
        return {"original": []}
    lyric = data.get("lyric")
    if not isinstance(lyric, dict):
        return {"original": []}
    content = _unescape(lyric.get("content") or "")
    translations = lyric.get("translations") or {}
    translated = []
    if isinstance(translations, dict):
        cn = translations.get("cn")
        if isinstance(cn, dict):
            translated = _timed_to_structured(_unescape(cn.get("content") or ""))
    return {
        "original": _timed_to_structured(content),
        "translated": translated,
    }
