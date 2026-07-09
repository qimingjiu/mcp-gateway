# 尾部动态注入缓存补丁

这个补丁基于上一版 `stream_protocol_hotfix`，继续优化 DeepSeek prompt cache。

## 为什么要改

如果动态内容（当前时间、Pinecone 检索、Supabase 最近历史）和稳定人设写在同一个 system 里，DeepSeek 只能缓存到第一个变化点之前。你看到 `prompt_cache_hit_tokens` 卡在约 2.3K，就是因为前 2.3K 后面开始出现动态内容，后面的前缀每轮都变。

## 这版怎么改

- 稳定内容仍放最前面：AI_PERSONA、user_facts、Core_Cognition
- 动态内容移到最后一条 user 消息前：当前时间、沉默时长、Pinecone 本轮召回、Supabase 最近历史
- 保留上一版协议修复：删除 `connection: keep-alive`，避免 RikkaHub HTTP/2 SSE reset

## 推荐环境变量

```env
CACHE_FRIENDLY_MODE=true
TIME_INJECTION=hour
INJECT_SILENCE_HOURS=true
HISTORY_LIMIT=4
VECTOR_TOP_K=3
VECTOR_MEMORY_CHARS=1000
USER_FACT_LIMIT=40
USER_FACT_VALUE_CHARS=500
```

## 验证

Zeabur 日志应出现：

```txt
🧠 [智能体/尾部动态注入] 注入完成
```

DeepSeek 的缓存命中不一定立刻暴涨，因为它是 best-effort，会在几次请求后逐渐稳定。连续聊同一会话时，命中应不再固定死在 2.3K 左右，而是有机会命中：

- 稳定人设 + 用户画像
- 前端传来的已有聊天历史
- 前一轮已持久化的长前缀

如果 RikkaHub 没有把完整历史发给网关，而是只发当前一轮，那命中仍会主要停在稳定 system 部分。这不是补丁失效，是前端请求本身没有可复用的历史前缀。
