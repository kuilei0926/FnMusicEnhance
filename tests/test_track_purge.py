"""曲目清理接口（POST /music/api/v1/track/purge）的单元测试。

覆盖：隔离区模式、彻底删除、幽灵歌（文件已丢失）、路径白名单、
幂等、参数校验、数据库备份。
"""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "app" / "server"
import sys
sys.path.insert(0, str(ROOT))

# server.py 末尾继承 socketserver.UnixStreamServer，该类仅存在于 Unix。
# Windows 上补一个空占位类，使本测试文件也能在 Windows 上直接运行
# （本测试只调用模块内的处理函数，不会真的启动服务器；NAS 上不受影响）。
import socketserver  # noqa: E402
if not hasattr(socketserver, "UnixStreamServer"):
    class _UnixStreamServerStub(object):
        """占位：仅供 import 期满足继承关系。"""
    socketserver.UnixStreamServer = _UnixStreamServerStub

# server 的配置项在模块加载时读取，必须在 import 之前注入
_TMP = tempfile.mkdtemp(prefix="fnmt-purge-test-")
os.environ["MUSIC_DB"] = os.path.join(_TMP, "music.db")
os.environ["MUSIC_ROOT"] = os.path.join(_TMP, "music")
os.environ["QUARANTINE_ROOT"] = os.path.join(_TMP, "trash")
os.environ["LOG_FILE"] = os.path.join(_TMP, "app.log")

import server  # noqa: E402

TOKEN = "test-token-abc"
GUID_A = "a" * 32
GUID_B = "b" * 32
GUID_GHOST = "c" * 32


