#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
match - 批量匹配（服务端全自动处理）。

流程（对齐 App 端 batch_match）：搜索取首个候选（autoConfirm）→ 自动写入
歌手 / 歌词 / 专辑 / 封面：
- 歌手：按名字查库，不存在自动创建（幂等）
- 专辑：按名字查库，不存在自动创建（幂等）
- 曲目：更新 title / album_id / year / track_no / disc_no，重建 track_artist
- 歌词：重新计算 stored_guid（新文件），删除旧歌词文件，更新 lyric 表
- 封面：下载候选封面（浏览器 UA），重新计算 cover_guid（新文件），删除旧封面
"""

import base64
import os
import re
import sqlite3
import tempfile
import uuid

from search import aggregate
from search.lyric_tools import apply_lyric_options
from search.sources import SOURCE_REGISTRY

# 与 server.py 共享的配置（环境变量注入）
MUSIC_DB = os.environ.get("MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db")
LYRIC_ROOT = os.environ.get("LYRIC_ROOT", "/var/apps/trim.music/meta/lyric")
COVER_ROOT = os.environ.get("COVER_ROOT", "/var/apps/trim.music/meta/cover")

GUID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_LRC_LINE_RE = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\](.*)")


def _log(msg):
    print("[match] %s" % msg, flush=True)


def _db_connect():
    return sqlite3.connect(MUSIC_DB, timeout=10)


def _next_id(conn, table):
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM %s" % table).fetchone()
    return (row[0] or 0) + 1


# ---------------------------------------------------------------------------
# 关键词构造（对齐 App 端 SongMatchService.buildKeyword）
# ---------------------------------------------------------------------------

def build_keyword(song, prefer_filename=False):
    """构造搜索关键词：优先文件名（去扩展名 + 去开头序号），否则 标题+歌手。"""
    if prefer_filename and song.get("filePath"):
        name = os.path.basename(str(song["filePath"]))
        name = re.sub(r"\.[^.]+$", "", name).strip()
        name = re.sub(r"^(?:\d{1,3}|\[?\d{1,3}\]?)[\s.\-_·]*(?=[^\s\d.])", "", name)
        if name:
            return name.strip()
    return " ".join(
        x for x in [song.get("title"), song.get("artist")] if x
    ).strip()


# ---------------------------------------------------------------------------
# 歌手 / 专辑解析（查库幂等，不存在自动创建）
# ---------------------------------------------------------------------------

def _resolve_artists(conn, artist_names, separator="/"):
    """把匹配到的歌手名解析为 artist id 列表（不存在自动创建）。"""
    if not artist_names:
        return []
    names = [
        n.strip() for n in re.split(separator, artist_names) if n.strip()
    ]
    ids = []
    for name in names:
        row = conn.execute(
            "SELECT id FROM artist WHERE name = ? LIMIT 1", (name,)
        ).fetchone()
        if row:
            ids.append(row[0])
        else:
            aid = _next_id(conn, "artist")
            conn.execute(
                "INSERT INTO artist (id, guid, name, name_latin_full, "
                "metadata_mode, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (aid, uuid.uuid4().hex, name, name),
            )
            ids.append(aid)
    return ids


def _resolve_album(conn, album_name, artist_ids):
    """把匹配到的专辑名解析为 album id（不存在自动创建 + album_artist 关联）。"""
    name = (album_name or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT id FROM album WHERE name = ? LIMIT 1", (name,)
    ).fetchone()
    if row:
        return row[0]
    aid = _next_id(conn, "album")
    conn.execute(
        "INSERT INTO album (id, guid, name, name_latin_full, metadata_mode, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (aid, uuid.uuid4().hex, name, name),
    )
    for i, artist_id in enumerate(artist_ids):
        conn.execute(
            "INSERT INTO album_artist (id, album_id, artist_id, artist_order, "
            "created_at, updated_at) VALUES ("
            "  (SELECT COALESCE(MAX(id), 0) + 1 FROM album_artist), ?, ?, ?, "
            "  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (aid, artist_id, i),
        )
    return aid


# ---------------------------------------------------------------------------
# 曲目元数据更新（含 track_artist 重建）
# ---------------------------------------------------------------------------

def _update_track(conn, track_row, patch, artist_ids, album_id, wants,
                  overwrite):
    """更新 track 元数据。返回 (更新了哪些字段, 是否更新歌手)。"""
    current = {
        "title": track_row["title"],
        "album_id": track_row["album_id"],
        "year": track_row["year"],
        "track_no": track_row["track_no"],
        "disc_no": track_row["disc_no"],
        "cover_guid": track_row["cover_guid"],
    }
    sets = []
    fields = []
    params = []

    def _apply(field, value, current_val):
        # 候选字段缺失（None / 空串）→ 不更新，绝不覆盖已有数据
        if value is None or value == "":
            return False
        if not overwrite and current_val not in (None, "", 0):
            return False
        sets.append("%s = ?" % field)
        fields.append(field)
        params.append(value)
        return True

    if "title" in wants and _apply("title", patch.get("title"), current["title"]):
        pass
    if "album" in wants and album_id is not None and \
            _apply("album_id", album_id, current["album_id"]):
        pass
    if "year" in wants:
        year = patch.get("year")
        y = int(year) if year and str(year).isdigit() else None
        _apply("year", y, current["year"])
    if "trackNumber" in wants:
        tn = patch.get("track_no")
        t = int(tn) if tn and str(tn).isdigit() else None
        _apply("track_no", t, current["track_no"])
    if "discNumber" in wants:
        dn = patch.get("disc_no")
        d = int(dn) if dn and str(dn).isdigit() else None
        _apply("disc_no", d, current["disc_no"])

    track_id = track_row["id"]
    if sets:
        sets.append("metadata_mode = 1")
        sets.append("updated_at = CURRENT_TIMESTAMP")
        conn.execute(
            "UPDATE track SET %s WHERE id = ?" % ", ".join(sets),
            (*params, track_id),
        )

    # 歌手：重建 track_artist 关联
    if "artist" in wants and artist_ids:
        conn.execute(
            "DELETE FROM track_artist WHERE track_id = ?", (track_id,)
        )
        for i, aid in enumerate(artist_ids):
            conn.execute(
                "INSERT INTO track_artist (id, track_id, artist_id, artist_order, "
                "created_at, updated_at) VALUES ("
                "  (SELECT COALESCE(MAX(id), 0) + 1 FROM track_artist), ?, ?, ?, "
                "  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (track_id, aid, i),
            )
    return fields, "artist" in wants and bool(artist_ids)


# ---------------------------------------------------------------------------
# 歌词写入（重新计算 stored_guid + 删除旧文件）
# ---------------------------------------------------------------------------

def _write_lyric_file(stored_guid, text):
    """原子写入歌词文件。返回 error（None 表示成功）。"""
    subdir = stored_guid[:2]
    dest_dir = os.path.join(LYRIC_ROOT, subdir)
    dest_file = os.path.join(dest_dir, stored_guid)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        _log("创建歌词目录失败 %s: %s" % (dest_dir, e))
        return "无法创建歌词目录"
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".lyric-", dir=dest_dir)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, dest_file)
    except OSError as e:
        _log("歌词写入失败 %s: %s" % (dest_file, e))
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return "歌词写入失败"
    return None


def _remove_lyric_file(stored_guid):
    if not stored_guid:
        return
    path = os.path.join(LYRIC_ROOT, stored_guid[:2], stored_guid)
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError as e:
        _log("删除旧歌词失败 %s: %s" % (path, e))


def _write_lyrics(conn, track_id, content):
    """写入歌词：重新计算 stored_guid（新文件），删除旧歌词文件，更新 lyric 表。"""
    content = (content or "").replace("\x00", "")
    row = conn.execute(
        "SELECT stored_guid FROM lyric WHERE track_id = ? LIMIT 1", (track_id,)
    ).fetchone()
    old = row[0] if row else None
    if old and not GUID_RE.match(str(old)):
        old = None
    new_guid = uuid.uuid4().hex
    if row:
        conn.execute(
            "UPDATE lyric SET stored_guid = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE track_id = ?",
            (new_guid, track_id),
        )
    else:
        conn.execute(
            "INSERT INTO lyric (id, guid, track_id, source, stored_guid, "
            "created_at, updated_at) VALUES ("
            "  (SELECT COALESCE(MAX(id), 0) + 1 FROM lyric),"
            "  ?, ?, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (uuid.uuid4().hex, track_id, new_guid),
        )
    if old and old != new_guid:
        _remove_lyric_file(old)
    return _write_lyric_file(new_guid, content)


# ---------------------------------------------------------------------------
# 封面写入（下载候选封面 + 新 cover_guid + 删旧）
# ---------------------------------------------------------------------------

def _write_cover_file(entity_type, cover_guid, image_bytes):
    """原子写入封面图片（entity_type: track/artist/album）。返回 error（None 表示成功）。"""
    subdir = cover_guid[:2]
    dest_dir = os.path.join(COVER_ROOT, entity_type, subdir)
    dest_file = os.path.join(dest_dir, cover_guid)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        _log("创建封面目录失败 %s: %s" % (dest_dir, e))
        return "无法创建封面目录"
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".cover-", dir=dest_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(image_bytes)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, dest_file)
    except OSError as e:
        _log("封面写入失败 %s: %s" % (dest_file, e))
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return "封面写入失败"
    return None


def _remove_cover_file(entity_type, cover_guid):
    if not cover_guid or not GUID_RE.match(str(cover_guid)):
        return
    path = os.path.join(COVER_ROOT, entity_type, cover_guid[:2], cover_guid)
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError as e:
        _log("删除旧封面失败 %s: %s" % (path, e))


def _write_entity_cover(conn, table, entity_id, entity_type, image_bytes):
    """写歌手/专辑/track 封面：新 cover_guid → 删旧文件 → 更新表 cover_guid。返回 error。"""
    row = conn.execute(
        "SELECT cover_guid FROM %s WHERE id = ?" % table, (entity_id,)
    ).fetchone()
    old = row[0] if row else None
    new_guid = uuid.uuid4().hex
    err = _write_cover_file(entity_type, new_guid, image_bytes)
    if err:
        return err
    conn.execute(
        "UPDATE %s SET cover_guid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
        % table,
        (new_guid, entity_id),
    )
    if old and old != new_guid:
        _remove_cover_file(entity_type, old)
    return None


def _write_cover(conn, track_id, image_bytes):
    """写 track 封面：下载字节 → 新 cover_guid → 删旧文件 → 更新 track.cover_guid。"""
    return _write_entity_cover(conn, "track", track_id, "track", image_bytes)


def _download_image(url, timeout=15):
    """下载图片字节（浏览器 UA，规避防盗链）。失败返回 None。"""
    from search import net
    text = net.request(
        "GET", url, headers={"Accept": "image/*, */*"}, timeout=timeout
    )
    if text is None:
        return None
    # net.request 返回文本；图片需原始字节 → 用 urllib 直接下载
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": net.DEFAULT_UA,
        "Accept": "image/*, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if len(raw) < 32:
            return None
        return raw
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 歌词获取（带客户端偏好）
# ---------------------------------------------------------------------------

def _fetch_lyrics(candidate, song, lyric_options, sources):
    """获取候选歌词（LRC 文本）。先候选平台，再遍历 sources。"""
    convert = (lyric_options or {}).get("convert", "none")
    remove_blank = bool((lyric_options or {}).get("removeBlankLines", False))
    filter_rules = (lyric_options or {}).get("filterRules") or None
    title = song.get("title") or candidate.get("title") or ""
    artist = song.get("artist") or candidate.get("artist") or ""
    album = song.get("album") or candidate.get("album") or ""
    duration = int(song.get("duration") or candidate.get("duration") or 0)
    internal = candidate.get("internal")
    song_obj = {
        "songId": str(candidate.get("id") or ""),
        "id": str(candidate.get("id") or ""),
        "title": title,
        "artist": artist,
        "album": album,
        "duration": duration,
        "internal": internal if isinstance(internal, dict) else {},
    }
    platforms = []
    if candidate.get("_platform"):
        platforms.append(candidate["_platform"])
    platforms += [s for s in sources if s != candidate.get("_platform")]
    for platform in platforms:
        d = aggregate.get_lyrics(
            platform, song_obj,
            convert=convert, remove_blank_lines=remove_blank,
            filter_rules=filter_rules,
        )
        if d and d.get("original"):
            text = _lines_to_plain_lrc(d["original"])
            if text and text.strip():
                return text
    return None


def _lines_to_plain_lrc(lines):
    """structured 行数组 → 行级 LRC 文本（每行取整行文本）。"""
    if not lines:
        return ""
    out = []
    for item in lines:
        if not isinstance(item, list) or len(item) < 3:
            continue
        start = item[0]
        payload = item[2]
        if isinstance(payload, list):
            text = "".join(
                w[2] for w in payload if isinstance(w, list) and len(w) > 2
            )
        else:
            text = str(payload)
        if not text.strip():
            continue
        out.append(
            "[%02d:%02d.%02d]%s"
            % (int(start) // 60000, (int(start) % 60000) // 1000,
               (int(start) % 1000) // 10, text)
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 单首匹配 + 批量入口
# ---------------------------------------------------------------------------

def _candidate_complete(item, wants):
    """候选是否满足 wants 要求的字段（都非空）。"""
    key_map = {"cover": "picUrl", "year": "date"}
    for field in wants:
        key = key_map.get(field, field)
        if not item.get(key):
            return False
    return True


def _search_first_complete(keyword, sources, wants, page_size=5, timeout=8):
    """逐平台搜索：第一个候选字段齐全（wants 都非空）即返回，**不再请求后续源**。

    第一源数据齐全时只请求第一源；缺字段才请求第二源（多源补充）。
    返回 (candidate, all_flat)；无齐全候选时 candidate 为 None（all_flat 供补充）。
    """
    all_flat = []
    for platform in sources:
        groups, _total = aggregate.search_songs(
            keyword, [platform], page=1, page_size=page_size, timeout=timeout
        )
        items = []
        for g in groups:
            for item in g.get("items") or []:
                item = dict(item)
                item["_platform"] = g["pluginId"]
                items.append(item)
        all_flat.extend(items)
        for item in items:
            if _candidate_complete(item, wants):
                return item, all_flat  # 第一源齐全即停，不请求后续源
    return None, all_flat


def _complement_candidate(flat, wants):
    """从多平台候选合并出一个最完整的候选。

    - 取第一个候选为基准；
    - **title/artist 本身缺失**时，从后续候选补（无法用缺失字段匹配）；
    - 其余字段（album/封面/年份/序号等）缺失时，**候选必须 标题+歌手 与基准
      完全一致（同一首歌）**才补充，避免补到不同版本/歌曲。
    """
    if not flat:
        return None
    base = dict(flat[0])
    field_map = {
        "album": "album",
        "cover": "picUrl",
        "year": "date",
        "trackNumber": "trackNumber",
        "discNumber": "discNumber",
    }
    # 1) 先补 title / artist 本身（base 缺失时，从后续候选找有值的）
    for field in ("title", "artist"):
        if field not in wants or base.get(field):
            continue
        for cand in flat[1:]:
            if cand.get(field):
                base[field] = cand[field]
                break
    # 2) 其余字段：候选必须 标题+歌手 与 base 完全一致（同一首歌）才补充
    base_title = (base.get("title") or "").strip()
    base_artist = (base.get("artist") or "").strip()
    for field, key in field_map.items():
        if field not in wants or base.get(key):
            continue
        for cand in flat[1:]:
            if (cand.get("title") or "").strip() != base_title:
                continue
            if (cand.get("artist") or "").strip() != base_artist:
                continue
            if cand.get(key):
                base[key] = cand[key]
                break
    return base


def _match_one(conn, song, sources, lyric_options, wants, write_mode,
               prefer_filename):
    """处理一首歌。返回结果 dict。"""
    guid = str(song.get("guid") or "")
    result = {"guid": guid, "matched": False, "error": None,
              "matchedTitle": "", "matchedArtist": "", "matchedAlbum": ""}
    if not guid:
        result["error"] = "缺少 guid"
        return result

    conn.row_factory = sqlite3.Row
    trow = conn.execute(
        "SELECT id, title, album_id, cover_guid, year, disc_no, track_no "
        "FROM track WHERE guid = ? LIMIT 1",
        (guid,),
    ).fetchone()
    if not trow:
        result["error"] = "未找到曲目"
        return result

    # prefer_filename 且未传 filePath：从 DB 取音频文件路径（构造关键词用）
    if prefer_filename and not song.get("filePath"):
        path_row = conn.execute(
            "SELECT af.path FROM audio_file af "
            "JOIN track t ON t.audio_file_id = af.id "
            "WHERE t.guid = ? LIMIT 1",
            (guid,),
        ).fetchone()
        if path_row and path_row[0]:
            song = dict(song)
            song["filePath"] = path_row[0]

    # 关键词 + 搜索（逐平台：第一源候选字段齐全即停，不请求后续源）
    keyword = build_keyword(song, prefer_filename)
    if not keyword:
        result["error"] = "无匹配关键词"
        return result
    candidate, flat = _search_first_complete(keyword, sources, wants)
    if candidate is None:
        # 无单源齐全候选：多源补充（缺失字段从后续平台补）
        candidate = _complement_candidate(flat, wants)
    if candidate is None:
        result["error"] = "未匹配到候选"
        return result

    result["matchedTitle"] = candidate.get("title") or ""
    result["matchedArtist"] = candidate.get("artist") or ""
    result["matchedAlbum"] = candidate.get("album") or ""

    overwrite = write_mode == "overwrite"

    # 歌手解析
    artist_ids = []
    if "artist" in wants and candidate.get("artist"):
        artist_ids = _resolve_artists(conn, candidate["artist"])

    # 专辑解析
    album_id = None
    if "album" in wants and candidate.get("album"):
        album_id = _resolve_album(conn, candidate["album"], artist_ids)

    # 曲目元数据更新
    patch = {
        "title": candidate.get("title") or "",
        "artist": candidate.get("artist") or "",
        "album": candidate.get("album") or "",
        "year": candidate.get("date") or "",
        "track_no": candidate.get("trackNumber") or "",
        "disc_no": candidate.get("discNumber") or "",
    }
    fields_updated, _ = _update_track(
        conn, trow, patch, artist_ids, album_id, wants, overwrite
    )

    # 歌词
    lyrics_updated = False
    if "lyrics" in wants and candidate.get("id"):
        content = _fetch_lyrics(candidate, song, lyric_options, sources)
        if content:
            err = _write_lyrics(conn, trow["id"], content)
            if err:
                result["error"] = err
            else:
                lyrics_updated = True

    # 封面
    cover_updated = False
    if "cover" in wants and candidate.get("picUrl"):
        image = _download_image(candidate["picUrl"])
        if image:
            err = _write_cover(conn, trow["id"], image)
            if err:
                result["error"] = err
            else:
                cover_updated = True

    result.update({
        "matched": True,
        "fieldsUpdated": fields_updated,
        "lyricsUpdated": lyrics_updated,
        "coverUpdated": cover_updated,
        "artistGuids": _artist_guids(conn, artist_ids),
        "albumGuid": _album_guid(conn, album_id),
    })
    return result


def _artist_guids(conn, artist_ids):
    if not artist_ids:
        return []
    ph = ",".join("?" * len(artist_ids))
    rows = conn.execute(
        "SELECT guid FROM artist WHERE id IN (%s)" % ph, artist_ids
    ).fetchall()
    return [r[0] for r in rows]


def _album_guid(conn, album_id):
    if album_id is None:
        return ""
    row = conn.execute("SELECT guid FROM album WHERE id = ?", (album_id,)).fetchone()
    return row[0] if row else ""


def batch_match(songs, sources=None, lyric_options=None, wants=None,
                write_mode="fill", prefer_filename=False, auto_confirm=True):
    """批量匹配。返回结果列表。

    - songs: [{guid, title, artist, album, duration, filePath, ...}]
    - sources: 启用的平台 id 列表（顺序即优先级）
    - wants: 要匹配的字段集合（title/artist/album/year/trackNumber/discNumber/cover/lyrics）
    - write_mode: fill（仅空值）/ overwrite
    - auto_confirm: 自动取第一个候选（服务端处理，无需确认）
    """
    if sources is None:
        sources = list(SOURCE_REGISTRY.keys())
    if wants is None:
        wants = {"title", "artist", "album"}
    wants = set(wants)
    results = []
    conn = _db_connect()
    try:
        for song in songs or []:
            if not isinstance(song, dict):
                continue
            try:
                r = _match_one(conn, song, sources, lyric_options, wants,
                               write_mode, prefer_filename)
                results.append(r)
            except Exception as e:  # noqa: BLE001 单首失败不中断批量
                _log("匹配失败 %s: %s" % (song.get("guid"), e))
                results.append({
                    "guid": song.get("guid") or "",
                    "matched": False,
                    "error": str(e),
                })
        conn.commit()
    finally:
        conn.close()
    return results


# ---------------------------------------------------------------------------
# 批量刷新（高危全量操作）：全部歌曲信息 / 全部歌手图片 / 全部专辑图片
# ---------------------------------------------------------------------------

def _track_artist_names(conn, track_id):
    rows = conn.execute(
        "SELECT ar.name FROM track_artist ta JOIN artist ar ON ar.id = ta.artist_id "
        "WHERE ta.track_id = ? ORDER BY ta.artist_order",
        (track_id,),
    ).fetchall()
    return "/".join(r[0] for r in rows if r[0])


def _track_album_name(conn, album_id):
    if not album_id:
        return ""
    row = conn.execute("SELECT name FROM album WHERE id = ?", (album_id,)).fetchone()
    return row[0] if row else ""


def refresh_all_songs(sources=None, lyric_options=None, wants=None,
                      write_mode="fill", prefer_filename=False):
    """遍历全部歌曲跑批量匹配（服务端全自动写入）。

    高危操作：对音乐库全部歌曲逐首搜索+写入，耗时较长。返回
    {total, success, failed, results}。
    """
    if sources is None:
        sources = list(SOURCE_REGISTRY.keys())
    if wants is None:
        wants = {"title", "artist", "album"}
    wants = set(wants)
    conn = _db_connect()
    conn.row_factory = sqlite3.Row
    results = []
    try:
        tracks = conn.execute(
            "SELECT id, guid, title, album_id FROM track "
            "WHERE is_audio_file_deleted = 0 AND is_admin_deleted = 0"
        ).fetchall()
        for t in tracks:
            song = {
                "guid": t["guid"],
                "title": t["title"] or "",
                "artist": _track_artist_names(conn, t["id"]),
                "album": _track_album_name(conn, t["album_id"]),
                "duration": 0,
            }
            try:
                r = _match_one(conn, song, sources, lyric_options, wants,
                               write_mode, prefer_filename)
                results.append(r)
            except Exception as e:  # noqa: BLE001 单首失败不中断
                results.append({
                    "guid": t["guid"], "matched": False, "error": str(e),
                })
        conn.commit()
    finally:
        conn.close()
    success = sum(1 for r in results if r.get("matched"))
    return {
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
        "results": results,
    }


def _search_cover_pic(keyword, sources, search_type, timeout=10):
    """搜索封面候选，返回第一个有 picUrl 的 URL。"""
    items = aggregate.search_covers(
        keyword, sources, search_type=search_type, page_size=3, timeout=timeout
    )
    for item in items or []:
        if isinstance(item, dict) and item.get("picUrl"):
            return item["picUrl"]
    return None


def _refresh_covers(table, entity_type, search_type, sources):
    """通用：遍历歌手/专辑，搜索封面并更新（新 cover_guid + 删旧文件）。"""
    if sources is None:
        sources = list(SOURCE_REGISTRY.keys())
    conn = _db_connect()
    conn.row_factory = sqlite3.Row
    results = []
    try:
        entities = conn.execute(
            "SELECT id, guid, name, cover_guid FROM %s" % table
        ).fetchall()
        for e in entities:
            name = (e["name"] or "").strip()
            entry = {"guid": e["guid"], "name": name, "updated": False,
                     "error": None}
            if not name:
                entry["error"] = "名称为空"
                results.append(entry)
                continue
            try:
                pic = _search_cover_pic(name, sources, search_type)
                if not pic:
                    entry["error"] = "未搜索到封面"
                    results.append(entry)
                    continue
                image = _download_image(pic)
                if not image:
                    entry["error"] = "下载封面失败"
                    results.append(entry)
                    continue
                err = _write_entity_cover(conn, table, e["id"], entity_type, image)
                entry["updated"] = err is None
                entry["error"] = err
            except Exception as ex:  # noqa: BLE001 单个失败不中断
                entry["error"] = str(ex)
            results.append(entry)
        conn.commit()
    finally:
        conn.close()
    success = sum(1 for r in results if r["updated"])
    return {
        "total": len(results),
        "success": success,
        "failed": len(results) - success,
        "results": results,
    }


def refresh_artist_covers(sources=None):
    """遍历全部歌手，搜索歌手封面并更新（新 cover_guid + 删旧）。"""
    return _refresh_covers("artist", "artist", 1, sources)


def refresh_album_covers(sources=None):
    """遍历全部专辑，搜索专辑封面并更新（新 cover_guid + 删旧）。"""
    return _refresh_covers("album", "album", 2, sources)
