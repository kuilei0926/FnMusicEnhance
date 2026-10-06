# FnMusicEnhance · 飞牛音乐增强

通过 nginx(`/music-enhance/` 路径 → unix socket)为飞牛音乐（trim.music）提供其原生不存在的接口，增强手机 App 功能。

- 仅依赖 Python 标准库（无第三方依赖）
- 监听 unix socket `/var/run/fnmusic_enhance.sock`(经 nginx `/music-enhance/` 对外)
- 依赖应用：`trim.music`

## 功能

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 探活 + token 校验 |
| `GET /music/api/v1/folder/list` | 文件夹视图（目录树 + 文件 + track guid） |
| `POST /music/api/v1/lyric/list` | 歌词回写 |
| `POST /music/api/v1/cover` | 歌手 / 专辑封面写入 |
| `POST /music/api/v1/entity` | 歌手 / 专辑改名、创建实体 |
| `GET /music/api/v1/search/sources` | 数据源平台列表（客户端决定启用哪些） |
| `POST /music/api/v1/search/songs` | 多平台歌曲搜索（按客户端 `sources` 顺序分组） |
| `POST /music/api/v1/search/covers` | 封面搜索（扁平列表） |
| `POST /music/api/v1/search/lyrics` | 歌词获取（原文 + 翻译 + 罗马音） |

对应的手机端（FeiNiuMusic）功能：

- **歌词修改**：读取 / 编辑 / 保存歌词
- **歌手 / 专辑编辑**：改名、写封面、创建实体
- **文件夹视图**：按 NAS 目录层级浏览音乐，支持排序、分页、随机播放、递归搜索、CUE 整轨拆分
- **数据源搜索**：歌曲信息 / 歌词 / 封面一键匹配、批量匹配、播放无歌词自动搜索（数据源在 NAS 侧实现，App 端无需安装插件）

## 认证

所有业务接口通过请求头 `X-API-Key` 携带**飞牛音乐客户端登录后生成的 token**（`music.db` 的 `user_token` 表），服务端校验 token 存在、关联用户未停用且未过期。

```
X-API-Key: <user_token.token>
```

## 接口文档

统一响应信封：

```json
{ "code": 0, "msg": "", "data": { } }
```

`code == 0` 表示成功；错误时 `code` 为 400 / 401 / 404 / 500，`msg` 为中文提示。

### GET /health

探活，始终返回 HTTP 200。`data.auth` 三态：

| auth | 含义 |
| --- | --- |
| `ok` | token 有效 |
| `missing` | 未携带 X-API-Key |
| `invalid` | token 无效 / 过期 |

### GET /music/api/v1/folder/list

按 NAS 文件系统目录层级浏览音乐。**所有路径均为库内相对路径**（不含 `/vol1/...` 内部前缀）。

**参数**（query string）：

| 参数 | 说明 | 默认 |
| --- | --- | --- |
| `path` | 库内相对路径，`/` 为根；第一层是库根目录名（如 `/Music`） | `/` |
| `keyword` | 可选，在当前目录范围（含子文件夹）按文件名 / 曲目标题过滤 | 空 |
| `sort` | 排序键：`name` / `createdAt` / `duration` / `size` | `name` |
| `asc` | `true` 升序 / `false` 降序 | `true` |
| `page` | 页码（从 1 起） | `1` |
| `size` | 每页文件数，上限 500 | `50` |

**响应** `data`：

```json
{
  "libraryRoot": "Music",
  "path": "/Music/IU",
  "parent": "/Music",
  "folders": [ { "name": "A Flower Bookmark 2", "path": "/Music/IU/A Flower Bookmark 2" } ],
  "files": [
    {
      "name": "IU - 가을 아침.flac",
      "path": "/Music/IU/A Flower Bookmark 2/IU - 가을 아침.flac",
      "suffix": "flac",
      "size": 123456,
      "durationMs": 255000,
      "createdAt": 1786020546000,
      "audioSpec": { "bitrate": 768000, "sampleRate": 48000, "bitDepth": 16, "channel": 6, "codec": "eac3" },
      "tracks": [
        { "guid": "981df81d4b23479e8d733fe1e271fcfb", "title": "가을 아침",
          "durationMs": 255000, "trackNo": 1, "year": 2020, "isCue": false,
          "album": "A Flower Bookmark 2", "albumGuid": "0b24d2cd01854f35b909b83b79af7353",
          "coverId": "album_9d4c6a50609a4977a4d75f43d07cf57f",
          "artists": [ { "guid": "c6ca1fa346034a00acc46b6b0524d126", "name": "IU" } ] }
      ]
    }
  ],
  "total": 6,
  "fileTotal": 6
}
```

