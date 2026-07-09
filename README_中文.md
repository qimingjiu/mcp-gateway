# 合并补丁 v2：修复 CacheProbe 不出现

这个版本修正了上一版的问题：诊断函数定义了，但没有被调用，所以 Zeabur 搜不到 `CacheProbe`。

## 使用方式

1. 用本包里的 `gateway.py` 覆盖项目里的 `gateway.py`
2. Zeabur 环境变量确认：

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

3. Redeploy
4. 发一条消息
5. 在运行日志里搜：

```txt
CacheProbe
```

## 正常应该看到

```txt
🧪 [CacheProbe] DEBUG_CACHE_HASH=true，准备打印本轮请求前缀 hash
🧪 [CacheProbe] shape=...
🧪 [CacheProbe] prefix_messages=1, hash=...
🧪 [CacheProbe] full_prefix_chars=2000, hash=...
```

## 测完关闭

把 `DEBUG_CACHE_HASH` 改成 `false` 或删掉，避免日志太吵。
