# 合并补丁：流式修复 + DeepSeek 缓存 Hash 诊断

这个包是合并版，避免“部署诊断补丁时把 stream hotfix 覆盖掉”。

包含：

1. SSE/HTTP2 修复
   - 删除 `connection: keep-alive`
   - 避免 RikkaHub/Zeabur/HTTP2 链路出现 `stream was reset: PROTOCOL_ERROR`

2. DeepSeek 缓存诊断
   - 传 `user_id`
   - 支持 `DEBUG_CACHE_HASH=true`
   - 打印 prefix hash，不打印正文、不打印 Key

3. 尾部动态注入
   - 稳定内容在前
   - 动态时间/向量召回/最近历史尽量靠后

## 使用方式

用本包里的 `gateway.py` 覆盖当前项目里的 `gateway.py`，重新部署 Zeabur。

## 推荐环境变量

```env
DEBUG_CACHE_HASH=true
DEEPSEEK_USER_ID=chacha
DEEPSEEK_THINKING=enabled

CACHE_FRIENDLY_MODE=true
TIME_INJECTION=hour
INJECT_SILENCE_HOURS=false
HISTORY_LIMIT=0
VECTOR_TOP_K=2
VECTOR_MEMORY_CHARS=800
USER_FACT_LIMIT=40
USER_FACT_VALUE_CHARS=500
```

## 怎么看日志

你截图里那些：

```txt
GET /rest/v1/user_facts?select=value&key=eq.sys_config
GET /rest/v1/reminders?...
```

不是缓存诊断日志，只是网关后台定时任务在读 Supabase。

真正要找的是：

```txt
🧪 [CacheProbe] shape=...
🧪 [CacheProbe] prefix_messages=1, hash=...
🧪 [CacheProbe] full_prefix_chars=2000, hash=...
```

在 Zeabur 日志搜索：

```txt
CacheProbe
```

或者发完消息后往聊天请求那一段附近翻。

## 测完记得关

```env
DEBUG_CACHE_HASH=false
```

或删除这个变量。
