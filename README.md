# sync-server

手机模块的长连接接收端 + 消息/报价查看服务 + **飞书机器人（Agent 报价推送 + 问答 + 插件框架）**。纯 Python 标准库实现（wxgf 图片解码需要系统安装 ffmpeg）。

配合 WABTest Android 模块使用：模块监听微信群聊消息，经 TCP 长连接（HTTP 兜底）上报到本服务；本服务落盘消息、托管网页前端、提取报价，并经 **pi-agent SDK 桥** 把报价格式化后推送到飞书群机器人；飞书里 @机器人 可以问答报价数据，功能全部由插件注册，随时扩展。

```
WABTest 手机模块 ──TCP/HTTP──▶ sync-server ──报价 Agent(pi-agent SDK)──▶ 飞书群机器人
                                     ▲                                        ▲
                                   Web 前端                              飞书问答：@机器人
                                                                    （事件订阅 → 插件命令 / Agent 会话）
```

## 功能

- **TCP 长连接接收**（NDJSON 协议，默认 8081）：hello 握手（可选 token 鉴权）、逐条 ack、30s 心跳、60s 重复投递去重
- **HTTP 兜底上报**（`POST /`，与 socket 同一消息格式）
- **网页前端**（默认 8080）：实时消息流、房间分组/未读、搜索高亮、图片灯箱、导出/清空、「报价」视图
- **报价 Agent**：文本消息优先走 **pi-agent SDK 桥**（`pi_agent` 的 Agent 运行时），图片走 OpenAI 兼容视觉模型；按群分文件写 Markdown（`data/quotes/<roomId>.md`）+ 结构化 `quotes.jsonl`
- **飞书机器人**：
  - 报价推送：Agent 提取到报价 → LLM 格式化成群消息 → 推送到微信群机器人（webhook 加签）或应用消息
  - 问答：@机器人（群聊）或私聊，Agent 带工具（查报价/最近报价/群统计）回答；多轮会话有记忆、自动过期
  - 事件订阅 webhook（`POST /feishu/event`）：url_verification、Verification Token 校验、可选 Encrypt Key 的签名校验 + AES-256-CBC 解密
- **插件框架**：`plugins/*.py` 一个文件一个插件，注册命令 / 消息钩子 / 报价钩子；内置 help、quotes、rooms、stats、echo 五个示例
- **token 鉴权**（可选）：一个 token 同时保护 socket、HTTP 上报、API 与图片（飞书事件回调用飞书自己的校验，不经过此 token）

## 快速开始

### 本机模式（adb reverse，无鉴权）

```bash
python3 -m server.sync_server
adb reverse tcp:8080 tcp:8080
adb reverse tcp:8081 tcp:8081
```

手机模块设置页：服务器地址 `http://127.0.0.1:8080`，Socket 端口 `8081`，Token 留空。浏览器打开 `http://127.0.0.1:8080`。

### 公网模式（token 鉴权）

```bash
python3 -m server.sync_server --bind 0.0.0.0 --port 8080 --socket-port 8081 --token <secret>
# 或用环境变量：SYNC_TOKEN=<secret> python3 -m server.sync_server --bind 0.0.0.0
```

> 公网建议用 Caddy/nginx 反代加 TLS。飞书事件回调地址可反代到 `/feishu/event`（不带 SYNC_TOKEN，由飞书签名/Token 校验保护）。
> socket 端口（8081）如需暴露，注意其承载的是裸 TCP NDJSON 协议，无法经 HTTP 反代套 TLS（可用 TCP 层反代/隧道）。

## 飞书机器人接入（5 步）

1. **建应用**：飞书开放平台 `open.feishu.cn` → 开发者后台 → 创建企业自建应用 → 打开「机器人」能力。
2. **开权限**（权限管理，发布版本后生效）：
   - `im:message`（获取与发送单聊、群组消息）
   - `im:message.p2p_msg:readonly`（接收用户私聊机器人消息）
   - `im:message.group_at_msg:readonly`（接收群里 @机器人 的消息）
3. **事件订阅**：订阅 `接收消息 im.message.receive_v1`。
   - 推荐「使用长连接接收事件」：应用开通长连接能力后，服务端 `event_mode: "long_conn"`，用官方 SDK（`pip install lark-oapi`）的 WebSocket 长连接收事件，不需要公网 HTTPS 回调地址。
   - 备选「将事件发送至开发者服务器」（webhook）：回调地址填 `https://你的域名/feishu/event`，`event_mode: "webhook"`；公网建议配 Encrypt Key（`pip install pycryptodome`）并用 Caddy/nginx 反代 TLS。Verification Token 填「加密策略」页里的值。
