# DeepSeek 缓存前缀 Hash 诊断补丁

这个补丁不是优化补丁，是“抓小偷”补丁：  
它只在 Zeabur 日志里打印哈希，不打印正文、不打印 Key。

## 用途

你现在 cached tokens 固定在 2.3K～2.8K。  
这个补丁用来判断：到底是哪一段 prompt 每轮在变，导致 DeepSeek 只能缓存前面一点。

## 使用方法

1. 用本包里的 `gateway.py` 覆盖当前项目里的 `gateway.py`
2. Zeabur 环境变量加：

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
4. 在同一个 RikkaHub 会话连续发 3 条短消息，比如：
   - 测试缓存一
   - 测试缓存二
   - 测试缓存三

## 看日志

Zeabur 运行日志里会出现类似：

```txt
🧪 [CacheProbe] shape=...
🧪 [CacheProbe] prefix_messages=1, hash=xxxx, chars=...
🧪 [CacheProbe] prefix_messages=2, hash=yyyy, chars=...
🧪 [CacheProbe] full_prefix_chars=2000, hash=...
🧪 [CacheProbe] full_prefix_chars=4000, hash=...
```

## 怎么判断

如果每轮 `full_prefix_chars=2000` hash 都一样，但 `full_prefix_chars=4000` 开始变，  
就说明 DeepSeek 只能命中约 2K～4K 前缀，很符合你现在的 2.3K cached。

如果 `prefix_messages=1` 每轮都变，说明 system prompt 第一条就在变。  
如果 `prefix_messages=1` 稳定，但 `prefix_messages=2` 开始变，说明第二条消息开始变。

## 重要

测完记得把：

```env
DEBUG_CACHE_HASH=false
```

或者删掉这个环境变量，避免日志太吵。
