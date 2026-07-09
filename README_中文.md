# RikkaHub stream was reset: PROTOCOL_ERROR 热修补丁

这个补丁基于“缓存友好版 gateway.py”，额外修复一个 SSE/HTTP2 兼容问题。

## 修了什么

删除网关返回给前端时的响应头：

```txt
connection: keep-alive
```

原因：`Connection` 是 HTTP/1.1 hop-by-hop header，HTTP/2 不允许这个头。  
RikkaHub/Android/代理链路如果走 HTTP/2，可能会直接把流重置，报：

```txt
stream was reset: PROTOCOL_ERROR
```

## 额外小优化

把上游流式读取从：

```python
resp.iter_lines()
```

改成：

```python
resp.iter_lines(chunk_size=1, decode_unicode=True)
```

这样流会更及时一点，也避免 decode 重复处理。

## 使用方式

1. 用本包里的 `gateway.py` 覆盖项目原来的 `gateway.py`
2. 重新部署 Zeabur
3. RikkaHub 重新发一条消息测试

## 环境变量建议

```env
CACHE_FRIENDLY_MODE=true
TIME_INJECTION=hour
INJECT_SILENCE_HOURS=true
HISTORY_LIMIT=6
VECTOR_TOP_K=3
VECTOR_MEMORY_CHARS=1200
USER_FACT_LIMIT=40
USER_FACT_VALUE_CHARS=500
```

如果仍然偶发断流，先测试：

```env
VECTOR_TOP_K=2
HISTORY_LIMIT=4
```

确认是不是 prompt 太长或上游推理太久导致代理断开。
