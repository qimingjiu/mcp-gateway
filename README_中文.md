# 稳定回滚补丁：保留流式修复 + 尾部动态注入，关闭 CacheProbe 折腾

这个版本用于先把好荀网关恢复到稳定状态：

- 保留 `stream was reset: PROTOCOL_ERROR` 的修复
- 保留尾部动态注入
- 保留 DeepSeek `user_id`
- 不再打印 CacheProbe
- 不再强制改 `thinking=disabled/enabled`

## 使用方式

用本包里的 `gateway.py` 覆盖当前项目里的 `gateway.py`，重新部署 Zeabur。

## Zeabur 环境变量建议

先删掉或关闭这些诊断/实验变量：

```env
DEBUG_CACHE_HASH=false
# 删除 DEEPSEEK_THINKING，或者留空
```

保留/设置这些：

```env
DEEPSEEK_USER_ID=chacha

CACHE_FRIENDLY_MODE=true
TIME_INJECTION=hour
INJECT_SILENCE_HOURS=false
HISTORY_LIMIT=0
VECTOR_TOP_K=2
VECTOR_MEMORY_CHARS=800
USER_FACT_LIMIT=40
USER_FACT_VALUE_CHARS=500
```

如果回复太慢，可以临时降：

```env
VECTOR_TOP_K=1
VECTOR_MEMORY_CHARS=500
```

## 判断是否恢复

Zeabur 日志里应该看到：

```txt
[智能体/尾部动态注入] 注入完成
[DeepSeek] user_id=chacha，thinking 使用默认/前端设置
```

不应该再看到：

```txt
CacheProbe
thinking=disabled
```

## 下一步

先稳定聊天和流式输出。缓存问题先停一下。  
目前 1.3K～2.8K cached 属于 DeepSeek 自动缓存在这套动态记忆网关里的现实表现，不适合继续靠盲改补丁硬追。