4. **报价推送目标**：群里添加「自定义机器人」（群设置 → 群机器人 → 添加机器人 → 自定义机器人），复制 webhook 地址（可开加签），填入 `feishu.config.json` 的 `push.webhook_url / webhook_secret`；或者用带你应用机器人的群 chat_id + `push.mode=app`。
5. **配置并重启**：

```bash
cp feishu.config.json.example feishu.config.json   # 按注释填写，enabled: true
python3 -m server.sync_server --feishu-config ./feishu.config.json
```

验证：群里发一条含报价的消息 → 手机模块上报 → Agent 提取 → 自定义机器人推送；群里 `@机器人 /help` 或私聊机器人提问。

## 钉钉机器人接入

1. **群推送（自定义机器人）**：钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义 → 复制 webhook 里的 `access_token`；若开了加签，复制加签密钥填 `webhook.secret`。报价推送走这个通道。
2. **收消息/问答（企业内部应用机器人，可选）**：钉钉开发者后台创建企业内部应用，开通机器人能力，复制 `AppKey/AppSecret` 填 `app.client_id / client_secret`；`mode: "stream"` 用官方 `dingtalk-stream` SDK 长连接（无需公网地址），`mode: "webhook"` 只推送不收消息。
3. **配置并重启**：

```bash
cp dingtalk.config.json.example dingtalk.config.json   # enabled: true
pip install dingtalk-stream                             # 仅 stream 模式需要
systemctl restart wabtest-sync
```

验证：`GET /api/dingtalk` 看 `push_configured / ws_connected`；群机器人收到报价推送，`mode=stream` 时群里 @机器人 `/help` 可问答（Agent 与飞书共用同一套 bridge）。

## Agent（pi-agent SDK）

报价格式化与飞书问答统一走 `server/pi_bridge.py` 桥，可选两种后端（`agent.config.json` 的 `provider`）：

| provider | 说明 |
|---|---|
| `auto`（默认） | 已装 `pi-agent`（PyPI 包，Pi 的 Python 版）就走 pi-agent SDK 的 Agent 运行时，否则降级纯标准库 HTTP |
| `pi` | 强制 pi-agent SDK，未安装则报错停用 |
| `http` | 纯标准库 `urllib` 调 OpenAI 兼容 `/chat/completions` |
| `mock` | pi-agent SDK 内置 mock provider（不联网，测试/演示用） |

### 消息字段

模块上行 `ChatMessage` JSON（HTTP 与 socket 同一格式）：

```json
{
  "roomId": "49945523870@chatroom",
  "roomName": "富利铜业报价群",
  "sender": "wxid_fosies3z4gse22",
  "senderName": "陈工",
  "content": "无氧杆 2609合约+500",
  "type": 1,
  "createTime": 1700000000000,
  "msgSvrId": 1234567890123456789,
  "atMe": false,
  "atAll": false,
  "atUserList": "wxid_a,wxid_b"
}
```

`atMe` 为真表示这条群消息 @ 了模块所在微信号（含 @所有人）。配置了 Agent（`agent.config.json`）后，服务端只对「群聊 + `atMe` + 文本」调用 Agent 生成回复，经下行队列发回该群；私聊和没被 @ 的群消息不会自动回复。

`roomName` / `senderName` 是模块解析出的显示名（模块设置页「附带群名称/发送者名称」控制，模块版本 ≥ 1.1.0.r70），解析不出时为 `null`。服务端所有展示位置（网页消息行/房间列表/报价表、报价 md 标题与表格、`/rooms` `/quotes` 插件、飞书/钉钉推送与问答上下文）都优先用显示名，缺失时回退到 ID —— 因此**升级前的历史数据依然可用**，不会出现空白。

```json
{
  "enabled": true,
  "provider": "auto",
  "base_url": "https://your-openai-compatible-endpoint/v1",
  "api_key": "sk-...",
  "model": "your-model"
}
```

- SDK 安装（可选，建议用 venv）：`python3 -m venv .venv && .venv/bin/pip install pi-agent lark-oapi`，然后用 `.venv/bin/python -m server.sync_server` 启动
- 文本报价提取走桥（pi 后端 = SDK Agent 运行时）；**图片报价**仍走本服务的原生视觉调用（需视觉模型）
- 模型输出严格 JSON：`{"has_quote": false}` 或 `{"has_quote": true, "items": [{"项目","价格","单位","数量","备注"}]}`
- 飞书问答在多轮会话里注册了 3 个 Agent 工具：`search_quotes` / `latest_quotes` / `room_stats`，模型可自行调用查实时数据
- 输出：`data/quotes/<roomId>.md`、`data/quotes/quotes.jsonl`、`data/quotes/state.json`、`data/feishu/bot.log`、`data/feishu/push_state.json`

