# sync-server

手机模块的长连接接收端 + 消息/报价查看服务。纯 Python 标准库实现，零第三方依赖（wxgf 图片解码需要系统安装 ffmpeg）。

配合 WABTest Android 模块使用：模块监听微信群聊消息，经 TCP 长连接（HTTP 兜底）上报到本服务；本服务落盘消息、托管网页前端，并可选地对消息做报价提取分析。

## 功能

- **TCP 长连接接收**（NDJSON 协议，默认 8081）：hello 握手（可选 token 鉴权）、逐条 ack、30s 心跳、60s 重复投递去重
- **HTTP 兜底上报**（`POST /`，与 socket 同一消息格式）
- **网页前端**（默认 8080）：实时消息流、房间分组/未读、搜索高亮、图片灯箱、导出/清空、「报价」视图
- **报价 Agent**：文本/图片消息经 OpenAI 兼容视觉模型提取报价，按群分文件写 Markdown（`data/quotes/<roomId>.md`）+ 结构化 `quotes.jsonl` 供前端实时展示
- **token 鉴权**（可选）：一个 token 同时保护 socket、HTTP 上报、API 与图片

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

手机模块设置页：服务器地址 `http://<服务器IP>:8080`，Socket 端口 `8081`，Token 填 `<secret>`。浏览器打开后页面会要求输入 token（存 localStorage）。

> 公网建议用 Caddy/nginx 反代加 TLS，否则 token 与消息明文传输。socket 端口（8081）如需暴露，注意其承载的是裸 TCP NDJSON 协议，无法经 HTTP 反代套 TLS（可用 TCP 层反代/隧道）。

## CLI 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--port` | 8080 | HTTP（API + 前端）端口 |
| `--bind` | 127.0.0.1 | 绑定地址，公网用 0.0.0.0 |
| `--socket-port` | 8081 | 长连接端口，0 关闭 |
| `--data-dir` | ./data | 数据目录 |
| `--web-dir` | ./web | 前端目录 |
| `--agent-config` | ./agent.config.json | 报价 Agent 配置 |
| `--token` | `$SYNC_TOKEN` 或空 | 鉴权 token，空 = 不鉴权 |

## Socket 协议（NDJSON，每行一个 JSON）

| 方向 | 帧 | 说明 |
|---|---|---|
| 模块 → | `{"hello":"wabsync/1","token":"..."}` | 首帧必须握手；服务端有 token 时校验，失败回 `{"ok":false,"error":"unauthorized"}` 断开 |
| 模块 → | `{"ping":true}` | 心跳（30s），回 `{"ok":true}` |
| 模块 → | ChatMessage JSON | 与 HTTP POST body 相同，回 `{"ok":true,"seq":N}`；重复投递回 `{"ok":true,"dup":true}` |

ChatMessage 字段：`roomId / sender / content / type / createTime / msgSvrId`，图片另有 `imageBase64 / imagePath / imageKind / imageSize / cdnUrl / hdLength` 等。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/ping` | 健康检查（不鉴权） |
| GET | `/api/stats` | 消息数/图片数/`socketClients` |
| GET | `/api/rooms` | 群聊列表 |
| GET | `/api/messages?after=0&limit=1000&room=&q=` | 消息列表（响应不含 imageBase64） |
| GET | `/api/quotes?after=0&limit=500&room=` | 报价条目（按 qseq 增量） |
| POST | `/api/clear?room=` | 清空消息 |
| GET | `/images/<file>` | 图片文件 |
| POST | `/` | 模块 HTTP 兜底上报 |

启用 token 后，除 `/ping` 与静态页面外均需 `Authorization: Bearer <token>` 或 `?token=` 查询参数。

## 报价 Agent

配置 `agent.config.json`（参考 `agent.config.json.example`，已 gitignore）：

```json
{
  "enabled": true,
  "base_url": "https://your-openai-compatible-endpoint/v1",
  "api_key": "sk-...",
  "model": "your-vision-capable-model"
}
```

- 文本消息即时分析；图片延迟约 10s 等同消息原图到达后取最佳版本
- 模型需输出严格 JSON：`{"has_quote": false}` 或 `{"has_quote": true, "items": [{"项目","价格","单位","数量","备注"}]}`
- 输出：`data/quotes/<roomId>.md`（按日期分节表格）+ `data/quotes/quotes.jsonl`（前端报价视图数据源）
- 进度与日志：`data/quotes/state.json`、`data/quotes/agent.log`
- 历史回填（全量重建）：`python3 -m server.quote_agent --backfill`

## 目录结构

```
server/
  sync_server.py   主入口（HTTP + 装配 + 鉴权）
  relay.py         TCP 长连接监听
  store.py         消息存储（jsonl、图片、wxgf 解码、去重）+ QuoteLog
  quote_agent.py   报价提取 Agent
web/index.html     前端（消息流 + 报价视图）
data/              运行数据（gitignore）
```