字段说明：

- `total`：当前目录**拆分后的歌曲总数**（CUE 整轨文件按内部曲目数计，用于显示"共 N 首"）
- `fileTotal`：当前目录**文件总数**（分页依据）
- `files[].tracks[]`：一个文件对应的全部曲目。普通文件 1 首；**CUE 整轨文件多首**（`isCue: true`），每首有独立 guid 可播放
- `coverId`：优先曲目封面 `track_<guid>`，回退专辑封面 `album_<guid>`
- 路径均为相对路径，不暴露 NAS 内部绝对路径

### POST /music/api/v1/lyric/list

歌词回写。请求体：

```json
{ "guid": "93c1eb619f0545148591c7f827225aa3", "content": "[00:25.53]不做考虑也没半点犹豫\n..." }
```

服务端按 track guid 定位曲目，**每次写入重新计算 `stored_guid`（新文件名）**：删除旧歌词文件、更新 `lyric.stored_guid`、写入 `{LYRIC_ROOT}/{stored_guid[:2]}/{stored_guid}`（首次自动新增 lyric 记录）。

### POST /music/api/v1/cover

歌手 / 专辑封面写入（base64 JSON）：

```json
{ "type": "artist", "guid": "<32hex 实体 guid>", "imageBase64": "<base64 图片字节>" }
```

仅接受 PNG / JPEG（magic-byte 校验），写入 `cover/{type}/{guid[:2]}/{guid}` 并更新 DB `cover_guid`。

### POST /music/api/v1/entity

改名（`action=update` 默认）或创建（`action=create`）：

```json
{ "type": "album", "guid": "<32hex>", "name": "新名称" }
{ "type": "album", "name": "新专辑", "action": "create" }
```

### GET /music/api/v1/search/sources

数据源平台列表。客户端据此决定启用哪些平台及顺序。

**响应** `data`：

```json
{ "sources": [
  { "id": "netease", "name": "网易云音乐",
    "capabilities": ["searchSongs", "searchCovers", "getLyrics"],
    "searchTypes": { "song": 1, "artist": 100, "album": 10 }, "defaultSearchType": 1, "config": {} },
  { "id": "qq", "name": "QQ音乐", "capabilities": ["searchSongs", "searchCovers", "getLyrics"], "searchTypes": { "song": 0, "artist": 0, "album": 0 }, "defaultSearchType": 0, "config": {} }
] }
```

### POST /music/api/v1/search/songs

多平台歌曲搜索。**结果分组顺序 = 请求 `sources` 数组顺序**（后端不自行定序，排序由客户端决定）。

**请求体**：

```json
{ "keyword": "晴天 周杰伦", "page": 1, "pageSize": 20,
  "sources": ["netease", "qq", "kugou"], "sort": "default" }
```

- `sources`：启用的平台 id 数组，**顺序即返回分组顺序**；缺省 = 全部平台；`[]` = 返回空
- `sort`：组内排序，`default`（平台返回顺序）| `duration_asc` | `duration_desc` | `title_asc` | `title_desc`
- 单平台失败静默跳过，不影响其他平台

**响应** `data`：

```json
{ "groups": [ {
    "pluginId": "netease", "pluginName": "网易云音乐",
    "items": [ { "id": "2652820720", "title": "晴天", "artist": "周杰伦", "album": "叶惠美",
        "duration": 269000, "date": "2003-07-31", "trackNumber": "3", "discNumber": "",
        "picUrl": "https://...jpg", "fields": {}, "internal": { "netease_id": "2652820720" } } ] } ],
  "total": 4 }
```

### POST /music/api/v1/search/covers

封面搜索，扁平列表。请求体：

```json
{ "keyword": "周杰伦", "searchType": 1, "sources": ["qq"], "pageSize": 5 }
```

`searchType`：0=歌曲 1=歌手 2=专辑。响应 `data`：

