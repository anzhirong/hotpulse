# hotpulse

多平台热点聚合的 **AstrBot 插件**。在飞书 / QQ 里发 `/热点`，返回微博、百度、头条、B站、36氪、知乎的实时榜单。

- 零额外进程：寄宿在 AstrBot 内，不占端口、不额外吃内存
- 零公网暴露：纯出站请求，无备案相关问题
- 单源降级：任一上游改版 / 403 / 超时，只让该源报「本次不可用」，整体不会失败
- 进程内 TTL 缓存 + 单飞：同一时刻多人查询，对上游只打一次

## 目录

```
hotpulse/
├── astrbot_plugin_hotpulse/     # 插件本体（这就是全部代码）
│   ├── main.py                  # 指令注册、消息渲染
│   ├── metadata.yaml            # 插件元数据（名称/版本/兼容平台）
│   ├── _conf_schema.json        # WebUI 可调配置项
│   ├── requirements.txt         # 仅 httpx（AstrBot 自带）
│   └── core/                    # 抓取内核：与 AstrBot 解耦
│       ├── fetcher.py           # 并发调度、缓存、单飞、降级
│       ├── models.py            # HotItem / SourceResult
│       ├── settings.py          # 配置
│       └── sources/             # 每平台一个适配器
└── dist/                        # 打包产物（不入 git）
```

## 安装

1. 把 `dist/astrbot_plugin_hotpulse.zip` 上传到服务器
2. 解压到 AstrBot 的插件目录，确认路径形如 `data/plugins/astrbot_plugin_hotpulse/main.py`
3. 重启容器，在飞书发 `/热点`

```bash
cd /opt/astrbot
unzip -o astrbot_plugin_hotpulse.zip -d /tmp/hp
cp -r /tmp/hp/astrbot_plugin_hotpulse data/plugins/
docker restart astrbot
rm -rf /tmp/hp astrbot_plugin_hotpulse.zip
```

## 指令

| 指令 | 说明 |
|---|---|
| `/热点` | 默认 3 个平台 × 5 条 |
| `/热点 微博` | 单平台 |
| `/热点 微博 10` | 指定条数（1–20） |
| `/热点 微博、百度` | 多平台，顿号或逗号均可 |
| `/热点 搜 AI` | 全平台按关键词筛选，同一事件跨平台合并 |
| `/热点源` | 列出可用平台及别名 |

平台别名：`微博/wb`、`百度/bd`、`头条/tt`、`B站/哔哩哔哩/小破站/bili`、`36氪/36kr`、`知乎/zhihu`。

## 配置项

在 AstrBot WebUI 插件卡片 → 设置中调整：

| 项 | 默认 | 说明 |
|---|---|---|
| `default_sources` | `weibo,baidu,toutiao` | 不写平台时查哪几个 |
| `default_limit` | `5` | 每平台条数 |
| `cache_ttl` | `300` | 缓存秒数，不建议低于 60 |
| `zhihu_cookie` | 空 | 知乎必需，未填则该源不可用 |

## 已知限制（如实说明）

- **只有标题，没有正文**。热榜接口本身只返回标题 + 热度，正文在详情页且由 JS 渲染，HTML 里抓不到（实测：36氪 48 字、头条 14 字、微博 193 字）。
- **知乎需要 cookie**。2026 年起它强制校验登录态，无 cookie 返回 401，插件会显示该源不可用，不使用伪造登录态绕过。
- **热度值跨平台不可比**。微博是热度值、B站是播放量、36氪没有这个字段。

## 待办

- [x] `/热点监控 <关键词>`：命中即推送，同标题只推一次，夜间不抓
- [x] 插件日志改用 AstrBot logger，报错能在 WebUI 日志页看到
- [x] 失败结果加负缓存（30 秒），避免连续打上游
- [x] `/热点 搜 <关键词>`：全平台筛选 + 同义词组 + 跨源 bigram 合并
- [ ] 新上榜提醒：对比上一轮快照，只推「刚冲进榜」的条目

## 开发

```bash
# 代码风格检查（只需 ruff）
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m ruff format --check .

# 打包（务必用这个方式，PowerShell 的 Compress-Archive 会写出反斜杠路径，在 Linux 上解压会散成一堆文件）
.venv/Scripts/python -m zipfile -c dist/astrbot_plugin_hotpulse.zip astrbot_plugin_hotpulse
```
