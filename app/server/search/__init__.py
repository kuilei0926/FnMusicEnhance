#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search - FnMusicEnhance 数据源搜索包。

提供多平台音乐搜索/封面搜索/歌词获取，供 /music/api/v1/search/* 接口调用。
各平台实现移植自 musicdl（search/platforms/），仅依赖标准库。
"""

from . import sources
from . import platforms  # noqa: F401  导入即填充 SOURCE_REGISTRY 的 impl
from . import aggregate