```json
{ "items": [ { "id": "97773", "title": "晴天", "picUrl": "https://...jpg", "pluginId": "qq", "pluginName": "QQ音乐" } ] }
```

### POST /music/api/v1/search/lyrics

歌词获取。请求体：

```json
{ "platform": "netease", "songId": "2652820720", "title": "晴天", "artist": "周杰伦", "album": "叶惠美", "duration": 269000,
  "convert": "simplifiedToTraditional", "removeBlankLines": true,
  "filterRules": ["作词", "来源 QQ音乐"] }
```

可选**客户端偏好参数**（服务端对 structured 歌词应用后返回）：
- `convert`：简繁转换，`none`（默认）| `simplifiedToTraditional` | `traditionalToSimplified`
- `removeBlankLines`：移除空行（默认 `false`）
- `filterRules`：非歌词内容过滤规则数组，命中任一规则的行被删除

响应 `data` 返回 **structured 行/词数组**（Lyrico 交换格式，`original` 行可带词级时间戳）+ 行级 LRC 降级文本（是否合并 / 渲染成逐字由客户端决定）：

```json
{ "platform": "qq", "type": "structured",
  "original": [ [0, 2250, [ [0, 160, "晴"], [160, 320, "天"], [320, 480, " "] ] ], [2250, 4000, "故事的小黄花"] ],
  "translated": [ [0, 2250, "原文对应译文"] ],
  "romanization": [],
  "rawPlainLrc": "[00:00.00]晴天 - 周杰伦\n...",
  "tags": { "ti": "晴天", "ar": "周杰伦", "al": "叶惠美" } }
```

- `original[][2]` 为**词级数组** `[wordStartMs, wordEndMs, "词"]` 时该行带逐字时间戳；为字符串时为行级
- `translated` / `romanization` 始终为行级 `[startMs, endMs, "文本"]`
- 平台逐字能力：QQ（QRC 3DES）、酷狗（KRC XOR）、汽水（timed-lyrics）带逐字；网易云（YRC 需登录，匿名降级行级）、Apple（第三方多数行级）
- 未知 `platform` 或该平台不支持歌词 → 400

### POST /music/api/v1/match/batch

**批量匹配**（服务端全自动处理）：为一批歌曲自动搜索 → 取首个候选（autoConfirm）→ 自动写入歌手 / 歌词 / 专辑 / 封面 / 曲目元数据。

**请求体**：

```json
{ "songs": [
    { "guid": "87ad4ed5...", "title": "孤雏", "artist": "AGA", "album": "Ginadoll Concert Live", "duration": 293336, "filePath": "/Music/xxx.flac" }
  ],
  "sources": ["netease", "qq", "kugou"],
  "wants": ["title", "artist", "album", "year", "trackNumber", "cover", "lyrics"],
  "writeMode": "fill",
  "preferFilename": false,
  "lyricOptions": { "convert": "none", "removeBlankLines": true, "filterRules": ["作词"] } }
```

- `songs[].guid` 必填；`filePath` 供 `preferFilename` 时构造关键词
- `wants`：要匹配的字段（默认 `["title","artist","album"]`；`cover`/`lyrics` 额外处理）
- `writeMode`：`fill`（仅空值）/ `overwrite`
- 服务端处理：歌手/专辑按名字查库幂等（不存在自动创建）、重建 `track_artist`、歌词重新计算 `stored_guid`（删除旧文件）、封面下载后重新计算 `cover_guid`（删除旧文件）

**响应** `data`：

```json
{ "total": 1, "success": 1, "failed": 0,
  "results": [ { "guid": "...", "matched": true, "matchedTitle": "孤雏",
    "matchedArtist": "AGA", "matchedAlbum": "孤雏",
    "fieldsUpdated": ["album_id"], "lyricsUpdated": true, "coverUpdated": true,
    "artistGuids": ["e2b62531..."], "albumGuid": "42a3c4bf...", "error": null } ] }
```

`total` / `success` / `failed` 让客户端能够区分完整结果、空响应和请求超时；`results` 的顺序不保证与请求顺序相同，客户端应按 `guid` 关联歌曲。

### POST /music/api/v1/match/refresh-all-songs
### POST /music/api/v1/match/refresh-artist-covers
### POST /music/api/v1/match/refresh-album-covers

**批量刷新（高危全量操作）**，请求体均可含 `sources`（平台 id 数组）：

