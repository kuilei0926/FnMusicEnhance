# FnMusicEnhance · 飞牛音乐增强

通过服务端口为飞牛音乐（trim.music）提供其原生不存在的接口，增强手机 App 功能。

- 仅依赖 Python 标准库（无第三方依赖）
- 监听 `38200` 端口
- 依赖应用：`trim.music`

## 功能

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 探活 + token 校验 |
| `GET /music/api/v1/folder/list` | 文件夹视图（目录树 + 文件 + track guid） |
| `POST /music/api/v1/lyric/list` | 歌词回写 |
| `POST /music/api/v1/cover` | 歌手 / 专辑封面写入 |
| `POST /music/api/v1/entity` | 歌手 / 专辑改名、创建实体 |

对应的手机端（FeiNiuMusic）功能：

- **歌词修改**：读取 / 编辑 / 保存歌词
- **歌手 / 专辑编辑**：改名、写封面、创建实体
- **文件夹视图**：按 NAS 目录层级浏览音乐，支持排序、分页、随机播放、递归搜索、CUE 整轨拆分

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

服务端按 track guid 查 `lyric.stored_guid`，无记录自动新增，写入 `{LYRIC_ROOT}/{stored_guid[:2]}/{stored_guid}`。

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

## 配置

| 环境变量 | 说明 | 默认 |
| --- | --- | --- |
| `PORT` | 监听端口 | `38200` |
| `LOG_FILE` | 日志文件 | `/var/apps/FnMusicEnhance/var/app.log` |
| `LYRIC_ROOT` | 歌词存放根目录 | `/var/apps/trim.music/meta/lyric` |
| `COVER_ROOT` | 封面存放根目录 | `/var/apps/trim.music/meta/cover` |
| `MUSIC_DB` | 飞牛音乐数据库 | `/usr/local/apps/@appdata/trim.music/db/music.db` |

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
