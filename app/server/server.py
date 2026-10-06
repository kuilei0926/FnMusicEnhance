#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FnMusicEnhance - 歌词回写 + 歌手/专辑元数据服务（仅依赖标准库）

端点：
    GET  /health                      探活 + token 校验
    GET  /music/api/v1/folder/list    文件夹视图（目录树 + 文件 + track guid）
    POST /music/api/v1/lyric/list     歌词回写（需 X-API-Key 请求头，值为数据库 user_token.token）
    POST /music/api/v1/cover          歌手/专辑封面写入（base64 JSON）
    POST /music/api/v1/entity         歌手/专辑改名 / 创建实体

认证说明：
    通过请求头 X-API-Key 携带飞牛音乐客户端登录后生成的 token，
    服务端在 music.db 的 user_token 表校验该 token 是否存在且未过期。

歌词流程：
  1. 根据 track guid 查询 music.db 获取 lyric.stored_guid
  2. 无记录时自动生成 stored_guid 并 INSERT lyric
  3. 将歌词写入 {LYRIC_ROOT}/{stored_guid[:2]}/{stored_guid}
  4. 更新 lyric 和 track 的 updated_at
"""

import base64
import http.server
import json
import os
import re
import sqlite3
import socketserver
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

from search import aggregate, sources
from match import (
    batch_match,
    refresh_all_songs,
    refresh_artist_covers,
    refresh_album_covers,
)
from playlist_import import import_playlist

# ---------------------------------------------------------------------------
# 配置项（由 cmd/main 通过环境变量注入）
# ---------------------------------------------------------------------------

LOG_FILE = os.environ.get("LOG_FILE", "/var/apps/FnMusicEnhance/var/app.log")
SOCK_PATH = os.environ.get("SOCK_PATH", "")   # unix socket 路径(nginx 代理用, 必填)
LYRIC_ROOT = os.environ.get("LYRIC_ROOT", "/var/apps/trim.music/meta/lyric")
COVER_ROOT = os.environ.get("COVER_ROOT", "/var/apps/trim.music/meta/cover")
MUSIC_DB = os.environ.get("MUSIC_DB", "/usr/local/apps/@appdata/trim.music/db/music.db")

MAX_BODY_BYTES = 16 * 1024 * 1024   # 含封面 base64（歌词仍受 5MB 文本限制）
MAX_IMAGE_BYTES = 8 * 1024 * 1024   # 解码后的封面图片字节上限
GUID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
ENTITY_TYPES = {"artist", "album"}  # 白名单：SQL 表名仅允许这两个值

PROBE_PATH = "/health"
LYRIC_PATH = "/music/api/v1/lyric/list"
COVER_PATH = "/music/api/v1/cover"
ENTITY_PATH = "/music/api/v1/entity"
FOLDER_PATH = "/music/api/v1/folder/list"
SEARCH_SOURCES_PATH = "/music/api/v1/search/sources"
SEARCH_SONGS_PATH = "/music/api/v1/search/songs"
SEARCH_COVERS_PATH = "/music/api/v1/search/covers"
SEARCH_LYRICS_PATH = "/music/api/v1/search/lyrics"
MATCH_BATCH_PATH = "/music/api/v1/match/batch"
MATCH_REFRESH_SONGS_PATH = "/music/api/v1/match/refresh-all-songs"
MATCH_REFRESH_ARTIST_COVERS_PATH = "/music/api/v1/match/refresh-artist-covers"
MATCH_REFRESH_ALBUM_COVERS_PATH = "/music/api/v1/match/refresh-album-covers"
PLAYLIST_IMPORT_PATH = "/music/api/v1/playlist/import"

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def log(msg):
    line = "[%s] %s\n" % (__import__("datetime").datetime.now().isoformat(timespec="seconds"), msg)
    try:
        with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 认证：music.db user_token 表校验
# ---------------------------------------------------------------------------

def _db_connect():
    """连接 music.db（SQLite 默认允许并发读；写操作加锁由 SQLite 自身处理）。

    timeout 为 busy_timeout（等待锁释放的秒数）：飞牛音乐服务可能同时写库，
    WAL 下写者互斥，加大等待避免「database is locked」。
    """
    return sqlite3.connect(MUSIC_DB, timeout=30)


def _execute_retry(conn, sql, params, attempts=6, delay=0.5):
    """执行写操作，`database is locked` 时短暂等待后重试。

    飞牛音乐服务可能同时写 music.db（WAL 下写者互斥），短暂锁冲突时重试
    几次通常能成功。
    """
    last_err = None
    for i in range(attempts):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" in str(e) and i < attempts - 1:
                time.sleep(delay)
                continue
            raise
    raise last_err


def _token_valid(token):
    """校验 token 是否存在于 user_token 且未过期、关联用户仍 active。

    expired_at 格式如 "2026-08-31 15:44:14.823473351+08:00"（含时区），
    与 datetime('now') 字符串比较即可正确判断大小。
    """
    if not token:
        return False
    sql = (
        "SELECT 1 FROM user_token ut "
        "JOIN user u ON u.id = ut.user_id "
        "WHERE ut.token = ? "
        "AND u.status = 'active' "
        "AND (ut.expired_at IS NULL OR ut.expired_at > datetime('now')) "
        "LIMIT 1"
    )
    try:
        conn = _db_connect()
        try:
            row = conn.execute(sql, (token,)).fetchone()
        finally:
            conn.close()
        return row is not None
    except sqlite3.Error as e:
        log("token 校验查询失败: %s" % e)
        return False


def _authenticate(req):
    """校验 X-API-Key（= music.db user_token.token）。"""
    provided = req.headers.get("X-API-Key", "")
    return _token_valid(provided)


def _user_id_by_token(token):
    """从 token 查 user_id（供创建歌单等关联当前用户）。"""
    if not token:
        return None
    try:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT user_id FROM user_token WHERE token = ? LIMIT 1",
                (token,),
            ).fetchone()
        finally:
            conn.close()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _read_json_body(req):
    """认证 + 读取请求体 + JSON 解析。

    返回 (payload, code, err)：code==0 时 payload 为 dict，否则 payload 为 None。
    """
    if not _authenticate(req):
        return None, 401, "unauthorized"
    raw_len = req.headers.get("Content-Length")
    try:
        length = int(raw_len) if raw_len else 0
    except ValueError:
        return None, 400, "invalid content length"
    if length <= 0 or length > MAX_BODY_BYTES:
        return None, 400, "请求体为空或过大"
    try:
        body = req.rfile.read(length)
    except OSError:
        return None, 400, "读取请求体失败"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, 400, "JSON 解析失败"
    if not isinstance(payload, dict):
        return None, 400, "请求体必须是 JSON 对象"
    return payload, 0, None


# ---------------------------------------------------------------------------
# 数据库操作
# ---------------------------------------------------------------------------

def _lookup_stored_guid(track_guid):
    """查询 track 与已有歌词记录。返回 (track_id, old_stored_guid, error)。

    old_stored_guid 为 None 表示无歌词记录（首次写入）；有值表示旧歌词文件标识，
    写入时删除旧文件并重新计算。
    """
    sql = (
        "SELECT l.stored_guid, t.id AS track_id "
        "FROM track t "
        "LEFT JOIN lyric l ON l.track_id = t.id "
        "WHERE t.guid = ? "
        "LIMIT 1"
    )
    try:
        conn = _db_connect()
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(sql, (track_guid,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log("数据库查询失败 guid=%s: %s" % (track_guid, e))
        return None, None, "数据库查询失败"

    if not row:
        return None, None, "未找到对应的曲目记录"

    track_id = row["track_id"]
    stored_guid_raw = row["stored_guid"]
    old = None
    if stored_guid_raw:
        old = stored_guid_raw.strip().lower()
        if not GUID_RE.match(old):
            log("stored_guid 格式异常: %s" % old)
            return None, None, "歌词记录标识格式异常"
    return track_id, old, None


def _update_timestamps(track_id):
    """更新 lyric 和 track 的 updated_at。"""
    try:
        conn = _db_connect()
        try:
            _execute_retry(
                conn,
                "UPDATE track SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (track_id,),
            )
            _execute_retry(
                conn,
                "UPDATE lyric SET updated_at = CURRENT_TIMESTAMP WHERE track_id = ?",
                (track_id,),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log("更新 updated_at 失败 track_id=%s: %s" % (track_id, e))
        return "更新时间戳失败"
    return None


# ---------------------------------------------------------------------------
# 核心逻辑
# ---------------------------------------------------------------------------

def _normalize_content(content):
    """去除 NUL 字节并校验歌词文本。返回 (text, error)。"""
    if not isinstance(content, str):
        return None, "content 必须是字符串"
    text = content.replace("\x00", "")
    if len(text) > 5 * 1024 * 1024:
        return None, "歌词内容过大"
    if text and not text.endswith("\n"):
        text += "\n"
    return text, None


def _validate_guid(guid):
    """校验 32 位十六进制 GUID。返回 (小写 guid, error)。"""
    if not isinstance(guid, str):
        return None, "guid 必须是字符串"
    guid = guid.strip().lower()
    if not GUID_RE.match(guid):
        return None, "guid 格式不正确"
    return guid, None


def _write_lyric(stored_guid, text):
    """原子写入：先落临时文件，再 rename 替换。"""
    subdir = stored_guid[:2]
    dest_dir = os.path.join(LYRIC_ROOT, subdir)
    dest_file = os.path.join(dest_dir, stored_guid)

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        log("创建目录失败 %s: %s" % (dest_dir, e))
        return "无法创建歌词目录"

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".lyric-", dir=dest_dir)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, dest_file)
    except OSError as e:
        log("写入失败 %s: %s" % (dest_file, e))
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return "歌词写入失败"
    return None


def _remove_lyric_file(stored_guid):
    """删除旧歌词文件（写入新歌词时清理）。失败仅记录，不影响写入。"""
    path = os.path.join(LYRIC_ROOT, stored_guid[:2], stored_guid)
    try:
        if os.path.exists(path):
            os.unlink(path)
            log("删除旧歌词文件 %s" % path)
    except OSError as e:
        log("删除旧歌词文件失败 %s: %s" % (path, e))


def _set_lyric_stored_guid(track_id, new_guid):
    """把 lyric 记录的 stored_guid 更新为新值（无记录则新增）。返回 error。

    每次写入重新计算 stored_guid（新文件名），DB 指向新文件。
    """
    try:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT id FROM lyric WHERE track_id = ? LIMIT 1", (track_id,)
            ).fetchone()
            if row:
                _execute_retry(
                    conn,
                    "UPDATE lyric SET stored_guid = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE track_id = ?",
                    (new_guid, track_id),
                )
            else:
                _execute_retry(
                    conn,
                    "INSERT INTO lyric (id, guid, track_id, source, stored_guid, "
                    "created_at, updated_at) VALUES ("
                    "  (SELECT COALESCE(MAX(id), 0) + 1 FROM lyric),"
                    "  ?, ?, 1, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP"
                    ")",
                    (uuid.uuid4().hex, track_id, new_guid),
                )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log("歌词记录更新失败 track_id=%s: %s" % (track_id, e))
        return "歌词记录更新失败"
    return None


# ---------------------------------------------------------------------------
# 歌手 / 专辑封面与实体操作
# ---------------------------------------------------------------------------

def _write_cover(entity_type, cover_guid, image_bytes):
    """原子写入封面图片。返回 error（None 表示成功）。"""
    subdir = cover_guid[:2]
    dest_dir = os.path.join(COVER_ROOT, entity_type, subdir)
    dest_file = os.path.join(dest_dir, cover_guid)

    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        log("创建封面目录失败 %s: %s" % (dest_dir, e))
        return "无法创建封面目录"

    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".cover-", dir=dest_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(image_bytes)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, dest_file)
    except OSError as e:
        log("封面写入失败 %s: %s" % (dest_file, e))
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return "封面写入失败"
    return None


def _remove_cover_file(entity_type, cover_guid):
    """best-effort 删除旧封面文件。失败仅记日志，不阻断。"""
    if not cover_guid or not GUID_RE.match(str(cover_guid)):
        return
    path = os.path.join(COVER_ROOT, entity_type, cover_guid[:2], cover_guid)
    try:
        if os.path.exists(path):
            os.unlink(path)
    except OSError as e:
        log("删除旧封面失败 %s: %s" % (path, e))


def _handle_cover(req):
    """写入歌手/专辑封面。返回 (code, err, data)。"""
    payload, code, err = _read_json_body(req)
    if code != 0:
        return code, err, None

    entity_type = payload.get("type")
    if entity_type not in ENTITY_TYPES:
        return 400, "type 必须是 artist 或 album", None

    entity_guid, err = _validate_guid(payload.get("guid"))
    if err:
        return 400, err, None

    b64 = payload.get("imageBase64")
    if not isinstance(b64, str) or not b64:
        return 400, "imageBase64 必须是 base64 字符串", None
    try:
        image_bytes = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        return 400, "imageBase64 解码失败", None
    if not image_bytes:
        return 400, "图片内容为空", None
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return 400, "图片过大", None
    # magic-byte 校验：仅接受 PNG/JPEG
    if not (image_bytes.startswith(b"\x89PNG\r\n\x1a\n") or image_bytes.startswith(b"\xff\xd8")):
        return 400, "仅支持 PNG/JPEG 图片", None

    table = entity_type
    conn = None
    try:
        conn = _db_connect()
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT guid, cover_guid FROM %s WHERE guid = ? LIMIT 1" % table,
            (entity_guid,),
        ).fetchone()
        if not row:
            conn.close()
            conn = None
            return 404, "实体不存在", None
        old_cover_guid = row["cover_guid"]
    except sqlite3.Error as e:
        log("实体查询失败 type=%s guid=%s: %s" % (entity_type, entity_guid, e))
        if conn:
            conn.close()
        return 500, "数据库查询失败", None
    finally:
        if conn:
            conn.close()

    new_cover_guid = uuid.uuid4().hex
    err = _write_cover(entity_type, new_cover_guid, image_bytes)
    if err:
        return 500, err, None

    try:
        conn = _db_connect()
        try:
            _execute_retry(
                conn,
                "UPDATE %s SET cover_guid = ?, updated_at = CURRENT_TIMESTAMP WHERE guid = ?"
                % table,
                (new_cover_guid, entity_guid),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        log("封面更新失败 type=%s guid=%s: %s" % (entity_type, entity_guid, e))
        _remove_cover_file(entity_type, new_cover_guid)
        return 500, "封面更新失败", None

    if old_cover_guid and str(old_cover_guid) != new_cover_guid:
        _remove_cover_file(entity_type, str(old_cover_guid))

    log("type=%s guid=%s 封面已更新 cover_guid=%s (%d 字节)" % (
        entity_type, entity_guid, new_cover_guid, len(image_bytes)))
    return 0, None, {"coverId": "%s_%s" % (entity_type, new_cover_guid)}


def _validate_name(name):
    """校验实体名称。返回 (规范化 name, error)。"""
    if not isinstance(name, str):
        return None, "name 必须是字符串"
    name = name.strip()
    if not name:
        return None, "name 不能为空"
    if len(name) > 200:
        return None, "name 过长"
    return name, None


def _handle_entity(req):
    """改名（action=update 默认）或创建（action=create）。返回 (code, err, data)。"""
    payload, code, err = _read_json_body(req)
    if code != 0:
        return code, err, None

    entity_type = payload.get("type")
    if entity_type not in ENTITY_TYPES:
        return 400, "type 必须是 artist 或 album", None

    action = payload.get("action", "update")
    if action not in ("update", "create"):
        return 400, "action 必须是 update 或 create", None

    name, err = _validate_name(payload.get("name"))
    if err:
        return 400, err, None

    table = entity_type
    if action == "create":
        new_guid = uuid.uuid4().hex
        try:
            conn = _db_connect()
            try:
                _execute_retry(
                    conn,
                    "INSERT INTO %s "
                    "(id, guid, name, name_latin_full, metadata_mode, cover_guid, "
                    " created_at, updated_at) VALUES "
                    "( (SELECT COALESCE(MAX(id), 0) + 1 FROM %s), ?, ?, ?, 1, NULL, "
                    "  CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)" % (table, table),
                    (new_guid, name, name),
                )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            log("实体创建失败 type=%s name=%s: %s" % (entity_type, name, e))
            return 500, "实体创建失败", None
        log("type=%s 已创建 name=%s guid=%s" % (entity_type, name, new_guid))
        return 0, None, {"guid": new_guid}

    # action == update：需要 guid
    entity_guid, err = _validate_guid(payload.get("guid"))
    if err:
        return 400, err, None

    try:
        conn = _db_connect()
        try:
            cur = _execute_retry(
                conn,
                "UPDATE %s SET name = ?, name_latin_full = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE guid = ?" % table,
                (name, name, entity_guid),
            )
            conn.commit()
            if cur.rowcount == 0:
                return 404, "实体不存在", None
        finally:
            conn.close()
    except sqlite3.Error as e:
        log("实体改名失败 type=%s guid=%s: %s" % (entity_type, entity_guid, e))
        return 500, "实体改名失败", None
    log("type=%s guid=%s 已改名为 %s" % (entity_type, entity_guid, name))
    return 0, None, {"guid": entity_guid}


def _handle_post(req):
    """歌词回写。每次写入重新计算 stored_guid（新文件），删除旧歌词文件。返回 (code, err, data)。"""
    payload, code, err = _read_json_body(req)
    if code != 0:
        return code, err, None

    # 校验 track guid
    track_guid, err = _validate_guid(payload.get("guid"))
    if err:
        return 400, err, None

    # 校验歌词内容
    text, err = _normalize_content(payload.get("content"))
    if err:
        return 400, err, None

    # 查询 track_id 与旧歌词文件标识
    track_id, old_guid, err = _lookup_stored_guid(track_guid)
    if err:
        log("guid=%s 查询失败: %s" % (track_guid, err))
        return 400, err, None

    # 重新计算新的 stored_guid（新文件名，删除旧的路径）
    new_guid = uuid.uuid4().hex

    # 更新 DB：lyric 表 stored_guid 指向新文件（无记录则新增）
    err = _set_lyric_stored_guid(track_id, new_guid)
    if err:
        log("guid=%s track_id=%s 更新歌词记录失败: %s" % (track_guid, track_id, err))
        return 500, err, None

    # 删除旧歌词文件（若有）
    if old_guid and old_guid != new_guid:
        _remove_lyric_file(old_guid)

    # 写新文件
    err = _write_lyric(new_guid, text)
    if err:
        log("guid=%s stored_guid=%s 写入失败: %s" % (track_guid, new_guid, err))
        return 500, err, None

    # 写入成功，更新 track 和 lyric 的 updated_at
    err = _update_timestamps(track_id)
    if err:
        log("guid=%s track_id=%s 时间戳更新失败: %s" % (track_guid, track_id, err))
        return 500, err, None

    log("guid=%s stored_guid=%s 歌词已写入 (%d 字节)%s" % (
        track_guid, new_guid, len(text.encode("utf-8")),
        " [新增]" if not old_guid else " [更新]"
    ))
    return 0, None, None


def _handle_probe(req):
    """
    始终返回 HTTP 200，通过 data.auth 区分三种状态：
      "ok"      — token 有效（或数据库不可用但未配置……此处按 token 判定）
      "missing" — 未携带 token
      "invalid" — token 无效/过期
    """
    provided = req.headers.get("X-API-Key", "")
    if not provided:
        return 0, {"status": "ok", "auth": "missing"}
    if _token_valid(provided):
        return 0, {"status": "ok", "auth": "ok"}
    return 0, {"status": "ok", "auth": "invalid"}


# ---------------------------------------------------------------------------
# 文件夹视图
# ---------------------------------------------------------------------------

def _normalize_folder_path(raw):
    """规范化相对目录路径。返回 (相对路径, error)。

    相对路径不含前导/，根目录返回 ""。拒绝空段、. 和 ..（防目录穿越）。
    """
    if raw is None:
        return "", None
    if not isinstance(raw, str):
        return None, "路径格式不正确"
    text = raw.strip()
    if text in ("", "/"):
        return "", None
    text = text.lstrip("/").rstrip("/")
    if not text:
        return None, "路径格式不正确"
    parts = []
    for seg in text.split("/"):
        if not seg or seg in (".", ".."):
            return None, "路径格式不正确"
        parts.append(seg)
    return "/".join(parts), None


def _like_escape(text):
    """转义 LIKE 模式中的 % _ 通配符，防止用户输入被当作通配符。"""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _handle_folder(req):
    """目录列表：返回当前目录的子目录 + 音频文件（含 track guid）。

    GET /music/api/v1/folder/list?path=<相对路径>

    相对路径以库根目录名为第一层（如 `/Music`），根请求 `/`（或缺省）返回
    库根入口文件夹。所有返回路径均为库内相对路径（不含 /vol3/1000/... 前缀），
    仅用于展示，不暴露 NAS 内部绝对路径。
    """
    if not _authenticate(req):
        return 401, "unauthorized", None

    query = parse_qs(urlsplit(req.path).query)

    # 搜索关键词（可选）：仅匹配当前目录范围内的文件名或曲目标题
    keyword = (query.get("keyword", [""])[0] or "").strip()
    if len(keyword) > 100:
        keyword = keyword[:100]

    # 平铺模式：true 时返回当前目录树所有歌曲（不按目录分组，仅文件）
    flatten = (query.get("flatten", ["false"])[0] or "false").lower() == "true"

    # 分页参数：page 从 1 起，page_size 默认 50，上限 500
    try:
        page = max(1, int(query.get("page", ["1"])[0] or 1))
    except (ValueError, TypeError):
        page = 1
    try:
        page_size = int(query.get("size", ["50"])[0] or 50)
    except (ValueError, TypeError):
        page_size = 50
    page_size = min(max(page_size, 1), 500)

    # 排序参数：sort 为 name/createdAt/duration/size，asc 为 true/false
    sort_key = query.get("sort", ["name"])[0] or "name"
    asc = (query.get("asc", ["true"])[0] or "true").lower() != "false"
    sort_col = {
        "name": "name",
        "createdAt": "created_at",
        "duration": "duration_ms",
        "size": "size",
    }.get(sort_key, "name")
    sort_dir = "ASC" if asc else "DESC"

    rel, err = _normalize_folder_path(query.get("path", [None])[0])
    if err:
        return 400, err, None
    try:
        conn = _db_connect()
        try:
            row = conn.execute(
                "SELECT id, path FROM shared_library ORDER BY id LIMIT 1"
            ).fetchone()
            if not row:
                return 404, "未找到共享音乐库", None
            root = row[1]
            root_name = os.path.basename(root)

            # 根请求：只返回库根入口文件夹，内容从 /Music 进入
            if not rel:
                return 0, None, {
                    "libraryRoot": root_name,
                    "path": "/",
                    "parent": None,
                    "folders": [{"name": root_name, "path": "/" + root_name}],
                    "files": [],
                    "total": 0,
                    "fileTotal": 0,
                }

            # rel 首段必须是库根目录名
            rel_parts = rel.split("/")
            if rel_parts[0] != root_name:
                return 404, "未找到该目录", None
            inner = "/".join(rel_parts[1:])  # 库根之内的相对路径（可能为空）
            dir_prefix = root if not inner else root + "/" + inner  # 当前目录绝对前缀（用于切片）
            like_prefix = _like_escape(dir_prefix)                   # 用于 LIKE 匹配
            like_pattern = like_prefix + "/%"

            rows = conn.execute(
                "SELECT af.id, af.path, af.name, af.suffix, af.size, af.duration_ms, "
                "       af.bitrate, af.sample_rate, af.bit_depth, af.channel, af.codec, "
                "       af.created_at "
                "FROM audio_file af "
                "WHERE af.is_physical_file_deleted = 0 "
                "AND af.path LIKE ? ESCAPE '\\' "
                "ORDER BY af.path",
                (like_pattern,),
            ).fetchall()

            folders = set()
            # (name, rel_path, suffix, size, duration_ms, audio_file_id, audioSpec, created_at)
            files = []
            for r in rows:
                af_id, full_path, name, suffix, size, dur, bitrate, rate, depth, ch, codec, created_at = r
                relative = full_path[len(dir_prefix):].lstrip("/")
                parts = relative.split("/")
                if len(parts) == 1:
                    files.append((
                        name, parts[0], suffix, size, dur, af_id,
                        {
                            "bitrate": bitrate,
                            "sampleRate": rate,
                            "bitDepth": depth,
                            "channel": ch,
                            "codec": codec,
                        },
                        created_at,
                    ))
                else:
                    # 搜索或平铺时：子目录中的文件也加入结果（扁平化展示当前目录树）
                    if keyword or flatten:
                        files.append((
                            name, relative, suffix, size, dur, af_id,
                            {
                                "bitrate": bitrate,
                                "sampleRate": rate,
                                "bitDepth": depth,
                                "channel": ch,
                                "codec": codec,
                            },
                            created_at,
                        ))
                    folders.add(parts[0])

            folder_list = [
                {"name": d,
                 "path": "/" + "/".join(
                     [s for s in [root_name] + inner.split("/") + [d] if s]
                 )}
                for d in sorted(folders, key=str.lower)
            ]

            # 搜索时：子目录仅保留名称匹配关键词的（用于定位，文件结果已扁平化）
            if keyword:
                kw = keyword.lower()
                folder_list = [
                    f for f in folder_list if kw in f["name"].lower()
                ]
            # 平铺模式：不显示子目录
            if flatten:
                folder_list = []

            # 一次性取这些文件的所有 track（含 CUE 一对多），JOIN 专辑/歌手带出增强信息；
            # 分块避免 SQLite 变量上限
            tracks_by_file = {}
            ids = [f[5] for f in files]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                ph = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT t.audio_file_id, t.guid, t.title, t.duration_ms, "
                    "       t.track_no, t.year, t.is_cue, "
                    "       a.name AS album, a.guid AS album_guid, "
                    "       CASE "
                    "         WHEN t.cover_guid IS NOT NULL AND t.cover_guid != '' THEN 'track_' || t.cover_guid "
                    "         WHEN a.cover_guid IS NOT NULL AND a.cover_guid != '' THEN 'album_' || a.cover_guid "
                    "         ELSE NULL "
                    "       END AS cover_id, "
                    "       (SELECT GROUP_CONCAT(ar.guid || char(31) || ar.name, '|') "
                    "          FROM track_artist ta JOIN artist ar ON ar.id = ta.artist_id "
                    "         WHERE ta.track_id = t.id) AS artists_raw "
                    "FROM track t LEFT JOIN album a ON a.id = t.album_id "
                    "WHERE t.audio_file_id IN (%s) "
                    "AND t.is_audio_file_deleted = 0 AND t.is_admin_deleted = 0" % ph,
                    chunk,
                ).fetchall()
                for af_id, guid, title, dur, track_no, year, is_cue, album, album_guid, cover_id, artists_raw in rows:
                    # artists_raw 形如 "guid1\x1fname1|guid2\x1fname2"；按 | 拆分后按 \x1f 再拆
                    artists = []
                    if artists_raw:
                        for pair in artists_raw.split("|"):
                            if "\x1f" in pair:
                                ag, an = pair.split("\x1f", 1)
                                artists.append({"guid": ag, "name": an})
                            elif pair:
                                artists.append({"guid": "", "name": pair})
                    tracks_by_file.setdefault(af_id, []).append({
                        "guid": guid,
                        "title": title,
                        "durationMs": dur,
                        "trackNo": track_no,
                        "year": year,
                        "isCue": bool(is_cue),
                        "album": album,
                        "albumGuid": album_guid,
                        "coverId": cover_id,
                        "artists": artists,
                    })

            def _to_epoch_ms(dt_str):
                """把 audio_file.created_at（如 2026-07-31 03:42:23）转成 epoch 毫秒；解析失败返回 None。"""
                if not dt_str:
                    return None
                import datetime as _dt
                try:
                    dt = _dt.datetime.strptime(str(dt_str)[:19], "%Y-%m-%d %H:%M:%S")
                    return int(dt.timestamp() * 1000)
                except ValueError:
                    return None

            def _sort_key(item):
                """按请求的排序键生成排序键；null 值排最后。"""
                field = {"duration": "durationMs"}.get(sort_key, sort_key)
                val = item.get(field)
                if val is None:
                    return (1, 0, "")
                if isinstance(val, (int, float)):
                    return (0, val, "")
                return (0, 0, str(val).lower())

            file_list = [
                {
                    "name": f[0],
                    "path": "/" + f[1],
                    "suffix": f[2],
                    "size": f[3],
                    "durationMs": f[4],
                    "audioSpec": f[6],
                    "createdAt": _to_epoch_ms(f[7]),
                    "tracks": tracks_by_file.get(f[5], []),
                }
                for f in files
            ]
            file_list.sort(key=_sort_key, reverse=not asc)

            # 关键词过滤（当前目录范围）：匹配文件名或任意曲目标题
            if keyword:
                kw = keyword.lower()
                file_list = [
                    item for item in file_list
                    if kw in item["name"].lower()
                    or any(kw in t.get("title", "").lower() for t in item["tracks"])
                ]

            # 文件总数（分页依据）
            file_total = len(file_list)
            # 总歌曲数 = 拆分后的曲目数（CUE 整轨文件算多首）
            total = sum(len(item["tracks"]) for item in file_list)
            # 分页切片：一页 page_size 个文件
            offset = (page - 1) * page_size
            file_list = file_list[offset:offset + page_size]

            cur_path = "/" + "/".join([root_name] + inner.split("/")) if inner \
                else "/" + root_name
            if inner:
                parent_parts = rel_parts[:-1]
                parent = "/" + "/".join(parent_parts)
            else:
                parent = "/"

            data = {
                "libraryRoot": root_name,
                "path": cur_path,
                "parent": parent,
                "folders": folder_list,
                "files": file_list,
                "total": total,
                "fileTotal": file_total,
            }
            return 0, None, data
        finally:
            conn.close()
    except sqlite3.Error as e:
        log("文件夹查询失败: %s" % e)
        return 500, "数据库查询失败", None


# ---------------------------------------------------------------------------
# 数据源搜索（search 包，移植 musicdl）
# ---------------------------------------------------------------------------

def _int_field(payload, key, default):
    try:
        return int(payload.get(key, default))
    except (TypeError, ValueError):
        return default


def _handle_search_sources(req):
    """GET /music/api/v1/search/sources - 可用平台列表。"""
    if not _authenticate(req):
        return 401, "unauthorized", None
    return 0, None, {"sources": sources.list_sources()}


def _handle_search_songs(req):
    """POST /music/api/v1/search/songs - 多平台歌曲搜索（按客户端排序聚合）。"""
    payload, code, err = _read_json_body(req)
    if code != 0:
        return code, err, None
    keyword = payload.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip():
        return 400, "keyword 不能为空", None
    req_sources = payload.get("sources")
    if req_sources is not None and not isinstance(req_sources, list):
        return 400, "sources 必须是数组", None
    page = max(_int_field(payload, "page", 1), 1)
    page_size = min(max(_int_field(payload, "pageSize", 20), 1), 50)
    sort = payload.get("sort", "default")
    if not isinstance(sort, str):
        sort = "default"
    groups, total = aggregate.search_songs(
        keyword.strip(), req_sources, page=page, page_size=page_size, sort=sort)
    return 0, None, {"groups": groups, "total": total}


def _handle_search_covers(req):
    """POST /music/api/v1/search/covers - 封面搜索（扁平列表）。"""
    payload, code, err = _read_json_body(req)
    if code != 0:
        return code, err, None
    keyword = payload.get("keyword")
    if not isinstance(keyword, str) or not keyword.strip():
        return 400, "keyword 不能为空", None
    req_sources = payload.get("sources")
    if req_sources is not None and not isinstance(req_sources, list):
        return 400, "sources 必须是数组", None
    search_type = _int_field(payload, "searchType", 0)
    page = max(_int_field(payload, "page", 1), 1)
    page_size = min(max(_int_field(payload, "pageSize", 10), 1), 50)
    items = aggregate.search_covers(
        keyword.strip(), req_sources, search_type=search_type,
        page=page, page_size=page_size)
    return 0, None, {"items": items}


def _handle_search_lyrics(req):
    """POST /music/api/v1/search/lyrics - 歌词获取。"""
    payload, code, err = _read_json_body(req)
    if code != 0:
        return code, err, None
    platform = payload.get("platform")
    if not isinstance(platform, str) or not platform:
        return 400, "platform 不能为空", None
    song_id = payload.get("songId") or payload.get("id") or ""
    internal = payload.get("internal")
    song = {
        "songId": song_id,
        "id": song_id,
        "title": payload.get("title") or "",
        "artist": payload.get("artist") or "",
        "album": payload.get("album") or "",
        "duration": _int_field(payload, "duration", 0),
        "internal": internal if isinstance(internal, dict) else {},
    }
    # 客户端歌词偏好：简繁转换 / 移除空行 / 过滤规则
    convert = payload.get("convert") or "none"
    if convert not in ("none", "simplifiedToTraditional", "traditionalToSimplified"):
        convert = "none"
    remove_blank = bool(payload.get("removeBlankLines", False))
    filter_rules = payload.get("filterRules")
    if not isinstance(filter_rules, list):
        filter_rules = None
    data = aggregate.get_lyrics(
        platform, song,
        convert=convert,
        remove_blank_lines=remove_blank,
        filter_rules=filter_rules,
    )
    if data is None:
        return 400, "该平台不支持歌词", None
    return 0, None, data


def _handle_match_batch(req):
    """POST /music/api/v1/match/batch - 批量匹配（服务端全自动写入）。

    请求体：{ songs: [{guid, title, artist, album, duration, filePath}],
              sources, wants, writeMode, preferFilename, lyricOptions }
    """
    payload, code, err = _read_json_body(req)
    if code != 0:
        return code, err, None
    songs = payload.get("songs")
    if not isinstance(songs, list) or not songs:
        return 400, "songs 不能为空", None
    for s in songs:
        if not isinstance(s, dict) or not s.get("guid"):
            return 400, "每首歌曲需含 guid", None
    sources = payload.get("sources")
    if sources is not None and not isinstance(sources, list):
        return 400, "sources 必须是数组", None
    wants = payload.get("wants")
    if wants is not None and not isinstance(wants, list):
        return 400, "wants 必须是数组", None
    write_mode = payload.get("writeMode", "fill")
    if write_mode not in ("fill", "overwrite"):
        return 400, "writeMode 必须是 fill 或 overwrite", None
    prefer_filename = bool(payload.get("preferFilename", False))
    lyric_options = payload.get("lyricOptions")
    if lyric_options is not None and not isinstance(lyric_options, dict):
        lyric_options = None
    results = batch_match(
        songs,
        sources=sources,
        lyric_options=lyric_options,
        wants=wants,
        write_mode=write_mode,
        prefer_filename=prefer_filename,
    )
    total = len(results)
    success = sum(1 for result in results if result.get("matched"))
    return 0, None, {
        "total": total,
        "success": success,
        "failed": total - success,
        "results": results,
    }


def _handle_refresh(req, kind):
    """批量刷新（高危全量操作）：songs / artist-covers / album-covers。"""
    payload, code, err = _read_json_body(req)
    if code != 0:
        return code, err, None
    sources = payload.get("sources")
    if sources is not None and not isinstance(sources, list):
        return 400, "sources 必须是数组", None
    if kind == "songs":
        wants = payload.get("wants")
        if wants is not None and not isinstance(wants, list):
            return 400, "wants 必须是数组", None
        write_mode = payload.get("writeMode", "fill")
        if write_mode not in ("fill", "overwrite"):
            return 400, "writeMode 必须是 fill 或 overwrite", None
        lyric_options = payload.get("lyricOptions")
        if lyric_options is not None and not isinstance(lyric_options, dict):
            lyric_options = None
        data = refresh_all_songs(
            sources=sources,
            lyric_options=lyric_options,
            wants=wants,
            write_mode=write_mode,
            prefer_filename=bool(payload.get("preferFilename", False)),
        )
    elif kind == "artist-covers":
        data = refresh_artist_covers(sources=sources)
    else:
        data = refresh_album_covers(sources=sources)
    return 0, None, data


def _handle_playlist_import(req):
    """POST /music/api/v1/playlist/import - 导入歌单（网易云/QQ/酷狗）。"""
    payload, code, err = _read_json_body(req)
    if code != 0:
        return code, err, None
    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        return 400, "url 不能为空", None
    user_id = _user_id_by_token(req.headers.get("X-API-Key", ""))
    if user_id is None:
        return 401, "unauthorized", None
    try:
        data = import_playlist(user_id, url.strip())
    except Exception as e:  # noqa: BLE001 歌单解析失败给客户端明确提示
        return 400, str(e), None
    return 0, None, data


# ---------------------------------------------------------------------------
# HTTP 处理器
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    def address_string(self):
        # unix socket 下 client_address 是路径字符串而非 (host, port), 需兼容
        addr = getattr(self, "client_address", None)
        if isinstance(addr, tuple):
            return super().address_string()
        return str(addr) if addr else "-"

    def _respond(self, status, payload, content_type="application/json"):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self):
        path = urlsplit(self.path).path

        if self.command == "GET" and path == PROBE_PATH:
            code, data = _handle_probe(self)
            if code == 0:
                self._respond(200, {"code": 0, "msg": "", "data": data})
            return

        if self.command == "GET" and path == FOLDER_PATH:
            code, err, data = _handle_folder(self)
            self._respond_code(code, err, data)
            return

        if self.command == "GET" and path == SEARCH_SOURCES_PATH:
            code, err, data = _handle_search_sources(self)
            self._respond_code(code, err, data)
            return

        if self.command != "POST":
            self._respond(405, {"code": 405, "msg": "method not allowed", "data": None})
            return

        if path == LYRIC_PATH:
            code, err, data = _handle_post(self)
        elif path == COVER_PATH:
            code, err, data = _handle_cover(self)
        elif path == ENTITY_PATH:
            code, err, data = _handle_entity(self)
        elif path == SEARCH_SONGS_PATH:
            code, err, data = _handle_search_songs(self)
        elif path == SEARCH_COVERS_PATH:
            code, err, data = _handle_search_covers(self)
        elif path == SEARCH_LYRICS_PATH:
            code, err, data = _handle_search_lyrics(self)
        elif path == MATCH_BATCH_PATH:
            code, err, data = _handle_match_batch(self)
        elif path == MATCH_REFRESH_SONGS_PATH:
            code, err, data = _handle_refresh(self, "songs")
        elif path == MATCH_REFRESH_ARTIST_COVERS_PATH:
            code, err, data = _handle_refresh(self, "artist-covers")
        elif path == MATCH_REFRESH_ALBUM_COVERS_PATH:
            code, err, data = _handle_refresh(self, "album-covers")
        elif path == PLAYLIST_IMPORT_PATH:
            code, err, data = _handle_playlist_import(self)
        else:
            self._respond(404, {"code": 404, "msg": "not found", "data": None})
            return

        self._respond_code(code, err, data)

    def _respond_code(self, code, err, data):
        """按 (code, err, data) 三元组输出标准响应信封。"""
        if code == 0:
            self._respond(200, {"code": 0, "msg": "", "data": data})
        elif code == 401:
            self._respond(401, {"code": 401, "msg": "unauthorized", "data": None})
        else:
            self._respond(400 if code == 400 else 500, {"code": code, "msg": err, "data": None})

    def _safe_handle(self):
        try:
            self._handle()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log("未处理异常: %r" % (e,))
            try:
                self._respond(500, {"code": 500, "msg": "internal error", "data": None})
            except Exception:
                pass

    def do_POST(self):
        self._safe_handle()

    def do_GET(self):
        self._safe_handle()

    def do_PUT(self):
        self.do_GET()

    def do_DELETE(self):
        self.do_GET()


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """unix socket 版 HTTP 服务器(与官方 trim_music 同款)。"""
    daemon_threads = True
    allow_reuse_address = True


def main():
    sock = os.environ.get("SOCK_PATH", "")
    if not sock:
        log("未配置 SOCK_PATH，无法监听")
        return 1

    if os.path.exists(sock):
        try:
            os.unlink(sock)          # 清理上次遗留的 socket 文件
        except OSError:
            pass
    try:
        srv = ThreadingUnixHTTPServer(sock, Handler)
        os.chmod(sock, 0o666)        # 让 nginx 的 www-data 能连接
    except Exception as e:
        log("unix socket 启动失败: %s" % e)
        return 1

    log("服务启动，监听 unix socket %s" % sock)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
