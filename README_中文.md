# DeepSeek 缓存命中对照补丁

这个补丁基于尾部动态注入版 `gateway.py`，新增两个实验能力：

1. 给 DeepSeek 官方 API 请求传 `user_id`
2. 通过环境变量临时开关 DeepSeek thinking mode

## 为什么要做

你现在的缓存命中大概卡在 2.7K tokens。  
DeepSeek 官方缓存是自动前缀缓存，不是 Claude 那种手动 cache_control。  
所以我们要确认卡住的原因是不是：

- 没传稳定 user_id，DeepSeek 侧 KVCache 隔离不稳定
- thinking mode 的 `reasoning_content` 没被前端完整带回，导致后续对话无法继续复用更长前缀

## 使用方法

把本包里的 `gateway.py` 覆盖项目原来的 `gateway.py`，重新部署 Zeabur。

## Zeabur 环境变量：第一轮测试

先加这些：

```env
DEEPSEEK_USER_ID=chacha
DEEPSEEK_THINKING=disabled

CACHE_FRIENDLY_MODE=true
TIME_INJECTION=hour
INJECT_SILENCE_HOURS=false
HISTORY_LIMIT=0
VECTOR_TOP_K=2
VECTOR_MEMORY_CHARS=800
USER_FACT_LIMIT=40
USER_FACT_VALUE_CHARS=500
```

然后在同一个 RikkaHub 会话连续聊 3～5 轮，看 cached tokens 是否突破 2.7K。

## 如果缓存明显上涨

说明 thinking mode / reasoning_content 很可能影响了缓存复用。  
之后你可以选择：

- 日常模型用 thinking disabled，速度和缓存更稳
- 需要推理时再开 thinking enabled

## 如果缓存还是卡在 2.7K

说明问题大概率不是 thinking，而是 RikkaHub/网关每轮拼出来的前缀在 2.7K 后发生变化。  
下一步就要打印 messages 前缀 hash 来定位到底哪一段变了。

## 恢复 thinking

把环境变量改成：

```env
DEEPSEEK_THINKING=enabled
```

或者直接删掉 `DEEPSEEK_THINKING`。
