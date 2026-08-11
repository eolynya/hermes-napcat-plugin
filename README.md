<div align="center">

# hermes-napcat-plugin

**让 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 通过 [NapCat](https://github.com/NapNeko/NapCatQQ)（QQ / OneBot 11）接入 QQ 的标准平台插件**

</div>

本仓库是 [shubyi/hermes-napcat](https://github.com/shubyi/hermes-napcat) 的 fork，将其重构成**标准的 Hermes platform 插件**（`kind: platform`），通过 `hermes plugins install` 一键安装，**零源码 patch**。在任意 QQ 群或私聊中与你的 AI 助手对话，支持完整的群管理功能。

```
QQ客户端 ──── NapCat ──WS──▶ hermes-napcat-plugin ──▶ Hermes（大模型）
                              │                            │
                              └─────HTTP API ◀─────────────┘
                             (port 18801)          (port 18800)
```

---

## 特性

- **群聊 & 私聊** — 群内 @提及触发；私聊直接对话
- **共享群会话** — 整个群共享一份上下文，自动为发送者加前缀
- **管理员系统** — 将管理命令限制在可配置的 QQ 号列表
- **好友申请自动处理** — `friend_policy`：`open`（秒通过）/ `allowlist`（白名单）/ `disabled`
- **回复引用开关** — `reply_to_mode`：`off` / `first` / `all`
- **QQ 输入状态（typing）** — 通过 `set_input_status` 显示「对方正在输入」气泡
- **媒体发送** — 图片 / 语音 / 视频 / 文件
- **完整 `qq_*` 工具集** — 48 个 OneBot 11 操作工具（发消息、群管理、OCR、翻译等）
- **核心工具聚合** — 插件启动时自动把 `hermes-cli`（56 个核心工具：terminal/file/web/memory…）聚合进 `hermes-napcat` 工具集，QQ agent「开箱即有手」
- **自带 QQ skill** — 打包 `qq-napcat` 技能，安装时自动放入 flat 树（`~/.hermes/skills/qq/`），进 `<available_skills>` 索引自动加载

---

## 安装

### 方式一：作为 Hermes 平台插件（推荐）

```bash
hermes plugins install eolynya/hermes-napcat-plugin --enable
hermes gateway restart
```

插件从 git 仓库根部加载，`plugin.yaml`（`kind: platform`）+ 根 `__init__.py`（`register(ctx)` 入口）即插件本体，不依赖 pip / pyproject。

### 方式二：legacy 源码 patch

仓库仍保留 `installer.py` / `cli.py` / `napcat.py`（legacy 的 `hermes-napcat install` 源码 patch 模式），但推荐改用上面的插件方式。

---

## 配置

在 `~/.hermes/config.yaml` 中启用并配置 napcat 平台：

```yaml
platforms:
  napcat:
    enabled: true
    extra:
      http_api: "http://127.0.0.1:18801"   # NapCat OneBot HTTP API
      access_token: ""
      self_id: "<你的QQ号>"
      ws_port: 18800                        # NapCat 反向 WS
      dm_policy: "open"                     # open | allowlist | disabled
      allow_from: []                        # 私聊白名单 QQ 号
      group_policy: "open"                  # open | allowlist | disabled
      group_allow_from: []
      friend_policy: "open"                 # open | allowlist | disabled（好友申请）
      admins: []                            # 可调用管理工具的白名单 QQ
      reply_to_mode: "off"                  # off | first | all
      media_max_mb: 5
```

### 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `http_api` | `""`（必填） | NapCat OneBot HTTP API 基址，如 `http://127.0.0.1:18801` |
| `access_token` | `""` | OneBot access_token（NapCat 未开启则留空） |
| `self_id` | `""` | bot 的 QQ 号；留空或占位符（`YOUR_QQ_NUMBER`）时启动时经 HTTP probe 自动填充 |
| `ws_port` | `18800` | 反向 WebSocket 监听端口（NapCat 的 `websocketClients[0].url` 指向这里） |
| `dm_policy` | `"allowlist"` | 私聊策略：`allowlist` 仅白名单可聊 / `open` 所有人 / `disabled` 关闭私聊 |
| `allow_from` | `[]` | 私聊白名单 QQ 号列表（配合 `dm_policy: allowlist`） |
| `group_policy` | `"open"` | 群聊策略：`open` 所有群 / `allowlist` 仅白名单群 / `disabled` 关闭群聊 |
| `group_allow_from` | `[]` | 群白名单（群号列表，配合 `group_policy: allowlist`） |
| `friend_policy` | `"open"` | 好友申请处理：`open` 秒通过 / `allowlist` 仅通过白名单（`admins`+`allow_from`）/ `disabled` 不自动处理 |
| `admins` | `[]` | 管理员 QQ 号列表（可调用踢人/禁言等管理工具） |
| `reply_to_mode` | `"off"` | 发送时是否附加「引用回复」：`off` 直接发新消息 / `first` 仅回复引用时 / `all` 总是引用 |
| `media_max_mb` | `5` | 图片/语音大小上限（MB），超限降级为保留远程 URL |

> 白名单字段（`allow_from`/`admins`/`group_allow_from`）同时兼容三种写法：真 YAML list、逗号字符串 `"123,456"`、JSON 字符串 `'["123","456"]'`（插件内部统一解析）。

> **工具集**：插件已自动聚合核心工具，`platform_toolsets.napcat` 只需 `["hermes-napcat"]` 即可获得 48 个 qq_* + 56 个核心工具；也可显式列出各工具集。

**NapCat 容器**（必须 `--network host`，否则容器内 `127.0.0.1` 指容器自己，连不上宿主）：

```bash
docker run -d --name napcat --restart=always --network host \
  -e NAPCAT_UID=0 -e NAPCAT_GID=0 -e ACCOUNT=<QQ号> \
  -e WS_URLS='["ws://127.0.0.1:18800"]' -e WSR_ENABLE=true \
  -e MESSAGE_POST_FORMAT='array' \
  -v /opt/napcat/config:/app/napcat/config \
  -v /opt/napcat/.config:/app/.config/QQ \
  -v /opt/napcat/logs:/app/napcat/logs \
  mlikiowa/napcat-docker:latest
```

启动后需在 WebUI 配置 `network.websocketClients[0].url = ws://127.0.0.1:18800` 与 `network.httpServers[0] = {port 18801, host 127.0.0.1}`，然后扫码登录。

---

## 插件实现的几个关键点

- **相对导入**：根 `__init__.py` 必须用 `from .hermes_napcat import ...` 相对导入。`_load_directory_module` 不把插件目录加进 `sys.path`，绝对导入会命中 site-packages 旧版（`connect()` 缺 `is_reconnect`）导致连接失败。
- **工具补注册**：`qq_tool.py` 用 `registry.register(toolset="napcat")` 模块级注册绕过了 `ctx.register_tool`，插件管理器看不到 `napcat` 工具集。`register()` 里遍历 `registry.get_tool_names_for_toolset("napcat")` 逐个 `ctx.register_tool` 补注册，让 48 个 qq_* 工具真正装载进 QQ agent。
- **核心工具聚合**：`create_custom_toolset` 重定义 `hermes-napcat`，`includes` 加 `hermes-cli`，进程内生效、不改核心源码、`hermes update` 不覆盖。
- **skill 自动加载**：`register_skill` 仅提供 `skill_view("hermes-napcat:qq-napcat")` 显式加载。要让 skill 进 `<available_skills>` 索引自动加载，需额外把 `SKILL.md` 拷进 `~/.hermes/skills/qq/`（目标已存在则不覆盖，保护本地增强版）。

---

## 与上游的区别

本 fork 相对 [shubyi/hermes-napcat](https://github.com/shubyi/hermes-napcat)（上游最后一次提交 2026-04）的改动（已于 2026-08 实测验证）：

### 结构：pip 包 → 标准 Hermes 插件

| 项 | 上游 | 本 fork |
|---|---|---|
| 安装方式 | `pip install` + `hermes-napcat install`（源码 patch gateway/config.py、run.py、toolsets.py） | `hermes plugins install eolynya/hermes-napcat-plugin`（零源码 patch） |
| 加载 | 拷贝 adapter 到 `gateway/platforms/napcat.py` | 插件 `register_platform()` 注册进 `platform_registry` |
| 工具装载 | `qq_tool.py` 拷贝到 `tools/` | 插件内 `ctx.register_tool` 补注册 48 个 `qq_*` |

### 配置键（上游 10 个 → 本 fork 12 个）

本 fork **新增 2 个**配置键：
- `friend_policy` — 好友申请自动处理（`open`/`allowlist`/`disabled`），上游直接丢弃好友请求
- `reply_to_mode` — 回复引用开关（`off`/`first`/`all`），上游发送时无条件加引用段

其余 10 个键（`http_api`/`access_token`/`self_id`/`ws_port`/`dm_policy`/`allow_from`/`group_policy`/`group_allow_from`/`admins`/`media_max_mb`）与上游一致。

### 功能增强

| 功能 | 上游 | 本 fork |
|---|---|---|
| QQ 输入状态（typing） | `send_typing` 是 `pass` 空实现（注释误称「QQ 无 typing」） | 经 `set_input_status` 实现「对方正在输入」气泡（私聊） |
| 好友请求 | 事件被丢弃，不处理 | `_handle_request()` 按 `friend_policy` 自动通过/拒绝/忽略 |
| 白名单解析 | 仅接受真 list | `_coerce_qid_set()` 兼容 list / 逗号串 / JSON 字符串 |
| 重连兼容 | `connect(self)` 缺 `is_reconnect` 参数 | `connect(self, is_reconnect=False)` 兼容 Hermes 重连 |
| Markdown 剥离 | 已有 `_strip_markdown` | 保留（QQ 不渲染 Markdown，发送前转纯文本） |

### 其他

- 修复插件根绝对导入被 site-packages 旧包劫持的 bug（`connect()` 缺 `is_reconnect` 导致连接失败）
- 聚合核心工具集（`hermes-cli` 56 个并入 `hermes-napcat`）使 QQ agent「开箱即有手」
- 自带 `qq-napcat` skill，安装时自动放进 flat 树供 `<available_skills>` 索引自动加载

## License

MIT（与上游 [shubyi/hermes-napcat](https://github.com/shubyi/hermes-napcat) 一致）