- `refresh-all-songs`：遍历音乐库全部歌曲，逐一搜索并自动写入（同 `/match/batch` 参数：`wants`/`writeMode`/`lyricOptions`/`preferFilename`）
- `refresh-artist-covers`：遍历全部歌手，搜索头像并替换（**新 cover_guid，删除旧封面文件**）
- `refresh-album-covers`：遍历全部专辑，搜索封面并替换（**新 cover_guid，删除旧封面文件**）

**响应** `data`：

```json
{ "total": 480, "success": 450, "failed": 30,
  "results": [ { "guid": "...", "name": "AGA", "updated": true, "error": null } ] }
```

## 配置

| 环境变量 | 说明 | 默认 |
| --- | --- | --- |
| `SOCK_PATH` | unix socket 路径(nginx 代理用) | `/var/run/fnmusic_enhance.sock` |
| `LOG_FILE` | 日志文件 | `/var/apps/FnMusicEnhance/var/app.log` |
| `LYRIC_ROOT` | 歌词存放根目录 | `/var/apps/trim.music/meta/lyric` |
| `COVER_ROOT` | 封面存放根目录 | `/var/apps/trim.music/meta/cover` |
| `MUSIC_DB` | 飞牛音乐数据库 | `/usr/local/apps/@appdata/trim.music/db/music.db` |
| `REFRESH_RAW_LOG_FILE` | 全量/多选任务原始 JSONL 日志 | `LOG_FILE` 同目录下 `refresh.raw.log` |
| `QQ_HTTP_LOG_FILE` | QQ 请求人读摘要 | `LOG_FILE` 同目录下 `qq-http.log` |
| `QQ_HTTP_RAW_LOG_FILE` | QQ 请求原始 JSONL 日志 | `LOG_FILE` 同目录下 `qq-http.raw.log` |
| `QQ_QIMEI_CACHE` | QQ 设备标识缓存 | `LOG_FILE` 同目录下 `qq_device.json` |
| `QQ_DYNAMIC_QIMEI` | 启用动态 QIMEI；设为 `0` 使用 fallback | `1` |
| `QQ_SEARCH_MIN_INTERVAL` | QQ 请求最小间隔（秒） | `0.8` |
| `QQ_SEARCH_JITTER` | 请求间隔随机抖动（秒） | `0.3` |
| `QQ_BACKOFF_3` / `QQ_BACKOFF_5` / `QQ_BACKOFF_10` | 连续源错误后的等待时间（秒） | `5` / `15` / `60` |

## 运行日志

服务端将人读摘要和完整原始明细分开保存：

```text
/var/apps/FnMusicEnhance/var/refresh.log       # 全量/多选任务摘要
/var/apps/FnMusicEnhance/var/refresh.raw.log   # 完整任务 JSONL
/var/apps/FnMusicEnhance/var/qq-http.log       # QQ 请求摘要
/var/apps/FnMusicEnhance/var/qq-http.raw.log   # 完整 QQ 请求 JSONL
```

摘要只显示任务进度、歌曲结果、来源和错误；原始日志保留 URL、searchid、响应状态、耗时、响应大小和完整诊断字段。QQ 请求日志不会记录 Cookie、token 或认证密钥。

## 构建 / 发布

打标签 `v*`（或推 `main`）触发 GitHub Actions 自动打包 `.fpk` 并发布：

- 版本号从 `manifest` 的 `version` 字段读取
- 产物：`FnMusicEnhance-<version>.fpk`
- 工作流：`.github/workflows/release.yml`

本地打包：

```bash
fnpack build --directory .
```

> `fnpack.exe` 与 `__pycache__/` 已在 `.gitignore` 中排除。

## 项目结构

```
FnMusicEnhance/
├── app/
│   ├── server/server.py      # 服务端（标准库 HTTP 服务）
│   └── ui/config             # 桌面入口
├── cmd/
│   ├── main                  # 启动 / 停止 / 状态
│   └── *_init / *_callback   # 生命周期脚本
├── config/
│   ├── privilege             # 运行用户（root，需读写 trim.music 数据）
│   └── resource
├── wizard/                   # 安装向导
├── manifest                  # 应用元数据
├── ICON.PNG / ICON_256.PNG   # 应用图标
└── .github/workflows/        # 自动打包发布
```