## 插件开发

`plugins/` 目录下一个文件一个插件，定义 `register(api)` 即可（照抄 `plugins/echo.py`）：

```python
# plugins/hello.py
def register(api):
    def hello(api, event, args):
        return "你好，" + (args or "世界") + "！"
    api.register_command("hello", hello, usage="/hello <名字>", description="打招呼")

    def recent_hook(api, event, text):          # 消息钩子：str 回复 / False 已处理 / None 继续
        if text.strip() in ("在吗",):
            return "我在，试试 /help"
    api.register_message_hook(recent_hook)

    def quote_watch(api, ctx):                  # 报价钩子：返回 False 可拦截默认群推送
        api.log(f"新报价 {ctx['room']} {len(ctx['rows'])} 条")
    api.register_quote_hook(quote_watch)
```

重启服务即生效（`/help` 可确认加载）。运行期资源：`api.store`（消息库）、`api.quotes`（报价库）、`api.bridge`（Agent 桥）、`api.feishu`（飞书客户端）、`api.config`（飞书配置）、`api.reply(text)`、`api.log(msg)`。

## CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8080 | HTTP（API + 前端）端口 |
| `--bind` | 127.0.0.1 | 绑定地址，公网用 0.0.0.0 |
| `--socket-port` | 8081 | 长连接端口，0 关闭 |
| `--data-dir` | ./data | 数据目录 |
| `--web-dir` | ./web | 前端目录 |
| `--agent-config` | ./agent.config.json | 报价 Agent 配置 |
| `--feishu-config` | ./feishu.config.json | 飞书机器人配置（缺失/未启用则跳过） |
| `--plugins-dir` | ./plugins | 机器人插件目录 |
| `--token` | `$SYNC_TOKEN` 或空 | 鉴权 token，空 = 不鉴权 |

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/ping` | 健康检查（不鉴权） |
| POST | `/feishu/event` | 飞书事件回调（走飞书签名/Token 校验，不走本服务 token） |
| GET | `/api/stats` | 消息数/图片数/`socketClients` |
| GET | `/api/feishu` | 飞书机器人状态（push 模式/问答/Agent 后端/已推送数） |
| GET | `/api/rooms` | 群聊列表 |
| GET | `/api/messages?after=0&limit=1000&room=&q=` | 消息列表（响应不含 imageBase64） |
| GET | `/api/quotes?after=0&limit=500&room=` | 报价条目（按 qseq 增量） |
| POST | `/api/clear?room=` | 清空消息 |
| GET | `/images/<file>` | 图片文件 |
| POST | `/` | 模块 HTTP 兜底上报 |

启用 token 后，除 `/ping`、`/feishu/event` 与静态页面外均需 `Authorization: Bearer <token>` 或 `?token=` 查询参数。

## 飞书事件安全说明

- 未配置 Encrypt Key：校验事件体里的 Verification Token（明文传输，仅建议内网调试用）
- 配置 Encrypt Key（推荐）：校验 `X-Lark-Signature`（sha256(timestamp+nonce+encrypt_key+原始body)），再 AES-256-CBC 解密（key=sha256(encrypt_key)）；同时事件按 `event_id` 幂等去重

## 开发与测试

```bash
.venv/bin/python -m unittest discover -s tests -v   # 28 个测试：桥/插件/飞书事件/推送链路，全部离线
python3 -m server.quote_agent --backfill           # 历史回填（全量重建报价）
```

## 目录结构

```
server/
  sync_server.py   主入口（HTTP + 装配 + 鉴权 + 飞书 webhook 路由）
  relay.py         TCP 长连接监听
  store.py         消息存储（jsonl、图片、wxgf 解码、去重）+ QuoteLog
  quote_agent.py   报价提取 Agent（文本走 bridge，图片走视觉模型；on_quote 钩子）
  pi_bridge.py     pi-agent SDK 桥：Agent 会话/工具/格式化，http/mock 降级
  feishu.py        飞书客户端 + 机器人（推送队列/问答会话/事件验证解密）
  bot_plugins.py   插件框架（PluginHost / PluginApi）
plugins/           内置插件：help、quotes、rooms、stats、echo（开发模板）
web/index.html     前端（消息流 + 报价视图）
tests/             离线测试套件
data/              运行数据（gitignore）
feishu.config.json      飞书配置（gitignore，样例见 .example）
agent.config.json       Agent 配置（gitignore，样例见 .example）
```