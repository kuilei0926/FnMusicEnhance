#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search.aggregate - 多平台搜索聚合与排序。

排序依赖客户端传入：sources 数组顺序即返回分组顺序（后端不自行定序）；
组内可按 sort 参数排序（default 保持平台返回顺序）。单平台失败静默跳过。
"""

import logging
from concurrent.futures import ThreadPoolExecutor

from . import sources as source_registry
from .lyric_tools import apply_lyric_options

log = logging.getLogger("search")


def _sources_in_order(requested, capability):
    """返回 (有效平台 id 列表, 忽略的未知 id 列表)。"""
    registry = source_registry.SOURCE_REGISTRY
    known = []
    ignored = []
    for sid in requested or []:
        if sid in registry and capability in registry[sid]["capabilities"]:
            known.append(sid)
        else:
            ignored.append(sid)
    return known, ignored


def _sort_items(items, sort):
    if not items or sort in (None, "default"):
        return items
    if sort == "duration_asc":
        return sorted(items, key=lambda x: x.get("duration") or 0)
    if sort == "duration_desc":
        return sorted(items, key=lambda x: x.get("duration") or 0, reverse=True)
    if sort == "title_asc":
        return sorted(items, key=lambda x: (x.get("title") or "").lower())
    if sort == "title_desc":
        return sorted(items, key=lambda x: (x.get("title") or "").lower(),
                      reverse=True)
    return items


def search_songs(keyword, sources, page=1, page_size=20, sort="default",
                 timeout=8):
    """按 sources 顺序聚合多平台歌曲搜索。返回 (groups, total)。"""
    if not keyword:
        return [], 0
    if sources is None:
        sources = list(source_registry.SOURCE_REGISTRY.keys())
    known, _ignored = _sources_in_order(sources, "searchSongs")
    if not known:
        return [], 0
    registry = source_registry.SOURCE_REGISTRY

    def _run(sid):
        try:
            impl = registry[sid]["impl"]
            items = impl.search_songs(keyword, page, page_size, timeout=timeout)
            return sid, [i for i in items if i.get("id")], None
        except Exception as e:  # noqa: BLE001 单平台失败隔离
            log.warning("平台 %s search_songs 失败: %s", sid, e)
            return sid, [], None

    groups = []
    total = 0
    with ThreadPoolExecutor(max_workers=len(known)) as pool:
        futures = [pool.submit(_run, sid) for sid in known]
        for fut in futures:
            sid, items, _err = fut.result()
            if not items:
                continue
            meta = registry[sid]
            groups.append({
                "pluginId": sid,
                "pluginName": meta["name"],
                "items": _sort_items(items, sort),
            })
            total += len(items)
    return groups, total


def search_covers(keyword, sources, search_type=0, page=1, page_size=10,
                  timeout=8):
    """按 sources 顺序聚合封面搜索。返回扁平 items 列表。"""
    if not keyword:
        return []
    if sources is None:
        sources = list(source_registry.SOURCE_REGISTRY.keys())
    known, _ignored = _sources_in_order(sources, "searchCovers")
    if not known:
        return []
    registry = source_registry.SOURCE_REGISTRY

    def _run(sid):
        try:
            impl = registry[sid]["impl"]
            items = impl.search_covers(keyword, search_type, page, page_size,
                                       timeout=timeout)
            out = []
            for item in items or []:
                item["pluginId"] = sid
                item["pluginName"] = registry[sid]["name"]
                out.append(item)
            return out
        except Exception as e:  # noqa: BLE001
            log.warning("平台 %s search_covers 失败: %s", sid, e)
            return []

    items = []
    with ThreadPoolExecutor(max_workers=len(known)) as pool:
        futures = [pool.submit(_run, sid) for sid in known]
        for fut in futures:
            items.extend(fut.result())
    return items


def get_lyrics(platform, song, timeout=8, convert="none",
               remove_blank_lines=False, filter_rules=None):
    """获取某平台歌词。返回响应 data dict。

    平台模块返回结构化歌词行（Lyrico structured 交换格式）：
        original:   [[lineStart, lineEnd, "整行文本"] | [lineStart, lineEnd, [[ws, we, "词"], ...]]]
        translated: [[lineStart, lineEnd, "文本"], ...]     # 可选
        romanization: [[lineStart, lineEnd, "文本"], ...]   # 可选

    应用客户端偏好（convert 简繁 / remove_blank_lines 空行 / filter_rules 过滤），
    统一生成行级 LRC 文本（降级用）+ 判定 type。
    """
    registry = source_registry.SOURCE_REGISTRY
    if platform not in registry or "getLyrics" not in registry[platform]["capabilities"]:
        return None
    impl = registry[platform]["impl"]
    result = impl.get_lyrics(song, timeout=timeout)
    if not isinstance(result, dict):
        result = {}

    # 自动搜索场景：客户端只有 title/artist、无源歌曲 id（songId 为标题占位），
    # 或按 id 未取到歌词 → 先用 title+artist 搜索拿候选 id，再取歌词。
    if not result.get("original") and song.get("title"):
        song_id = str(song.get("songId") or song.get("id") or "")
        keyword = " ".join(
            x for x in [song.get("title"), song.get("artist")] if x
        ).strip()
        if keyword:
            try:
                candidates = impl.search_songs(keyword, 1, 5, timeout=timeout)
            except Exception:
                candidates = []
            for cand in candidates or []:
                cid = cand.get("id") if isinstance(cand, dict) else None
                if not cid or cid == song_id:
                    continue
                song2 = dict(song)
                song2["songId"] = cid
                song2["id"] = cid
                internal = cand.get("internal") if isinstance(cand, dict) else None
                if isinstance(internal, dict):
                    song2["internal"] = internal
                r2 = impl.get_lyrics(song2, timeout=timeout)
                if isinstance(r2, dict) and r2.get("original"):
                    result = r2
                    break

    original = result.get("original")
    if not isinstance(original, list):
        original = []
    translated = result.get("translated")
    if not isinstance(translated, list):
        translated = []
    romanization = result.get("romanization")
    if not isinstance(romanization, list):
        romanization = []
    original = apply_lyric_options(original, convert, remove_blank_lines, filter_rules)
    translated = apply_lyric_options(translated, convert, remove_blank_lines, filter_rules)
    romanization = apply_lyric_options(romanization, convert, remove_blank_lines, filter_rules)
    has_word_level = any(
        isinstance(item[2], list) if isinstance(item, list) and len(item) > 2 else False
        for item in original
    )
    return {
        "platform": platform,
        "type": "structured" if original else "rawPlainLrc",
        "original": original,
        "translated": translated,
        "romanization": romanization,
        "rawPlainLrc": _lines_to_plain_lrc(original),
        "tags": {
            "ti": song.get("title") or "",
            "ar": song.get("artist") or "",
            "al": song.get("album") or "",
        },
    }


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
            text = "".join(w[2] for w in payload if isinstance(w, list) and len(w) > 2)
        else:
            text = str(payload)
        if not text.strip():
            continue
        out.append("[%02d:%02d.%02d]%s"
                   % (int(start) // 60000, (int(start) % 60000) // 1000,
                      (int(start) % 1000) // 10, text))
    return "\n".join(out)
