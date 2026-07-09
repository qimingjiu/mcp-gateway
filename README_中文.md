# 网关缓存友好补丁

这个补丁只改 `gateway.py` 里的 `_inject_context()`，目标是提高 DeepSeek prompt cache 命中：

- 稳定内容放前面：AI_PERSONA / user_facts / Core_Cognition
- 动态内容放后面：当前时间 / 沉默时长 / Pinecone 本轮检索 / 最近历史
- 默认不注入分钟级时间，只注入日期
- 默认最近历史从 10 条降为 6 条
- 默认 Pinecone 召回从 5 条降为 3 条

## 使用方法

把本包里的 `gateway.py` 覆盖你项目里的原 `gateway.py`，提交/重新部署 Zeabur。

## 推荐 Zeabur 环境变量

```env
CACHE_FRIENDLY_MODE=true
TIME_INJECTION=date
INJECT_SILENCE_HOURS=false
HISTORY_LIMIT=6
VECTOR_TOP_K=3
VECTOR_MEMORY_CHARS=1200
USER_FACT_LIMIT=40
USER_FACT_VALUE_CHARS=500
```

## 想更省 token / 更高缓存

```env
TIME_INJECTION=off
HISTORY_LIMIT=4
VECTOR_TOP_K=2
VECTOR_MEMORY_CHARS=800
```

## 想更强记忆召回

```env
TIME_INJECTION=date
HISTORY_LIMIT=8
VECTOR_TOP_K=5
VECTOR_MEMORY_CHARS=1600
```

## 查看是否生效

Zeabur 日志里应该出现：

```txt
🧠 [智能体/缓存友好] 注入完成：...
```

并显示：

```txt
time=date, history=6, vector_top_k=3
```
