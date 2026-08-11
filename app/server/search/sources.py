#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search.sources - 搜索平台注册表（元数据）。

每个平台：name（显示名）、capabilities（searchSongs/searchCovers/getLyrics）、
searchTypes（平台自己的搜索类型码，song/artist/album）、defaultSearchType、
config（可选，如 apple 的 region）。

impl 字段由 search.platforms 包在 import 时填充（各平台模块的搜索函数）。
"""

SOURCE_REGISTRY = {
    "netease": {
        "name": "网易云音乐",
        "capabilities": ["searchSongs", "searchCovers", "getLyrics"],
        "searchTypes": {"song": 1, "artist": 100, "album": 10},
        "defaultSearchType": 1,
        "config": {},
        "impl": None,
    },
    "qq": {
        "name": "QQ音乐",
        "capabilities": ["searchSongs", "searchCovers", "getLyrics"],
        "searchTypes": {"song": 0, "artist": 0, "album": 0},
        "defaultSearchType": 0,
        "config": {},
        "impl": None,
    },
    "kugou": {
        "name": "酷狗音乐",
        "capabilities": ["searchSongs", "searchCovers", "getLyrics"],
        "searchTypes": {"song": 0, "artist": 0, "album": 0},
        "defaultSearchType": 0,
        "config": {},
        "impl": None,
    },
    "soda": {
        "name": "汽水音乐",
        "capabilities": ["searchSongs", "searchCovers", "getLyrics"],
        "searchTypes": {"song": 0, "artist": 0, "album": 0},
        "defaultSearchType": 0,
        "config": {},
        "impl": None,
    },
    "apple": {
        "name": "Apple Music",
        "capabilities": ["searchSongs", "searchCovers", "getLyrics"],
        "searchTypes": {"song": 0, "artist": 0, "album": 0},
        "defaultSearchType": 0,
        "config": {"region": "cn"},
        "impl": None,
    },
}


def list_sources():
    """返回平台元数据列表（不含 impl），供 /search/sources 接口。"""
    result = []
    for sid, meta in SOURCE_REGISTRY.items():
        result.append({
            "id": sid,
            "name": meta["name"],
            "capabilities": list(meta["capabilities"]),
            "searchTypes": dict(meta["searchTypes"]),
            "defaultSearchType": meta["defaultSearchType"],
            "config": dict(meta.get("config") or {}),
        })
    return result


def has_capability(sid, capability):
    meta = SOURCE_REGISTRY.get(sid)
    return bool(meta and capability in meta["capabilities"])
