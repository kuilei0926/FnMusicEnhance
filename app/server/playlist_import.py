#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
playlist_import - 歌单导入（网易云 / QQ / 酷狗 / 酷我）。

解析歌单链接 → 歌单名 / 封面 / 歌曲列表；匹配本地音乐库（歌名+歌手精确）的
直接加入歌单，本地不存在的插入占位歌曲（is_audio_file_deleted=1 标记失效）。
"""

import hashlib
import json
import re
import time
import uuid

from search import net
from match import (
    _db_connect,
    _next_id,
    _resolve_artists,
    _resolve_album,
    _write_cover_file,
    _download_image,
    _track_artist_names,
)

NETEASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}
QQ_METING_URL = "https://api.injahow.cn/meting/"
# 酷狗歌单页需要手机 UA 才渲染 window.$output / specialInfo
KUGOU_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 "
    "Mobile/15E148 Safari/604.1"
)
# gatewayretry 接口的 md5 签名盐（musicdl 同款）
KUGOU_SALT = "OIlwieks28dk2k092lksi2UIkp"
KUGOU_IMPORT_API = "http://gatewayretry.kugou.com/v2/get_other_list_file"
KUGOU_IMPORT_HEADERS = {
    "User-Agent": "Android9-AndroidPhone-11239-18-0-playlist-wifi",
    "Host": "gatewayretry.kugou.com",
    "x-router": "pubsongscdn.kugou.com",
    "mid": "239526275778893399526700786998289824956",
    "dfid": "-",
}


def parse_playlist_url(url):
    """识别平台 + 歌单 id。返回 (platform, playlist_id)。"""
    if "music.163.com" in url or "163.com" in url:
        return "netease", _netease_playlist_id(url)
    if "y.qq.com" in url or "qq.com" in url:
        return "qq", _qq_playlist_id(url)
    if "kugou.com" in url:
        return "kugou", _kugou_playlist_id(url)
    if "kuwo.cn" in url:
        return "kuwo", _kuwo_playlist_id(url)
    return None, None


def _netease_playlist_id(url):
    m = re.search(r"[?#].*?id=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/playlist/(\d+)", url)
    if m:
        return m.group(1)
    return None


def _qq_playlist_id(url):
    """QQ 歌单 id：路径 /playlist/<id>，或手机分享链接 query 里的 id=。"""
    m = re.search(r"/playlist/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?#].*?id=(\d+)", url)
    if m:
        return m.group(1)
    return None


def _kugou_playlist_id(url):
    """酷狗歌单 id：新版 gcid_xxx（任意位置）或旧版 special/single/<数字>.html。"""
    m = re.search(r"(gcid_[a-zA-Z0-9]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"special/single/(\d+)", url)
    if m:
        return m.group(1)
    return None


def _kuwo_playlist_id(url):
    """酷我歌单 id：playlist_detail/<pid> 或 query 里的 pid。"""
    m = re.search(r"playlist_detail/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?#].*?pid=(\d+)", url)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# 歌单解析（网易云官方接口 / QQ Meting / 酷狗 gatewayretry）
# ---------------------------------------------------------------------------

def _parse_netease(playlist_id):
    """网易云歌单：playlist/detail 拿名/封面/trackIds，song/detail 批量拿歌曲。"""
    d = net.post_form_json(
        "https://music.163.com/api/v6/playlist/detail",
        data={"id": playlist_id},
        headers=NETEASE_HEADERS,
        timeout=15,
    )
    if not isinstance(d, dict) or not d.get("playlist"):
        raise Exception("网易云歌单解析失败")
    p = d["playlist"]
    track_ids = [t.get("id") for t in (p.get("trackIds") or []) if t.get("id")]
    songs = []
    for i in range(0, len(track_ids), 50):
        chunk = [{"id": tid} for tid in track_ids[i:i + 50]]
        detail = net.post_form_json(
            "https://music.163.com/api/v3/song/detail",
            data={"c": json.dumps(chunk)},
            headers=NETEASE_HEADERS,
            timeout=15,
        )
        for s in (detail or {}).get("songs") or []:
            songs.append({
                "title": s.get("name") or "",
                "artist": "/".join(
                    a.get("name") for a in (s.get("ar") or []) if a.get("name")
                ),
                "album": (s.get("al") or {}).get("name") or "",
            })
    return {
        "name": p.get("name") or "网易云歌单",
        "cover_url": p.get("coverImgUrl"),
        "songs": songs,
    }


def _parse_qq(playlist_id):
    """QQ 歌单：官方接口需登录 cookie，用 Meting 解析歌曲列表。

    Meting 公共接口偶发超时/空响应，重试 3 次再判失败。
    """
    parsed = []
    for attempt in range(3):
        songs = net.get_json(
            QQ_METING_URL,
            params={"server": "tencent", "type": "playlist", "id": playlist_id},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        parsed = []
        for s in songs or []:
            if isinstance(s, dict) and s.get("name"):
                parsed.append({
                    "title": s.get("name"),
                    "artist": s.get("artist") or "",
                    "album": "",
                })
        if parsed:
            break
        if attempt < 2:
            time.sleep(1)
    if not parsed:
        raise Exception("QQ歌单解析失败")
    return {
        "name": "QQ歌单 %s" % playlist_id,
        "cover_url": None,
        "songs": parsed,
    }


# ---------------------------------------------------------------------------
# 酷狗解析（新版 songlist/gcid 页 + 旧版 special/single + gatewayretry 签名接口）
# ---------------------------------------------------------------------------

def _kugou_signature(api_url):
    """gatewayretry 接口签名：md5(盐 + 排序后的 query + 盐)。"""
    params = "".join(sorted(api_url.split("?", 1)[1].split("&")))
    return hashlib.md5(
        (KUGOU_SALT + params + KUGOU_SALT).encode("utf-8")
    ).hexdigest()


def _extract_output_json(html):
    """提取页面里 window.$output = {...} 的完整 JSON（括号匹配）。"""
    m = re.search(r"window\.\$output\s*=\s*", html)
    if not m:
        return None
    brace = html.find("{", m.start())
    if brace < 0:
        return None
    depth = 0
    i = brace
    in_str = False
    esc = False
    while i < len(html):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[brace:i + 1])
                    except ValueError:
                        return None
        i += 1
    return None


def _kugou_track_list(specialid):
    """gatewayretry 签名接口，分页拉取歌单全部歌曲。按 hash 去重。"""
    tracks = []
    page = 1
    while True:
        headers = dict(KUGOU_IMPORT_HEADERS)
        headers["clienttime"] = str(int(time.time()))
        api_url = (
            "%s?specialid=%s&need_sort=1&module=CloudMusic"
            "&clientver=11239&pagesize=300&specalidpgc=%s&userid=0"
            "&page=%s&type=0&area_code=1&appid=1005"
            % (KUGOU_IMPORT_API, specialid, specialid, page)
        )
        d = net.get_json(
            api_url + "&signature=" + _kugou_signature(api_url),
            headers=headers,
            timeout=15,
        )
        if not isinstance(d, dict):
            break
        info = (d.get("data") or {}).get("info") or []
        if not info:
            break
        tracks.extend(info)
        count = (d.get("data") or {}).get("count") or 0
        if count <= len(tracks):
            break
        page += 1
    # 按 hash 去重（接口偶发重复）
    seen = set()
    unique = []
    for t in tracks:
        h = t.get("hash")
        if h in seen:
            continue
        if h:
            seen.add(h)
        unique.append(t)
    return unique


def _kugou_parse_song(t):
    """歌曲 dict -> {title, artist, album}。

    name 形如「歌手 - 歌名」；singerinfo 是字符串化的歌手列表；albuminfo 是
    字符串化的专辑 dict（旧接口是 dict）。各字段缺失时降级。
    """
    name = t.get("name") or ""
    title, artist = name, ""
    if " - " in name:
        artist, title = name.split(" - ", 1)
    si = t.get("singerinfo")
    if isinstance(si, str) and si.strip().startswith("["):
        try:
            names = [
                s.get("name")
                for s in json.loads(si.replace("'", '"'))
                if s.get("name")
            ]
            if names:
                artist = "/".join(names)
        except ValueError:
            pass
    album = ""
    ai = t.get("albuminfo")
    if isinstance(ai, dict):
        album = ai.get("name") or ""
    elif isinstance(ai, str) and ai.strip().startswith("{"):
        try:
            album = (json.loads(ai.replace("'", '"')) or {}).get("name") or ""
        except ValueError:
            pass
    return {"title": title, "artist": artist, "album": album}


def _parse_kugou(playlist_id):
    """酷狗歌单解析。playlist_id 为 gcid_xxx（新版）或数字（旧版）。"""
    if playlist_id.startswith("gcid_"):
        return _parse_kugou_gcid(playlist_id)
    return _parse_kugou_special(playlist_id)


def _parse_kugou_gcid(gcid):
    """新版：抓 songlist 页拿 specialid / 歌单名 / 封面。"""
    html = net.get_text(
        "https://www.kugou.com/songlist/%s/" % gcid,
        headers={"User-Agent": KUGOU_MOBILE_UA},
        timeout=15,
    )
    if not html:
        raise Exception("酷狗歌单页抓取失败")
    out = _extract_output_json(html)
    if not out:
        raise Exception("酷狗歌单解析失败")
    li = (out.get("info") or {}).get("listinfo") or {}
    specialid = li.get("specialid")
    if not specialid:
        raise Exception("酷狗歌单解析失败：无 specialid")
    name = li.get("name") or "酷狗歌单 %s" % specialid
    pic = (li.get("pic") or "").replace("{size}", "400") or None
    tracks = _kugou_track_list(str(specialid))
    return {
        "name": name,
        "cover_url": pic,
        "songs": [_kugou_parse_song(t) for t in tracks],
    }


def _parse_kugou_special(specialid):
    """旧版：specialid 直接用，页面只取歌单名/封面（specialInfo）。"""
    html = net.get_text(
        "https://www.kugou.com/yy/special/single/%s.html" % specialid,
        headers={"User-Agent": KUGOU_MOBILE_UA},
        timeout=15,
    )
    name = None
    image = None
    if html:
        m = re.search(r"var\s+specialInfo\s*=\s*(\{.*?\});", html, re.S)
        if m:
            try:
                si = json.loads(m.group(1))
                name = si.get("name")
                image = si.get("image")
            except ValueError:
                pass
    tracks = _kugou_track_list(str(specialid))
    if not tracks:
        raise Exception("酷狗歌单解析失败")
    return {
        "name": name or "酷狗歌单 %s" % specialid,
        "cover_url": image,
        "songs": [_kugou_parse_song(t) for t in tracks],
    }


# ---------------------------------------------------------------------------
# 酷我解析（m.kuwo.cn playListInfo 接口）
# ---------------------------------------------------------------------------

KUWO_API = "https://m.kuwo.cn/newh5app/wapi/api/www/playlist/playListInfo"


def _kuwo_parse_song(t):
    """歌曲 dict -> {title, artist, album}。多歌手用 & 分隔，归一化成 /。"""
    return {
        "title": t.get("name") or "",
        "artist": (t.get("artist") or "").replace("&", "/"),
        "album": t.get("album") or "",
    }


def _parse_kuwo(playlist_id):
    """酷我歌单：playListInfo 接口分页拉取，按 musicrid 去重。"""
    tracks = []
    page = 1
    meta = {}
    while True:
        d = net.get_json(
            KUWO_API,
            params={"pid": playlist_id, "pn": page, "rn": 100},
            timeout=15,
        )
        if not isinstance(d, dict):
            break
        data = d.get("data") or {}
        ml = data.get("musicList") or []
        if not ml:
            break
        if not meta:
            meta = data
        tracks.extend(ml)
        total = data.get("total") or 0
        if total <= len(tracks):
            break
        page += 1
    if not tracks:
        raise Exception("酷我歌单解析失败")
    seen = set()
    unique = []
    for t in tracks:
        rid = t.get("musicrid") or t.get("rid")
        if rid in seen:
            continue
        if rid:
            seen.add(rid)
        unique.append(t)
    return {
        "name": meta.get("name") or "酷我歌单 %s" % playlist_id,
        "cover_url": meta.get("img500") or meta.get("img") or None,
        "songs": [_kuwo_parse_song(t) for t in unique],
    }


# ---------------------------------------------------------------------------
# 导入：创建歌单 + 匹配本地 / 插入占位失效歌曲
# ---------------------------------------------------------------------------

def _find_or_insert_track(conn, song):
    """按歌名+歌手精确匹配本地有效歌曲；未匹配则插入占位歌曲（失效）。"""
    rows = conn.execute(
        "SELECT id FROM track WHERE title = ? "
        "AND is_audio_file_deleted = 0 AND is_admin_deleted = 0",
        (song["title"],),
    ).fetchall()
    target_artist = song["artist"]
    for r in rows:
        artists = _track_artist_names(conn, r[0])
        if artists and target_artist and (
            artists == target_artist or target_artist in artists
        ):
            return r[0]
    return _insert_placeholder_track(conn, song)


def _insert_placeholder_track(conn, song):
    """插入占位歌曲（无音频，is_audio_file_deleted=1 标记失效）+ 歌手/专辑关联。

    track.audio_file_id 为 NOT NULL，需先插入一条占位 audio_file 满足约束。
    """
    artist_ids = _resolve_artists(conn, song["artist"])
    album_id = _resolve_album(conn, song["album"], artist_ids)
    # 占位 audio_file（满足 NOT NULL：shared_library_id/path/name/suffix/size/stream_index）
    sl_row = conn.execute(
        "SELECT id FROM shared_library ORDER BY id LIMIT 1"
    ).fetchone()
    sl_id = sl_row[0] if sl_row else 0
    audio_id = _next_id(conn, "audio_file")
    conn.execute(
        "INSERT INTO audio_file (id, shared_library_id, path, name, suffix, "
        "size, stream_index, created_at) VALUES (?,?,?,?,?,0,0,CURRENT_TIMESTAMP)",
        (audio_id, sl_id, "placeholder://%s" % song["title"], song["title"], ""),
    )
    track_id = _next_id(conn, "track")
    conn.execute(
        "INSERT INTO track (id, guid, audio_file_id, shared_library_id, title, "
        "title_latin_full, album_id, year, disc_no, track_no, is_cue, duration_ms, "
        "start_offset_ms, end_offset_ms, metadata_mode, cloud_scrape_status, "
        "created_at, updated_at, is_audio_file_deleted, is_admin_deleted) "
        "VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,0,0,NULL,NULL,1,0,"
        "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,1,0)",
        (track_id, uuid.uuid4().hex, audio_id, sl_id, song["title"],
         song["title"], album_id),
    )
    for i, aid in enumerate(artist_ids):
        conn.execute(
            "INSERT INTO track_artist (id, track_id, artist_id, artist_order, "
            "created_at, updated_at) VALUES ("
            "  (SELECT COALESCE(MAX(id), 0) + 1 FROM track_artist), ?, ?, ?, "
            "  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (track_id, aid, i),
        )
    return track_id


def _add_to_playlist(conn, user_id, playlist_id, track_id):
    conn.execute(
        "INSERT INTO playlist_track (id, user_id, playlist_id, track_id, "
        "added_at, created_at, updated_at) VALUES ("
        "  (SELECT COALESCE(MAX(id), 0) + 1 FROM playlist_track), ?, ?, ?, "
        "  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (user_id, playlist_id, track_id),
    )


def import_playlist(user_id, playlist_url):
    """导入歌单。返回 {playlistId, name, total, matched, inserted, failed}。"""
    platform, pid = parse_playlist_url(playlist_url)
    if not platform or not pid:
        # 手机分享常给短链/跳转链接（163cn.tv、c.y.qq.com/u 等）：
        # 跟随跳转拿到真实链接再解析一次。
        final = net.get_final_url(playlist_url, timeout=10)
        if final and final != playlist_url:
            platform, pid = parse_playlist_url(final)
    if not platform or not pid:
        raise Exception("无法识别的歌单链接")
    if platform == "netease":
        info = _parse_netease(pid)
    elif platform == "qq":
        info = _parse_qq(pid)
    elif platform == "kugou":
        info = _parse_kugou(pid)
    else:
        info = _parse_kuwo(pid)

    conn = _db_connect()
    conn.row_factory = None
    try:
        # 创建歌单
        playlist_guid = uuid.uuid4().hex
        playlist_id = _next_id(conn, "playlist")
        cover_guid = None
        if info.get("cover_url"):
            image = _download_image(info["cover_url"])
            if image:
                cover_guid = uuid.uuid4().hex
                _write_cover_file("playlist", cover_guid, image)
        conn.execute(
            "INSERT INTO playlist (id, guid, name, cover_guid, user_id, "
            "created_at, updated_at) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP,"
            "CURRENT_TIMESTAMP)",
            (playlist_id, playlist_guid, info["name"], cover_guid, user_id),
        )

        matched = inserted = failed = 0
        for song in info["songs"]:
            if not song["title"]:
                failed += 1
                continue
            try:
                track_id = _find_or_insert_track(conn, song)
                _add_to_playlist(conn, user_id, playlist_id, track_id)
                row = conn.execute(
                    "SELECT is_audio_file_deleted FROM track WHERE id = ?",
                    (track_id,),
                ).fetchone()
                if row and row[0]:
                    inserted += 1
                else:
                    matched += 1
                # 每首歌提交，释放写锁
                conn.commit()
            except Exception as e:  # noqa: BLE001 单首失败不中断
                failed += 1
        return {
            "playlistId": playlist_guid,
            "name": info["name"],
            "total": len(info["songs"]),
            "matched": matched,
            "inserted": inserted,
            "failed": failed,
        }
    finally:
        conn.close()
