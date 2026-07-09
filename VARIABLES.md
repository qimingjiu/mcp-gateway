# 📋 环境变量完整清单

本文档列出「通用 MCP 网关」**所有**支持的环境变量，按子系统分组。每项标注是否必填、默认值、来源代码与说明。

> - 标注 **【必填】**：缺失会导致对应功能无法启动。
> - 标注 **【可选】**：留空即自动禁用该功能，网关会优雅降级。
> - 兼容旧变量名（向后兼容）在「兼容别名」列注明。

---

## 目录
- [1. 基础部署](#1-基础部署)
- [2. 数据库 (Supabase)](#2-数据库-supabase)
- [3. 多模型 LLM](#3-多模型-llm)
- [4. 向量记忆 (Pinecone)](#4-向量记忆-pinecone)
- [5. 通讯渠道](#5-通讯渠道)
- [6. Google 集成](#6-google-集成)
- [7. 地图 / GPS](#7-地图--gps)
- [8. 多媒体生成](#8-多媒体生成)
- [9. 网页搜索](#9-网页搜索)
- [10. 云端笔记 (WebDAV)](#10-云端笔记-webdav)
- [11. NapCat QQ 接入](#11-napcat-qq-接入)
- [12. 后台心跳调度](#12-后台心跳调度)
- [13. 其他可选](#13-其他可选)
- [最小可运行配置示例](#最小可运行配置示例)

---

## 1. 基础部署

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `PORT` | ❌ | `10000` | 网关监听端口（Dockerfile `EXPOSE 10000`，云平台会自动注入） |
| `API_SECRET` | 🔴 强烈建议 | 空 | `/api/*` `/sse` `/messages` `/v1/*` 接口的安全密钥，**留空则不强制鉴权（危险）** |

### 1.1 🧠 智能体身份

控制对话的人格化行为：`AI_NAME` / `USER_NAME` / `AI_PERSONA` 在 gateway 智能体注入、heartbeat 心跳、napcat QQ 总结等多处复用；`USER_ID` / `CHAT_TAG` / `SUMMARY_THRESHOLD` 主要影响 `/v1/chat/completions` 的存库与自动总结。

> 上文注入 + 存库仅在同时配置了 `SUPABASE_URL` 时生效（否则 `/v1/*` 纯透传）。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `USER_NAME` | ❌ | `用户` | 用户称呼，注入到 system 提示与存库记录（如 `张三`） |
| `AI_NAME` | ❌ | `助手` | AI 角色称呼，注入到 system 提示与存库记录（如 `小橘`） |
| `USER_ID` | ❌ | `default` | 智能体模式下的用户标识（gateway.py 上文注入用，区分不同用户的对话流） |
| `AI_PERSONA` | ❌ | `你是一个通用智能助手。` | AI 人设完整文本，会拼接到 system 提示最前面 |
| `CHAT_TAG` | ❌ | `Web_Chat` | `/v1/chat/completions` 存库时给本轮对话打的标签（用于区分网页/TG/QQ 渠道） |
| `SUMMARY_THRESHOLD` | ❌ | `30` | 自动总结阈值：全渠道（网页/QQ/TG/邮件）对话流水累计达到该条数时，自动调用聊天模型（`main_chat`）生成第一人称阶段总结，存入 `Core_Cognition` 并归档旧记录。依赖 `CHAT_API_KEY`。 |

---

## 2. 数据库 (Supabase)

记忆、画像、提醒、记忆小屋、记账、设备定位等持久化所需。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `SUPABASE_URL` | ✅ | 空 | Supabase 项目 URL，如 `https://xxxxx.supabase.co` |
| `SUPABASE_KEY` | ✅ | 空 | Supabase service_role key（生产推荐）或 anon key |

> 建表 SQL 见 [`README.md` § 数据库表结构](README.md#-数据库表结构-supabase)。

---

## 3. 多模型 LLM

网关支持 4 类 LLM 角色：**主对话 `CHAT_*`**（必配，覆盖对话/总结/日记/`/v1/*` 代理全部场景）+ 3 类可选专用模型：**视觉 `VISION_*`**（TG 识图 + QQ OCR）、**语音识别 STT**（TG 语音转文字）、**语音合成 TTS**（TG 语音条回复）。最小化配置只需 `CHAT_*` 一组。

### 3.1 主对话模型 CHAT (日常聊天 + v1 代理主力)

可被数据库 `user_facts` 表 `key='llm_settings'` 的 JSON 动态覆盖（数据库配置优先于环境变量）。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `CHAT_API_KEY` | 🔴 强烈建议 | 空 | 主对话模型 API Key（不配则 LLM 相关功能全部失效） |
| `CHAT_BASE_URL` | ❌ | `https://api.minimaxi.com/v1` | 兼容任意 OpenAI 协议服务（OpenAI / DeepSeek / 通义 / 硅基流动 / 自建 vLLM） |
| `CHAT_MODEL_NAME` | ❌ | `abab6.5s-chat` | 模型名 |

> 💡 这个模型同时服务于：MCP 工具层、心跳日记/总结、`/v1/chat/completions` 代理。配齐这 3 项即可覆盖所有纯文字场景。

### 3.2 视觉模型 VISION (TG 识图 / QQ OCR)

TG 收到图片、QQ 收到带图消息时，调用视觉模型识别图片内容并拼到文字里给主对话模型。**必须用支持图片输入的模型**（如 `gpt-4o-mini` / `Qwen-VL`），聊天模型不支持。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `VISION_API_KEY` | ❌ | 空 | 视觉模型 API Key（不配则识图功能跳过） |
| `VISION_BASE_URL` | ❌ | `https://api.openai.com/v1` | 视觉模型服务地址 |
| `VISION_MODEL_NAME` | ❌ | `gpt-4o-mini` | 视觉模型名（须支持图片输入） |

> QQ OCR 额外受 `OCR_ENABLED`（默认 `false`）/ `OCR_MAX_IMAGES`（默认 `3`）控制，见 §11。

### 3.3 语音 STT/TTS (TG 语音消息收发)

TG 收到语音消息时：先用 STT 转文字 → 当普通文字处理 → 回复再用 TTS 合成语音条发回。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `SILICONFLOW_API_KEY` | ❌ | 空 | STT 用的 API Key（复用 §3.4 的硅基流动 key） |
| `STT_BASE_URL` | ❌ | `https://api.siliconflow.cn/v1` | STT 服务地址 |
| `STT_MODEL` | ❌ | `FunAudioLLM/SenseVoiceSmall` | 语音转文字模型 |
| `TTS_API_KEY` | ❌ | 空 | TTS 合成 API Key（不配则语音回复降级为纯文字） |
| `TTS_BASE_URL` | ❌ | `https://api.openai.com/v1` | TTS 服务地址 |
| `TTS_MODEL` | ❌ | `tts-1` | 语音合成模型 |
| `TTS_VOICE` | ❌ | `echo` | 音色 ID |

> 💡 TTS 合成语音条还需 `imageio-ffmpeg`（已列入 requirements.txt，可选）用于 mp3→ogg 转码；不装则自动降级为发文字。

### 3.4 向量嵌入 (SiliconFlow 硅基流动)

> ⚠️ 实际请求的是硅基流动 (SiliconFlow) 的 `embeddings` 接口，**不是火山引擎 Doubao**。`DOUBAO_EMBEDDING_EP` 仅作嵌入模型名变量保留。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `SILICONFLOW_API_KEY` | ❌ | 空 | 硅基流动 API Key |
| `DOUBAO_EMBEDDING_EP` | ❌ | 空 | 嵌入模型名，如 `BAAI/bge-m3` |

### 3.5 AI 人设

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `AI_PERSONA` | ❌ | `你是一个通用智能助手。` |

---

## 4. 向量记忆 (Pinecone)

长期语义记忆统一走 Pinecone：写入时用 SiliconFlow embedding（见 §3.4）向量化后 upsert，检索时同样向量化后做 top_k 近邻查询。需同时配置 `PINECONE_API_KEY` + `SILICONFLOW_API_KEY` + `DOUBAO_EMBEDDING_EP`。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `PINECONE_API_KEY` | ❌ | 空 | Pinecone 向量库 API Key |
| `PINECONE_INDEX_NAME` | ❌ | `notion-brain-v2` | Pinecone 索引名 |
| `PINECONE_USER_ID` | ❌ | `default` | 写入 Pinecone metadata 的用户隔离 ID |

---

## 5. 通讯渠道

### 5.1 Telegram

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `TG_BOT_TOKEN` | ❌ | 空 | Telegram Bot Token |
| `TG_CHAT_ID` | ❌ | 空 | 默认推送目标（私聊 ID） |

### 5.2 邮件 (Resend)

| 变量名 | 必填 | 默认值 | 兼容别名 |
|--------|:---:|--------|---------|
| `RESEND_API_KEY` | ❌ | 空 | — |
| `MY_EMAIL` | ❌ | 空 | `ADMIN_EMAIL` |
| `GMAIL_BRIDGE_URL` | ❌ | 空 | Gmail 桥接地址（供信箱巡视器轮询） |

---

## 6. Google 集成

Gmail 收发 & Google 日历。需要 Google OAuth 用户令牌。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `GOOGLE_USER_TOKEN_JSON` | ❌ | 空 | OAuth 用户令牌 JSON（序列化为单行字符串） |
| `GOOGLE_CALENDAR_ID` | ❌ | `primary` | 目标日历 ID |

> 最简单获取 `token.json` 的方式：本地用 Google 官方 [quickstart](https://developers.google.com/gmail/api/quickstart/python) 跑一次。

---

## 7. 地图 / GPS

高德地图服务，周边探索 / 天气。设备定位数据通过 Supabase 的 `device_data` 表写入。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `AMAP_API_KEY` | ❌ | 空 | [高德开放平台](https://lbs.amap.com) Web 服务 Key |

---

## 8. 多媒体生成

### 8.1 AI 音乐 / 翻唱 (Replicate)

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `REPLICATE_API_KEY` | ❌ | 空 | Replicate 官方 Token |
| `MUSIC_MODEL_VERSION` | ❌ | 空 | 原创音乐模型 version hash |
| `VOICE_MODEL_VERSION` | ❌ | 空 | RVC 翻唱音色模型 version hash |

### 8.2 HTML 转图片 (HCTI)

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `HCTI_API_ID` | ❌ | 空 |
| `HCTI_API_KEY` | ❌ | 空 |

---

## 9. 网页搜索

默认使用 DuckDuckGo 免费兜底（零配置）。配置 Tavily 后切换到高质量搜索。

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `TAVILY_API_KEY` | ❌ | 空 |

---

## 10. 云端笔记 (WebDAV)

支持坚果云等 WebDAV 服务。

| 变量名 | 必填 | 默认值 |
|--------|:---:|--------|
| `WEBDAV_URL` | ❌ | 空 |
| `WEBDAV_USER` | ❌ | 空 |
| `WEBDAV_PASSWORD` | ❌ | 空 |

---

## 11. NapCat QQ 接入

通过 [NapCat](https://github.com/NapNeko/NapCatQQ) 协议接入 QQ。本网关在 `/qq-ws` 暴露**反向 WS 端点**，等待本地 NapCat 主动连接。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `NAPCAT_WS_URL` | ❌ | 空 | NapCat WS 地址（仅供状态查询展示；实际接入走 `/qq-ws` 反向 WS） |
| `NAPCAT_HTTP_URL` | ❌ | 空 | NapCat HTTP 回调地址（供掉线通知等主动调用使用） |
| `NAPCAT_BOT_QQ` | ❌ | 空 | 机器人 QQ 号（用于群消息 @ 识别） |
| `NAPCAT_TARGET_USER` | ❌ | 空 | 限定响应的私聊用户 QQ（留空则所有人可聊） |
| `NAPCAT_NOTIFY_QQ` | ❌ | 空 | 掉线通知接收 QQ，多个用逗号分隔（通过 `NAPCAT_HTTP_URL` 发送） |
| `NAPCAT_NOTIFY_TG` | ❌ | 空 | 掉线时同时通知的 Telegram chat_id 列表，逗号分隔（依赖 `TG_BOT_TOKEN`） |
| `NAPCAT_ALLOWED_GROUPS` | ❌ | 空 | 允许响应的群号，逗号分隔（留空则不响应任何群） |
| `NAPCAT_RECONNECT_DELAY` | ❌ | `5` | 重连初始延迟（秒） |
| `NAPCAT_BACKOFF_FACTOR` | ❌ | `1.5` | 退避乘数 |
| `NAPCAT_MAX_DELAY` | ❌ | `60` | 最大重连延迟（秒） |
| `OCR_ENABLED` | ❌ | `false` | 📷 QQ 收到带图消息时是否自动 OCR 识图（依赖 `VISION_API_KEY`） |
| `OCR_MAX_IMAGES` | ❌ | `3` | 单条消息最多识别的图片数 |

---

## 12. 后台心跳调度

`heartbeat.py` 的主动问候、消息总结、日程播报相关。

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `HEARTBEAT_INTERVAL` | ❌ | `7200` | 主动问候间隔（秒） |
| `SUMMARIZE_INTERVAL` | ❌ | `1800` | 消息总结间隔（秒） |
| `SCHEDULE_MORNING_TIME` | ❌ | `07:30` | 日程早播时间 |
| `SCHEDULE_EVENING_TIME` | ❌ | `22:00` | 日程晚播时间 |
| `DIARY_TIME` | ❌ | `03:00` | 每日日记生成时间（24小时制）。到点自动拉取昨日全部对话流水，调用聊天模型（`main_chat`）生成第一人称"昨日回溯"日记，存入 Core_Cognition。启动时若发现昨日日记缺失会自动补写。依赖 `CHAT_API_KEY`。 |

> 💡 环境变量热同步：`heartbeat.py` 每 10 秒从数据库 `user_facts.sys_config` 读取一批键，热更新到 `os.environ`。默认同步的键见 `async_env_sync` 源码；可用 `SYNC_KEYS`（见第 13 节）追加。

---

## 13. 其他可选

| 变量名 | 必填 | 默认值 | 说明 |
|--------|:---:|--------|------|
| `SYNC_KEYS` | ❌ | 空 | 除默认热同步键外，额外需要从数据库 `sys_config` 同步的环境变量键，逗号分隔（见第 12 节） |

> 📌 本仓库代码实际读取的全部环境变量均已收录在本文档第 1~12 节。如果某教程提到本节以外、上文未列出的变量（如 `MUTE_*`、`MINIMAX_*`、`ZEABUR_*` 等），均属历史遗留，当前代码未实现。

---

## 最小可运行配置示例

只配置以下 3 项，网关即可正常启动并提供基础 MCP 工具：

```env
# 必填：基础 + 主对话模型（这一组覆盖 MCP 工具/心跳/日记/v1 代理 全部场景）
PORT=10000
API_SECRET=请改成你的随机密钥
CHAT_API_KEY=sk-xxxxxxxx
CHAT_BASE_URL=https://api.minimaxi.com/v1   # 用 OpenAI/DeepSeek/通义/硅基流动时改成对应地址
CHAT_MODEL_NAME=abab6.5s-chat

# 可选但推荐：数据库 + 推送
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_KEY=eyJhbGci...
TG_BOT_TOKEN=123456:ABC-DEF...
TG_CHAT_ID=123456789
AI_PERSONA=你是一个通用智能助手。
```

> 💡 其余所有变量均为可选，按需启用对应功能即可。未配置的功能会优雅降级而非报错。

---

## 变量生效方式

- **启动时读取**：所有变量在网关启动时读取。修改后需重启进程（或在云平台重新部署）才完整生效。
- **运行时热更新**：`heartbeat.py` 的 `async_env_sync` 协程每 10 秒从数据库 `user_facts.sys_config`（JSON）读取一批键，热写入 `os.environ`，无需重启即可即时生效（主要供 AI 人设、推送目标等高频调整项使用）。

> 📚 部署细节请参考 [DEPLOY_ZEABUR_新手版.md](DEPLOY_ZEABUR_新手版.md)，项目总览请参考 [README.md](README.md)。