class FakeReq:
    """最小化的 http.server 请求对象，够 _read_json_body 使用。"""

    def __init__(self, payload, token=TOKEN):
        body = json.dumps(payload).encode("utf-8")
        self.headers = {"X-API-Key": token, "Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)


SCHEMA = """
CREATE TABLE user (id INTEGER PRIMARY KEY, status TEXT);
CREATE TABLE user_token (id INTEGER PRIMARY KEY, user_id INTEGER, token TEXT, expired_at TEXT);
CREATE TABLE album (id INTEGER PRIMARY KEY, guid TEXT, name TEXT);
CREATE TABLE artist (id INTEGER PRIMARY KEY, guid TEXT, name TEXT);
CREATE TABLE track_artist (track_id INTEGER, artist_id INTEGER, artist_order INTEGER);
CREATE TABLE lyric (id INTEGER PRIMARY KEY, track_id INTEGER, stored_guid TEXT);
CREATE TABLE audio_file (
    id INTEGER PRIMARY KEY, shared_library_id INTEGER, path TEXT, name TEXT,
    suffix TEXT, size INTEGER, stream_index INTEGER,
    is_physical_file_deleted INTEGER DEFAULT 0);
CREATE TABLE track (
    id INTEGER PRIMARY KEY, guid TEXT, audio_file_id INTEGER, shared_library_id INTEGER,
    title TEXT, album_id INTEGER, duration_ms INTEGER, metadata_mode INTEGER,
    is_admin_deleted INTEGER DEFAULT 0, is_audio_file_deleted INTEGER DEFAULT 0);
"""


class TrackPurgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _TMP
        self.music = Path(os.environ["MUSIC_ROOT"])
        self.trash = Path(os.environ["QUARANTINE_ROOT"])
        for p in (self.music, self.trash):
            if p.exists():
                shutil.rmtree(p)
            p.mkdir(parents=True)

        self.db = os.environ["MUSIC_DB"]
        con = sqlite3.connect(self.db)
        if not self._has_tables(con):
            con.executescript(SCHEMA)
        else:
            # 清表而不是删库文件：Windows 上 sqlite 文件句柄释放有延迟，
            # 删文件会报 "另一个程序正在使用此文件"。
            for tb in ("user", "user_token", "album", "artist", "track_artist",
                       "lyric", "audio_file", "track"):
                con.execute("DELETE FROM %s" % tb)
        con.execute("INSERT INTO user (id, status) VALUES (1, 'active')")
        con.execute(
            "INSERT INTO user_token (id, user_id, token, expired_at) VALUES (1, 1, ?, NULL)",
            (TOKEN,),
        )
        con.execute("INSERT INTO album (id, guid, name) VALUES (1, 'al1', '测试专辑')")
        con.execute("INSERT INTO artist (id, guid, name) VALUES (1, 'ar1', '测试歌手')")
        con.commit()
        con.close()

    @staticmethod
    def _has_tables(con):
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='track'"
        ).fetchall()
        return bool(rows)

    def tearDown(self):
        for p in (self.music, self.trash):
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)

    # ---- 造数据 ----------------------------------------------------------
    def _add_track(self, guid, title, *, with_file=True, outside=False):
        """插入一条 track(+audio_file)。

        with_file=True  正常曲目，音频文件真实存在
        with_file=False 幽灵歌 —— audio_file 记录仍在，但文件已不在磁盘上
        outside=True    文件路径不在音乐库目录内（应被白名单拒绝）
        """
        con = sqlite3.connect(self.db)
        if with_file:
            if outside:
                path = str(Path(self.tmp) / "outside" / "x.mp3")
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            else:
                path = str(self.music / title / ("%s.mp3" % title))
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"fake-audio")
            lrc = str(Path(path).with_suffix(".lrc"))
            Path(lrc).write_text("[00:00.00]lyric", encoding="utf-8")
        else:
            # 幽灵歌：记录指向一个磁盘上并不存在的文件
            path = str(self.music / title / ("%s.mp3" % title))
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        cur = con.execute(
            "INSERT INTO audio_file (id, shared_library_id, path, name, suffix, size,"
            " stream_index) VALUES (1, 1, ?, ?, 'mp3', 10, 0)", (path, title))
        afid = cur.lastrowid
        cur = con.execute(
            "INSERT INTO track (id, guid, audio_file_id, shared_library_id, title,"
            " album_id, duration_ms, metadata_mode) VALUES (1, ?, ?, 1, ?, 1, 1000, 0)",
            (guid, afid, title),
        )
        tid = cur.lastrowid
        con.execute("INSERT INTO track_artist (track_id, artist_id, artist_order) VALUES (?, 1, 0)", (tid,))
        con.execute("INSERT INTO lyric (id, track_id, stored_guid) VALUES (1, ?, 'xx')", (tid,))
        con.commit()
        con.close()
        return tid

    def _count(self, table, where="1=1", args=()):
        con = sqlite3.connect(self.db)
        n = con.execute("SELECT COUNT(*) FROM %s WHERE %s" % (table, where), args).fetchone()[0]
        con.close()
        return n

    # ---- 用例 ------------------------------------------------------------
    def test_quarantine_moves_file_and_removes_rows(self):
        """默认模式：文件移入隔离区（可恢复），数据库记录清除，备份生成。"""
        self._add_track(GUID_A, "歌A")
        code, err, data = server._handle_track_purge(
            FakeReq({"guids": [GUID_A]}))

        self.assertEqual(code, 0, err)
        self.assertEqual(len(data["removed"]), 1)
        self.assertEqual(data["removed"][0]["action"], "quarantine")
        # 文件不在原处，但进了隔离区
        moved = data["moved"][0]
        self.assertFalse(os.path.exists(moved["file"]))
        self.assertTrue(os.path.exists(moved["to"]))
        # 同名 .lrc 一起处理
        self.assertEqual(len(data["moved"]), 2)
        # 数据库记录清干净
        self.assertEqual(self._count("track"), 0)
        self.assertEqual(self._count("audio_file"), 0)
        self.assertEqual(self._count("track_artist"), 0)
        self.assertEqual(self._count("lyric"), 0)
        # 失去全部曲目的专辑/歌手空壳也清掉
        self.assertEqual(self._count("album"), 0)
        self.assertEqual(self._count("artist"), 0)
        # 备份存在且可打开
        self.assertTrue(os.path.exists(data["backup"]))
        con = sqlite3.connect(data["backup"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM user").fetchone()[0], 1)
        con.close()

    def test_purge_mode_deletes_file(self):
        """mode=purge：文件真正删除，隔离区保持为空。"""
        self._add_track(GUID_A, "歌A")
        code, err, data = server._handle_track_purge(
            FakeReq({"guids": [GUID_A], "mode": "purge"}))

        self.assertEqual(code, 0, err)
        self.assertEqual(data["removed"][0]["action"], "purge")
        self.assertEqual(data["moved"], [])
        self.assertEqual(len(list(self.trash.iterdir())), 0)
        self.assertEqual(self._count("track"), 0)

    def test_ghost_track_without_file_is_still_cleaned(self):
        """幽灵歌：音频文件已不在磁盘上，库记录照样能清掉。"""
        self._add_track(GUID_GHOST, "幽灵", with_file=False)
        code, err, data = server._handle_track_purge(
            FakeReq({"guids": [GUID_GHOST]}))

        self.assertEqual(code, 0, err)
        self.assertEqual(len(data["removed"]), 1)
        self.assertEqual(self._count("track"), 0)

    def test_file_outside_music_root_is_rejected(self):
        """文件不在音乐库目录内 → 拒绝处理，不动数据库。"""
        self._add_track(GUID_A, "歌A", outside=True)
        code, err, data = server._handle_track_purge(
            FakeReq({"guids": [GUID_A]}))

        self.assertEqual(code, 0, err)
        self.assertEqual(data["removed"], [])
        self.assertEqual(len(data["skipped"]), 1)
        self.assertIn("音乐库目录", data["skipped"][0]["reason"])
        self.assertEqual(self._count("track"), 1)

    def test_unknown_guid_is_skipped_not_error(self):
        """guid 不存在 → 记入 skipped，不影响其它曲目。"""
        self._add_track(GUID_A, "歌A")
        code, err, data = server._handle_track_purge(
            FakeReq({"guids": [GUID_A, GUID_B]}))

        self.assertEqual(code, 0, err)
        self.assertEqual(len(data["removed"]), 1)
        self.assertEqual(len(data["skipped"]), 1)
        self.assertIn("不存在", data["skipped"][0]["reason"])

    def test_invalid_guid_is_rejected(self):
        """guid 格式非法 → 400。"""
        code, err, _ = server._handle_track_purge(FakeReq({"guids": ["not-a-guid"]}))
        self.assertEqual(code, 400)
        self.assertIn("guid", err)

    def test_empty_guids_is_rejected(self):
        code, err, _ = server._handle_track_purge(FakeReq({"guids": []}))
        self.assertEqual(code, 400)

    def test_invalid_mode_is_rejected(self):
        code, err, _ = server._handle_track_purge(
            FakeReq({"guids": [GUID_A], "mode": "nuke"}))
        self.assertEqual(code, 400)

    def test_unauthorized_request_is_rejected(self):
        """没有有效 token → 401。"""
        code, err, _ = server._handle_track_purge(
            FakeReq({"guids": [GUID_A]}, token="bad-token"))
        self.assertEqual(code, 401)


if __name__ == "__main__":
    unittest.main()
