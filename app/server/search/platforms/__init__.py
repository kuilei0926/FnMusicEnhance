#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
搜索平台实现包。导入时把各平台模块挂到 SOURCE_REGISTRY 的 impl。

每个平台模块暴露统一接口（纯函数，仅依赖 search.net）：
    search_songs(keyword, page, page_size, timeout) -> list[dict]
    search_covers(keyword, search_type, page, page_size, timeout) -> list[dict]
    get_lyrics(song, timeout) -> {rawPlainLrc, translated, romanization}
"""

from ..sources import SOURCE_REGISTRY
from . import apple, kugou, netease, qq, soda

SOURCE_REGISTRY["netease"]["impl"] = netease
SOURCE_REGISTRY["qq"]["impl"] = qq
SOURCE_REGISTRY["kugou"]["impl"] = kugou
SOURCE_REGISTRY["soda"]["impl"] = soda
SOURCE_REGISTRY["apple"]["impl"] = apple
