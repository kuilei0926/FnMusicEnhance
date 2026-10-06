import json
import logging
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1] / "app" / "server"
import sys
sys.path.insert(0, str(ROOT))

from search import net
from search.platforms import qq
from search import aggregate
from search import qq_device
import match


class FakeResponse:
    def __init__(self, body, status=200, content_type="application/json"):
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.status = status
        self.headers = {"Content-Type": content_type, "Content-Encoding": ""}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit=-1): return self.body if limit < 0 else self.body[:limit]


class QqFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.qqlog = str(Path(self.temp.name) / "qq-http.log")
        self.refreshlog = str(Path(self.temp.name) / "refresh.log")
        self.refreshraw = str(Path(self.temp.name) / "refresh.raw.log")
        self.qqraw = str(Path(self.temp.name) / "qq-http.raw.log")
        self.env = patch.dict(os.environ, {
            "LOG_FILE": str(Path(self.temp.name) / "app.log"),
            "QQ_HTTP_LOG_FILE": self.qqlog,
            "REFRESH_LOG_FILE": self.refreshlog,
            "REFRESH_RAW_LOG_FILE": self.refreshraw,
            "QQ_HTTP_RAW_LOG_FILE": self.qqraw,
            "QQ_DYNAMIC_QIMEI": "0",
            "QQ_SEARCH_MIN_INTERVAL": "0",
            "QQ_SEARCH_JITTER": "0",
        })
        self.env.start()
        net._QQ_LOGGER = None
        net._QQ_RAW_LOGGER = None
        net._QQ_LOG_WARNED = False
        match._refresh_logger = None
        match._refresh_raw_logger = None
        match._refresh_log_warned = False
        qq._QQ_RATE_LIMITER._next_at = 0
        qq._QQ_RATE_LIMITER._backoff_until = 0
        qq._QQ_RATE_LIMITER._consecutive_failures = 0

    def tearDown(self):
        for logger_name in ("FnMusicEnhance.qq-http", "FnMusicEnhance.qq-http.raw",
                            "FnMusicEnhance.refresh", "FnMusicEnhance.refresh.raw"):
            logger = logging.getLogger(logger_name)
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
        self.env.stop()
        assert Path(self.temp.name).resolve().is_relative_to(Path(__file__).resolve().parent)
        self.temp.cleanup()

    def test_net_http_and_parse_errors_are_classified_and_ordered(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                "https://u.y.qq.com/cgi-bin/musicu.fcg", 429, "Too Many", {}, None)):
            result = net.request_detailed("POST", "https://u.y.qq.com/cgi-bin/musicu.fcg",
                                          json_body={"x": 1}, context={"keyword": "晴天 周杰伦"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["errorType"], "RATE_LIMITED")
        self.assertIn("HTTP 429", Path(self.qqlog).read_text(encoding="utf-8"))

        with patch("urllib.request.urlopen", return_value=FakeResponse("<html>blocked</html>", content_type="text/html")):
            data, meta = net.post_json_parsed_detailed(
                "https://u.y.qq.com/cgi-bin/musicu.fcg", json_body={}, context={"keyword": "晴天"})
        self.assertIsNone(data)
        self.assertEqual(meta["errorType"], "PARSE_ERROR")
        log = Path(self.qqlog).read_text(encoding="utf-8")
        self.assertIn("响应解析失败", log)
        self.assertTrue(all("\n" not in line.rstrip("\n") for line in log.splitlines()))

    def test_searchid_matches_upstream_shape(self):
        with patch.object(qq.random, "randint", side_effect=[1, 0]), patch.object(qq.time, "time", return_value=0):
            self.assertEqual(qq._searchid(), str(18014398509481984))

    def test_search_results_keep_source_diagnostic(self):
        payload = {"music.search.SearchCgiService.DoSearchForQQMusicMobile": {
            "data": {"body": {"item_song": [{
                "id": 1, "title": "晴天", "singer": [{"name": "周杰伦"}],
                "album": {"name": "叶惠美", "mid": "alb"}, "interval": 269,
            }]}}
        }}
        meta = {"ok": True, "httpStatus": 200, "elapsedMs": 12.3}
        with patch.object(qq.net, "post_json_parsed_detailed", return_value=(payload, meta)):
            items = qq.search_songs("晴天 周杰伦", page_size=5)
        self.assertEqual(len(items), 1)
        self.assertEqual(items.diagnostic["status"], "MATCHED")
        self.assertEqual(items[0]["title"], "晴天")

        with patch.object(qq.net, "post_json_parsed_detailed", return_value=(None, {
                "ok": False, "errorType": "RATE_LIMITED", "httpStatus": 429, "elapsedMs": 1})):
            items = qq.search_songs("后来 刘若英", page_size=5)
        self.assertEqual(items.diagnostic["status"], "SOURCE_ERROR")
        self.assertEqual(items.diagnostic["errorType"], "RATE_LIMITED")

    def test_aggregate_exposes_diagnostics_only_when_requested(self):
        class Impl:
            capabilities = None
            def search_songs(self, *args, **kwargs):
                return qq.SearchItems([], {"source": "qq", "status": "SOURCE_ERROR", "errorType": "RATE_LIMITED"})
        old = aggregate.source_registry.SOURCE_REGISTRY
        aggregate.source_registry.SOURCE_REGISTRY = {"qq": {"name": "QQ", "capabilities": {"searchSongs"}, "impl": Impl()}}
        try:
            diagnostics = {}
            groups, total = aggregate.search_songs("晴天", ["qq"], diagnostics=diagnostics)
            self.assertEqual(groups, [])
            self.assertEqual(total, 0)
            self.assertEqual(diagnostics["qq"]["errorType"], "RATE_LIMITED")
        finally:
            aggregate.source_registry.SOURCE_REGISTRY = old

    def test_legacy_lyric_fallback_has_song_id_context(self):
        song = {"songId": "123", "id": "123", "songmid": "mid123",
                "title": "测试", "artist": "歌手", "album": "专辑", "duration": 1000}
        with patch.object(qq.net, "post_json_parsed_detailed", return_value=({"req_0": {"data": {}}}, {"ok": True})), \
             patch.object(qq.net, "get_json_detailed", return_value=({"lyric": "", "trans": ""}, {"ok": True})) as legacy:
            result = qq.get_lyrics(song)
        self.assertEqual(result["original"], [])
        self.assertEqual(legacy.call_args.kwargs["context"]["songId"], "123")

    def test_qimei_can_be_disabled_and_cached(self):
        self.assertEqual(qq_device.get_qimei36(), qq_device.FALLBACK_QIMEI36)
        cache = Path(self.temp.name) / "device.json"
        cache.write_text(json.dumps({"q36": "a" * 36, "source": "cache"}), encoding="utf-8")
        with patch.dict(os.environ, {"QQ_DYNAMIC_QIMEI": "1", "QQ_QIMEI_CACHE": str(cache)}), \
             patch.object(qq_device, "_obtain_remote") as remote:
            qq_device._CACHED = None
            self.assertEqual(qq_device.get_qimei36(), "a" * 36)
            remote.assert_not_called()

    def test_batch_match_writes_summary_and_raw_details(self):
        class Conn:
            def __init__(self):
                self.commits = 0
                self.closed = False
            def commit(self):
                self.commits += 1
            def close(self):
                self.closed = True
        conn = Conn()
        songs = [{"guid": "g1", "title": "晴天", "artist": "周杰伦"},
                 {"guid": "g2", "title": "夜曲", "artist": "周杰伦"},
                 "invalid"]
        def fake_match(*args):
            song = args[1]
            return {"guid": song["guid"], "matched": song["guid"] == "g1",
                    "status": "MATCHED" if song["guid"] == "g1" else "NO_MATCH",
                    "matchedTitle": song["title"], "matchedArtist": song["artist"],
                    "matchedAlbum": "专辑", "fieldsUpdated": [],
                    "lyricsUpdated": False, "coverUpdated": False,
                    "error": None if song["guid"] == "g1" else "未匹配到候选"}
        with patch.object(match, "_db_connect", return_value=conn), \
             patch.object(match, "_match_one", side_effect=fake_match):
            result = match.batch_match(songs, sources=["qq"], wants=["title", "artist"])
        self.assertEqual(len(result), 2)
        self.assertEqual(conn.commits, 2)
        lines = Path(self.refreshlog).read_text(encoding="utf-8").splitlines()
        self.assertTrue(any("[多选匹配" in line and "开始" in line for line in lines))
        self.assertTrue(any("2/2" in line and "未匹配" in line for line in lines))
        self.assertTrue(any("完成" in line and "[多选匹配" in line for line in lines))
        self.assertTrue(any("成功 1；失败 1" in line for line in lines))
        self.assertFalse(any("guid=" in line or "event=" in line for line in lines))
        raw = [json.loads(line) for line in Path(self.refreshraw).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(raw[-1]["event"], "DONE")
        self.assertEqual(raw[-1]["taskType"], "batch")
        self.assertEqual(raw[0]["requestedCount"], 3)
        self.assertTrue(any(item.get("matchedTitle") == "晴天" for item in raw))

    def test_qq_summary_filters_duplicate_http_events_but_raw_keeps_all(self):
        net.log_qq_event("REQUEST", keyword="17岁 刘德华", searchid="211935835585349825", url="https://example.invalid/test")
        net.log_qq_event("RESPONSE", keyword="17岁 刘德华", status=200, elapsedMs=459.9, bytes=29095)
        net.log_qq_event("SEARCH", keyword="17岁 刘德华", status="MATCHED", elapsedMs=459.9, itemCount=5)
        text = Path(self.qqlog).read_text(encoding="utf-8")
        self.assertIn("[QQ搜索] 成功 · 5个候选 · 460毫秒", text)
        self.assertEqual(text.count("17岁 刘德华"), 1)
        self.assertNotIn("searchid", text)
        self.assertNotIn("http", text)
        raw = [json.loads(line) for line in Path(self.qqraw).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["event"] for record in raw], ["REQUEST", "RESPONSE", "SEARCH"])
        self.assertEqual(raw[0]["searchid"], "211935835585349825")
        self.assertEqual(raw[1]["bytes"], 29095)

    def test_long_fields_and_tracebacks_are_retained_in_raw(self):
        import time, unicodedata
        long_artist = "孙楠 / 李光洁 / 王赫野 / " * 20
        full_trace = "line 1\n" + "traceback detail " * 1000
        match._refresh_log("RESULT", "full-task-id-1234567890", time.monotonic(),
                           taskType="batch", index=1, total=2, processed=1,
                           title="24个比利 (Live)", artist=long_artist,
                           status="EXCEPTION", guid="1234567890abcdef", error="simulated", traceback=full_trace)
        text = Path(self.refreshlog).read_text(encoding="utf-8")
        self.assertNotIn("traceback", text)
        self.assertNotIn("1234567890abcdef", text)
        for line in text.splitlines():
            width = sum(0 if unicodedata.combining(ch) else 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in line)
            self.assertLessEqual(width, 64)
        raw = json.loads(Path(self.refreshraw).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(raw["artist"], long_artist)
        self.assertEqual(raw["traceback"], full_trace)
        self.assertEqual(raw["guid"], "1234567890abcdef")

    def test_summary_failure_does_not_prevent_raw_logs(self):
        import time
        with patch.object(match, "_refresh_line", side_effect=PermissionError("summary failed")), patch.object(sys, "stderr"):
            match._refresh_log("DONE", "test-summary-failure", time.monotonic(), taskType="batch", processed=0, total=0)
        raw = json.loads(Path(self.refreshraw).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(raw["event"], "DONE")
        with patch.object(net, "_format_human_event", side_effect=PermissionError("summary failed")), patch.object(sys, "stderr"):
            net.log_qq_event("SEARCH", status="MATCHED", keyword="晴天", itemCount=5)
        raw = json.loads(Path(self.qqraw).read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(raw["keyword"], "晴天")

    def test_concurrent_rate_limit_reservations_honor_backoff(self):
        limiter = qq._QQRateLimiter()
        now = [100.0]
        released = []
        def sleep(delay):
            now[0] += delay
        limiter._backoff_until = 105.0
        with patch.dict(os.environ, {"QQ_SEARCH_MIN_INTERVAL": "0.8", "QQ_SEARCH_JITTER": "0"}), \
             patch.object(qq.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(qq.time, "sleep", side_effect=sleep):
            for _ in range(3):
                limiter.wait()
                released.append(now[0])
        self.assertEqual(released, [105.0, 105.8, 106.6])

    def test_qimei_keeps_fetched_identity_when_cache_write_fails(self):
        remote = {"q36": "c" * 36, "q16": "c" * 16, "source": "tencent"}
        with patch.dict(os.environ, {"QQ_DYNAMIC_QIMEI": "1"}), \
             patch.object(qq_device, "_CACHED", None), \
             patch.object(qq_device, "_load_cache", return_value=None), \
             patch.object(qq_device, "_obtain_remote", return_value=remote), \
             patch.object(qq_device, "_save_cache", side_effect=PermissionError("cache denied")):
            self.assertEqual(qq_device.get_qimei36(), "c" * 36)

    def test_nonzero_qq_module_code_is_not_reported_as_no_results(self):
        payload = {"music.search.SearchCgiService.DoSearchForQQMusicMobile": {
            "code": 403, "data": {"body": {"item_song": []}},
        }}
        with patch.object(qq.net, "post_json_parsed_detailed", return_value=(payload, {"ok": True, "httpStatus": 200})):
            result = qq.search_songs("test")
        self.assertEqual(result.diagnostic["status"], "SOURCE_ERROR")
        self.assertEqual(result.diagnostic["moduleCode"], 403)

    def test_raw_diagnostics_redact_credentials_and_url_userinfo(self):
        net.log_qq_event("HTTP_ERROR", reason='qqmusic_key=private-test-key',
                         credential={"access_token": "private-test-token"},
                         status=403)
        content = Path(self.qqraw).read_text(encoding="utf-8")
        self.assertNotIn("private-test-key", content)
        self.assertNotIn("private-test-token", content)
        self.assertIn("<redacted>", content)
        self.assertEqual(net._safe_url("https://user:private-test-password@y.qq.com/path?authst=private"),
                         "https://y.qq.com/path")

    def test_batch_honors_global_prefer_filename_setting(self):
        class Conn:
            def commit(self): pass
            def close(self): pass
        with patch.object(match, "_db_connect", return_value=Conn()), \
             patch.object(match, "_match_one", return_value={"matched": False}) as process:
            match.batch_match([{"guid": "test", "title": "test"}], prefer_filename=True)
        self.assertTrue(process.call_args.args[-1])

    def test_refresh_line_is_compact_and_stable(self):
        line = match._refresh_line({
            "event": "RESULT", "taskId": "abc", "elapsedSeconds": 1.25,
            "taskType": "songs", "index": 35, "total": 221,
            "processed": 35, "success": 20, "failed": 15, "progressPercent": 15.8,
            "status": "SOURCE_ERROR", "source": "qq", "sourceError": "RATE_LIMITED",
            "httpStatus": 429, "title": "晴天", "artist": "周杰伦",
        })
        self.assertIn("35/221 (15.8%) · 源异常", line.splitlines()[0])
        self.assertIn("歌曲：晴天 — 周杰伦", line)
        self.assertIn("请求过于频繁（HTTP 429）", line)
        self.assertNotIn("{", line)
        self.assertNotIn("sourceError=", line)
        self.assertNotIn("processed=", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
