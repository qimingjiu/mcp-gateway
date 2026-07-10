"""
通用 ASGI 网关中间件 (Generic ASGI Gateway Middleware)
======================================================
特性：
- 修正反代场景下的 Host 头
- 统一处理 CORS 预检
- 🔐 全局 API 安全拦截（校验 API_SECRET，对 /sse /messages /api/* 强制鉴权）
- 暴露一组管理 / 健康检查 / 配置接口
- 🧠 OpenAI 兼容代理 (/v1/chat/completions, /v1/models)：
    * 支持纯透传模式（无 Supabase 时）
    * 支持智能体模式（配了 Supabase + 可选 Pinecone 向量记忆）：
      自动注入上文（最近N条对话）、人设、用户画像、阶段总结、向量记忆
    * 流式收集 → 异步双写存库（不阻塞响应）
- 将业务请求转发给下游 MCP 应用

所有配置从环境变量读取，全部"个人化内容"已变量化，无任何硬编码。
未配置的功能会优雅降级，保证最小配置（仅 CHAT_API_KEY）即可运行。
"""

import os
import re
import json
import asyncio
import time
import datetime
import requests


# ==========================================
# 全局连接（延迟初始化，避免启动时无 Supabase 就崩）
# ==========================================
_supabase_client = None
_system_logs_buffer = []   # 简易日志缓存（用于 /api/logs）
_MAX_LOGS = 200
_pending_save_tasks = set()   # 持有后台存库 task 的强引用，防止被 GC 提前回收
KEEPALIVE_INTERVAL_SECONDS = 15
REASONING_FLUSH_INTERVAL_SECONDS = 0.2
REASONING_BUFFER_MAX_CHARS = 1024

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def _log(msg: str):
    """统一的日志打印 + 内存缓存（供 /api/logs 查询）"""
    line = f"[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _system_logs_buffer.append(line)
    if len(_system_logs_buffer) > _MAX_LOGS:
        del _system_logs_buffer[: len(_system_logs_buffer) - _MAX_LOGS]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except Exception:
        value = default
    return max(min_value, min(value, max_value))


def _sanitize_reasoning_for_request(messages):
    for m in messages or []:
        if m.get("role") != "assistant":
            continue
        if isinstance(m.get("content"), str) and "<think>" in m["content"]:
            m["content"] = _THINK_BLOCK_RE.sub("", m["content"]).strip()
        if not m.get("tool_calls"):
            m.pop("reasoning_content", None)


def _ensure_stream_usage(req_data):
    # ⚠️ 不强制注入 include_usage！
    # 原因：强制加上之后 DeepSeek 在 [DONE] 前额外发一个 usage chunk，
    # 网关把它静默丢弃，但这段空档叠上思考链静默期，会让 Zeabur 边缘代理
    # 误判为空闲连接发 RST_STREAM → PROTOCOL_ERROR。
    # 只透传客户端原本就带的 stream_options，不主动添加。
    pass


def _log_tool_diagnostic(req_data):
    messages = req_data.get("messages") or []
    tools = req_data.get("tools") or []
    tools_count = len(tools) if isinstance(tools, list) else 0
    assistant_tool_call_messages = 0
    tool_result_messages = 0
    tool_call_reasoning_present = False
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            assistant_tool_call_messages += 1
            if bool(m.get("reasoning_content")):
                tool_call_reasoning_present = True
        elif role == "tool":
            tool_result_messages += 1
    _log(
        "[tool-diagnostic] "
        f"tools_count={tools_count}; "
        f"assistant_tool_call_messages={assistant_tool_call_messages}; "
        f"tool_result_messages={tool_result_messages}; "
        f"tool_call_reasoning_present={str(tool_call_reasoning_present).lower()}"
    )


def _append_dynamic_context_to_last_user(messages, dynamic_part):
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") != "user":
            continue
        original = messages[i].get("content", "")
        if isinstance(original, list):
            messages[i]["content"] = original + [
                {"type": "text", "text": "\n\n---\n" + dynamic_part}
            ]
        else:
            messages[i]["content"] = str(original) + "\n\n---\n" + dynamic_part
        return True
    return False


def _merge_tool_call_delta(tool_calls_dict, tc):
    idx = tc.get("index", 0)
    if idx not in tool_calls_dict:
        tool_calls_dict[idx] = json.loads(json.dumps(tc, ensure_ascii=False))
        return
    existing = tool_calls_dict[idx]
    if tc.get("id"):
        existing["id"] = tc["id"]
    if tc.get("type"):
        existing["type"] = tc["type"]
    if tc.get("function"):
        existing.setdefault("function", {})
        fn = tc["function"]
        if fn.get("name"):
            existing["function"]["name"] = fn["name"]
        if fn.get("arguments"):
            existing["function"].setdefault("arguments", "")
            existing["function"]["arguments"] += fn["arguments"]


def _pure_reasoning_chunk_info(parsed_chunk):
    if not isinstance(parsed_chunk, dict):
        return None
    if "error" in parsed_chunk or parsed_chunk.get("usage") is not None:
        return None
    choices = parsed_chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return None
    reasoning = delta.get("reasoning_content")
    if not isinstance(reasoning, str) or not reasoning:
        return None
    if delta.get("content") not in (None, ""):
        return None
    if delta.get("tool_calls") not in (None, []):
        return None
    if choice.get("finish_reason") is not None:
        return None
    return {
        "reasoning_content": reasoning,
        "index": choice.get("index", 0),
        "identity": (
            parsed_chunk.get("id"),
            parsed_chunk.get("object"),
            parsed_chunk.get("created"),
            parsed_chunk.get("model"),
            choice.get("index", 0),
        ),
        "template": {key: parsed_chunk.get(key) for key in ("id", "object", "created", "model")},
    }


def _pure_content_chunk_info(parsed_chunk):
    if not isinstance(parsed_chunk, dict):
        return None
    if "error" in parsed_chunk or parsed_chunk.get("usage") is not None:
        return None
    choices = parsed_chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        return None
    content = delta.get("content")
    if not isinstance(content, str) or not content:
        return None
    if delta.get("reasoning_content") not in (None, ""):
        return None
    if delta.get("tool_calls") not in (None, []):
        return None
    if choice.get("finish_reason") is not None:
        return None
    return {
        "content": content,
        "index": choice.get("index", 0),
        "identity": (
            parsed_chunk.get("id"),
            parsed_chunk.get("object"),
            parsed_chunk.get("created"),
            parsed_chunk.get("model"),
            choice.get("index", 0),
        ),
        "template": {key: parsed_chunk.get(key) for key in ("id", "object", "created", "model")},
    }


def _cache_usage_line(usage):
    if not isinstance(usage, dict):
        return None
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    cache_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
    cache_miss = int(usage.get("prompt_cache_miss_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    denom = cache_hit + cache_miss
    hit_rate = (cache_hit / denom * 100) if denom else 0.0
    return (
        "[cache-usage] "
        f"prompt_tokens={prompt_tokens}; "
        f"cache_hit={cache_hit}; "
        f"cache_miss={cache_miss}; "
        f"hit_rate={hit_rate:.1f}%; "
        f"completion_tokens={completion_tokens}"
    )


def _get_supabase():
    """获取 Supabase 客户端（复用 server.py 已初始化的实例，避免重复建连）"""
    global _supabase_client
    if _supabase_client is not None:
        return _supabase_client
    try:
        import server
        if getattr(server, "supabase", None) is not None:
            _supabase_client = server.supabase
            _log(f"✅ 复用 server.py 的 Supabase 客户端: {(os.environ.get('SUPABASE_URL') or '')[:30]}...")
            return _supabase_client
    except Exception as e:
        _log(f"⚠️ 复用 server.supabase 失败，回退到自建: {e}")
    # 回退：本模块自建（仅在 server.py 未成功初始化时）
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    if not url or not key:
        return None
    try:
        from supabase import create_client
        _supabase_client = create_client(url, key)
        _log(f"✅ Supabase 已连接(自建): {url[:30]}...")
    except Exception as e:
        _log(f"❌ Supabase 连接失败: {e}")
        _supabase_client = None
    return _supabase_client


class HostFixMiddleware:
    """ASGI 中间件：路由分发 + OpenAI 兼容代理 + MCP 下游转发"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # ---------- NapCat 反向 WebSocket 端点 ----------
        if scope["type"] == "websocket" and scope["path"] == "/qq-ws":
            try:
                import napcat
                await napcat.handle_napcat_ws(scope, receive, send)
            except Exception as e:
                _log(f"❌ NapCat WS 处理异常: {e}")
            return

        # 非 HTTP 类型直接透传给下游
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # ---------- 根路径：返回占位（或前端 index.html）----------
        if scope["path"] == "/":
            html = "<h1>🚪 MCP Gateway</h1><p>Endpoints: <code>/health</code> <code>/sse</code> <code>/v1/chat/completions</code></p>"
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/html; charset=utf-8")]})
            await send({"type": "http.response.body", "body": html.encode("utf-8")})
            return

        # ---------- 健康检查 ----------
        if scope["path"] == "/health":
            await _send_json_resp(send, 200, {"status": "ok", "service": "generic-mcp-gateway"})
            return

        # ---------- 🆕 OpenAI 兼容代理 (/v1/*) ----------
        if scope["path"].startswith("/v1/"):
            if scope["method"] == "OPTIONS":
                await _send_cors_preflight(send)
                return
            await self._handle_openai_proxy(scope, receive, send)
            return

        # 🛡️ 全局 API 安全拦截 (涵盖 /api/* /sse /messages)
        if (scope["path"].startswith("/api/") or scope["path"].startswith("/sse") or scope["path"].startswith("/messages")) and scope["method"] != "OPTIONS":
            if not await _check_api_secret(scope, send):
                return

        # ---------- CORS 预检 ----------
        if scope["method"] == "OPTIONS":
            await _send_cors_preflight(send)
            return

        # ---------- 运行日志接口 ----------
        if scope["path"] == "/api/logs":
            await self._handle_logs(send)
            return

        # ---------- 兜底其余请求 (Host Fix → 下游 MCP) ----------
        headers = dict(scope.get("headers", []))
        headers[b"host"] = b"localhost:8000"
        scope["headers"] = list(headers.items())
        await self.app(scope, receive, send)

    # ------------------------------------------
    # 🧠 OpenAI 兼容代理（核心）
    # ------------------------------------------

    async def _handle_openai_proxy(self, scope, receive, send):
        """把 /v1/* 请求转发到上游模型。配了 Supabase 时自动开启智能体模式。"""
        path = scope["path"]
        method = scope["method"]

        # 可选鉴权
        api_secret = os.environ.get("API_SECRET", "").strip()
        if api_secret:
            if not await _check_api_secret(scope, send):
                return

        # ---- /v1/models ----
        if path == "/v1/models" and method == "GET":
            default_model = os.environ.get("CHAT_MODEL_NAME", "abab6.5s-chat")
            models = [{"id": default_model, "object": "model", "created": int(time.time()), "owned_by": "mcp-gateway"}]
            await _send_json_resp(send, 200, {"object": "list", "data": models})
            return

        # ---- /v1/chat/completions ----
        if path == "/v1/chat/completions" and method == "POST":
            await self._handle_chat(scope, receive, send)
            return

        await _send_json_resp(send, 404, {"error": {"message": f"Unknown endpoint: {path}"}})

    async def _handle_chat(self, scope, receive, send):
        """聊天核心：透传 + 可选上文注入 + 流式收集双写"""
        # 读请求体
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break

        try:
            req_data = json.loads(body.decode("utf-8"))
        except Exception:
            await _send_json_resp(send, 400, {"error": {"message": "Invalid JSON body"}})
            return

        client_wants_stream = bool(req_data.get("stream", False))

        # 解析上游配置：统一用 CHAT_*（主对话模型），与 MCP 工具层一致
        upstream_base = os.environ.get("CHAT_BASE_URL", "https://api.minimaxi.com/v1").strip()
        upstream_key = os.environ.get("CHAT_API_KEY", "").strip()
        default_model = os.environ.get("CHAT_MODEL_NAME", "abab6.5s-chat")

        if not upstream_key:
            await _send_json_resp(send, 500, {"error": {"message": "Server 未配置 CHAT_API_KEY"}})
            return

        if not req_data.get("model"):
            req_data["model"] = default_model

        # 构造上游 URL（兼容用户填或不填 /v1 后缀）
        base = upstream_base.rstrip("/") or "https://api.openai.com/v1"
        if not base.endswith("/v1"):
            upstream_url = f"{base}/v1/chat/completions"
        else:
            upstream_url = f"{base}/chat/completions"

        # ==========================================
        # 🧠 智能体模式：注入上文/人设/记忆（仅当配了 Supabase 时启用）
        # ==========================================
        sb = _get_supabase()
        user_msg = "" 
        for m in reversed(req_data.get("messages", [])):
            if m.get("role") == "user":
                user_msg = str(m.get("content", ""))
                break

        # 普通历史不回传思维链；但 assistant.tool_calls 里的 reasoning_content
        # 是 DeepSeek thinking + tools 协议的一部分，必须保留给后续 tool 请求。
        _sanitize_reasoning_for_request(req_data.get("messages", []))

        if sb and user_msg:
            try:
                await self._inject_context(req_data, sb, user_msg)
            except Exception as e:
                _log(f"⚠️ 上文注入失败（已降级为透传）: {e}")
        else:
            if sb:
                _log("➡️ [透传] 无 user 消息或无 Supabase，直接转发")

        req_data["stream"] = client_wants_stream
        if client_wants_stream:
            _ensure_stream_usage(req_data)
        if req_data.get("tools"):
            req_data["tool_choice"] = "auto"
        _log_tool_diagnostic(req_data)

        # 构造请求头（修复 python-requests UA 被拦截 + 透传客户端头）
        client_headers = {k.decode("utf-8", "ignore").lower(): v.decode("utf-8", "ignore") for k, v in scope.get("headers", [])}
        client_ua = client_headers.get("user-agent", "")
        fwd_headers = {
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json",
            "User-Agent": client_ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/event-stream" if client_wants_stream else "application/json",
        }
        for h in ("accept-language", "x-requested-with"):
            if h in client_headers:
                fwd_headers[h] = client_headers[h]

        _log(f"➡️ [转发] POST {upstream_url} | model={req_data.get('model')}")

        if not client_wants_stream:
            try:
                resp = await asyncio.to_thread(
                    requests.post,
                    upstream_url,
                    headers=fwd_headers,
                    json=req_data,
                    stream=False,
                    timeout=300,
                )
                status_code = resp.status_code
                try:
                    response_data = resp.json()
                except Exception:
                    response_data = json.loads(resp.text)
                if not isinstance(response_data, dict):
                    raise ValueError("Upstream returned a non-object JSON response")
            except Exception as e:
                _log(f"❌ 上游非流式报错: {e}")
                status_code = 502
                response_data = {"error": {"message": f"Upstream error: {e}"}}

            collected_content = ""
            collected_reasoning = ""
            tool_calls_dict = {}
            if status_code == 200:
                try:
                    message = (response_data.get("choices") or [])[0].get("message") or {}
                    collected_content = message.get("content") or ""
                    collected_reasoning = message.get("reasoning_content") or ""
                    tool_calls = message.get("tool_calls") or []
                    tool_calls_dict = {
                        index: json.loads(json.dumps(tool_call, ensure_ascii=False))
                        for index, tool_call in enumerate(tool_calls)
                    }
                except Exception:
                    pass

            response_body = json.dumps(response_data, ensure_ascii=False).encode("utf-8")
            try:
                await send({
                    "type": "http.response.start",
                    "status": status_code,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"access-control-allow-origin", b"*"),
                    ],
                })
                await send({
                    "type": "http.response.body",
                    "body": response_body,
                    "more_body": False,
                })
            except Exception:
                pass

            _log(
                "[stream-diagnostic] "
                "client_requested_stream=false; "
                "done_sent_with_end_stream=false; "
                "extra_final_body_sent=false"
            )

            if sb and user_msg and (collected_content or tool_calls_dict):
                task = asyncio.create_task(
                    self._save_conversation(sb, user_msg, collected_content, collected_reasoning, tool_calls_dict)
                )
                _pending_save_tasks.add(task)
                task.add_done_callback(_pending_save_tasks.discard)
            return

        # 启动响应流（通知客户端开始接收 SSE）
        asgi_send_count = 0
        client_disconnected = False
        send_error_type = "none"

        async def _send_asgi(message):
            nonlocal asgi_send_count, client_disconnected, send_error_type
            try:
                await send(message)
                asgi_send_count += 1
                return True
            except Exception as e:
                client_disconnected = True
                send_error_type = type(e).__name__
                return False

        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache, no-transform"),
                (b"x-accel-buffering", b"no"),
                (b"access-control-allow-origin", b"*"),
            ],
        })
        asgi_send_count += 1

        # 后台线程：读取上游流，喂给 asyncio.Queue；主协程只 await async_queue.get()
        import threading
        async_queue = asyncio.Queue()
        stream_end = object()
        loop = asyncio.get_running_loop()
        stream_started = time.perf_counter()

        def _enqueue(item):
            try:
                loop.call_soon_threadsafe(async_queue.put_nowait, item)
            except RuntimeError:
                pass

        def _stream_forward():
            try:
                with requests.post(upstream_url, headers=fwd_headers, json=req_data, stream=True, timeout=300) as resp:
                    if resp.status_code != 200:
                        _enqueue({"error": f"HTTP {resp.status_code}: {resp.text[:500]}"})
                        _enqueue(stream_end)
                        return
                    for line in resp.iter_lines(decode_unicode=True):
                        if line:
                            if isinstance(line, bytes):
                                line = line.decode("utf-8", "ignore")
                            _enqueue(line)
                _enqueue(stream_end)
            except Exception as e:
                _enqueue({"error": str(e)})
                _enqueue(stream_end)

        _log("[stream-transport] iter_lines_chunk_size=default")
        threading.Thread(target=_stream_forward, daemon=True).start()

        content_flush_interval_seconds = _env_int("CONTENT_FLUSH_MS", 80, 20, 500) / 1000.0
        content_buffer_max_chars = _env_int("CONTENT_BUFFER_CHARS", 512, 64, 4096)
        collected_content = ""
        collected_reasoning = ""
        tool_calls_dict = {}
        latest_usage = None
        upstream_first_sse_ms = None
        heartbeat_count = 0
        sse_line_count = 0
        received_done = False
        usage_chunk_seen = False
        usage_chunk_forwarded = False
        response_closed = False
        done_sent_with_end_stream = False
        extra_final_body_sent = False
        reasoning_raw_chunk_count = 0
        reasoning_forwarded_chunk_count = 0
        reasoning_chars = 0
        first_reasoning_ms = None
        last_reasoning_ms = None
        first_content_ms = None
        reasoning_to_content_gap_ms = None
        content_chunk_count = 0
        content_raw_chunk_count = 0
        content_forwarded_chunk_count = 0
        content_flush_count = 0
        content_chars = 0
        max_content_buffer_chars = 0
        pure_content_matched_count = 0
        first_content_forwarded = False
        tool_call_chunk_count = 0
        reasoning_flush_count = 0
        max_reasoning_buffer_chars = 0
        pure_reasoning_matched_count = 0
        reasoning_rejected_content_count = 0
        reasoning_rejected_tool_count = 0
        reasoning_rejected_finish_count = 0
        reasoning_rejected_parse_count = 0
        first_reasoning_forwarded = False
        reasoning_buffer_parts = []
        reasoning_buffer_chars = 0
        reasoning_buffer_template = None
        reasoning_buffer_identity = None
        last_reasoning_flush_at = None
        content_buffer_parts = []
        content_buffer_chars = 0
        content_buffer_template = None
        content_buffer_identity = None
        last_content_flush_at = None

        async def _flush_reasoning_buffer():
            nonlocal reasoning_buffer_chars, reasoning_buffer_template, reasoning_buffer_identity
            nonlocal reasoning_forwarded_chunk_count, reasoning_flush_count, last_reasoning_flush_at
            if not reasoning_buffer_parts:
                return True

            merged_reasoning = "".join(reasoning_buffer_parts)
            payload = dict(reasoning_buffer_template)
            payload["choices"] = [{
                "index": reasoning_buffer_identity[-1],
                "delta": {"reasoning_content": merged_reasoning},
                "finish_reason": None,
            }]
            reasoning_buffer_parts.clear()
            reasoning_buffer_chars = 0
            reasoning_buffer_template = None
            reasoning_buffer_identity = None

            sent = await _send_asgi({
                "type": "http.response.body",
                "body": f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"),
                "more_body": True,
            })
            if sent:
                reasoning_forwarded_chunk_count += 1
                reasoning_flush_count += 1
                last_reasoning_flush_at = time.perf_counter()
            return sent

        async def _flush_content_buffer():
            nonlocal content_buffer_chars, content_buffer_template, content_buffer_identity
            nonlocal content_forwarded_chunk_count, content_flush_count, last_content_flush_at
            if not content_buffer_parts:
                return True

            merged_content = "".join(content_buffer_parts)
            payload = dict(content_buffer_template)
            payload["choices"] = [{
                "index": content_buffer_identity[-1],
                "delta": {"content": merged_content},
                "finish_reason": None,
            }]
            content_buffer_parts.clear()
            content_buffer_chars = 0
            content_buffer_template = None
            content_buffer_identity = None

            sent = await _send_asgi({
                "type": "http.response.body",
                "body": f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8"),
                "more_body": True,
            })
            if sent:
                content_forwarded_chunk_count += 1
                content_flush_count += 1
                last_content_flush_at = time.perf_counter()
            return sent

        # 主循环：透传 + 收集
        while True:
            wait_timeout = KEEPALIVE_INTERVAL_SECONDS
            if reasoning_buffer_parts:
                elapsed_since_flush = time.perf_counter() - last_reasoning_flush_at
                wait_timeout = max(0.0, REASONING_FLUSH_INTERVAL_SECONDS - elapsed_since_flush)
            if content_buffer_parts:
                elapsed_since_content_flush = time.perf_counter() - last_content_flush_at
                content_wait_timeout = max(0.0, content_flush_interval_seconds - elapsed_since_content_flush)
                wait_timeout = min(wait_timeout, content_wait_timeout) if reasoning_buffer_parts else content_wait_timeout
            try:
                chunk = await asyncio.wait_for(async_queue.get(), timeout=wait_timeout)
            except asyncio.TimeoutError:
                if reasoning_buffer_parts:
                    if not await _flush_reasoning_buffer():
                        break
                    continue
                if content_buffer_parts:
                    if not await _flush_content_buffer():
                        break
                    continue
                heartbeat_count += 1
                if not await _send_asgi({"type": "http.response.body", "body": b": ping\n\n", "more_body": True}):
                    break
                continue

            if chunk is stream_end:
                if not await _flush_reasoning_buffer():
                    break
                if not await _flush_content_buffer():
                    break
                break

            if upstream_first_sse_ms is None:
                upstream_first_sse_ms = int((time.perf_counter() - stream_started) * 1000)

            if isinstance(chunk, dict) and "error" in chunk:
                if not await _flush_reasoning_buffer():
                    break
                if not await _flush_content_buffer():
                    break
                _log(f"❌ 上游流式报错: {chunk['error']}")
                err_chunk = {
                    "id": "chatcmpl-error",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req_data.get("model"),
                    "choices": [{"index": 0, "delta": {"content": f"\n\n[上游错误] {chunk['error']}"}, "finish_reason": "stop"}],
                }
                if not await _send_asgi({"type": "http.response.body", "body": f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8"), "more_body": True}):
                    break
                continue

            if chunk == "data: [DONE]":
                if not await _flush_reasoning_buffer():
                    break
                if not await _flush_content_buffer():
                    break
                if not received_done:
                    received_done = True
                    sse_line_count += 1
                    if await _send_asgi({"type": "http.response.body", "body": b"data: [DONE]\n\n", "more_body": False}):
                        response_closed = True
                        done_sent_with_end_stream = True
                break

            if received_done:
                continue

            sse_line_count += 1
            parsed_chunk = None
            usage_only_chunk = False
            reasoning_value = None
            content_value = None
            tool_calls_value = None
            finish_reason_value = None

            if chunk.startswith("data: ") and chunk != "data: [DONE]":
                try:
                    parsed_chunk = json.loads(chunk[6:])
                    choices = parsed_chunk.get("choices")
                    if isinstance(parsed_chunk.get("usage"), dict):
                        latest_usage = parsed_chunk["usage"]
                        if choices == []:
                            usage_chunk_seen = True
                            usage_only_chunk = True
                except Exception:
                    parsed_chunk = None
                    reasoning_rejected_parse_count += 1

            if parsed_chunk is not None:
                try:
                    choices = parsed_chunk.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta", {})
                        event_ms = int((time.perf_counter() - stream_started) * 1000)
                        reasoning_candidate = delta.get("reasoning_content")
                        content_value = delta.get("content")
                        tool_calls_value = delta.get("tool_calls")
                        finish_reason_value = choices[0].get("finish_reason")
                        if isinstance(reasoning_candidate, str) and reasoning_candidate:
                            reasoning_value = reasoning_candidate
                            collected_reasoning += reasoning_value
                            reasoning_raw_chunk_count += 1
                            reasoning_chars += len(reasoning_value)
                            if first_reasoning_ms is None:
                                first_reasoning_ms = event_ms
                            last_reasoning_ms = event_ms
                        if isinstance(content_value, str) and content_value:
                            collected_content += content_value
                            content_chunk_count += 1
                            content_raw_chunk_count += 1
                            content_chars += len(content_value)
                            if first_content_ms is None:
                                first_content_ms = event_ms
                                if last_reasoning_ms is not None:
                                    reasoning_to_content_gap_ms = max(0, event_ms - last_reasoning_ms)
                        if tool_calls_value:
                            tool_call_chunk_count += 1
                            for tc in tool_calls_value:
                                _merge_tool_call_delta(tool_calls_dict, tc)
                except Exception:
                    pass

            pure_reasoning = _pure_reasoning_chunk_info(parsed_chunk)
            if pure_reasoning is not None:
                if not await _flush_content_buffer():
                    break
                pure_reasoning_matched_count += 1
                if not first_reasoning_forwarded:
                    if not await _send_asgi({"type": "http.response.body", "body": (chunk + "\n\n").encode("utf-8"), "more_body": True}):
                        break
                    reasoning_forwarded_chunk_count += 1
                    first_reasoning_forwarded = True
                    last_reasoning_flush_at = time.perf_counter()
                    continue

                if reasoning_buffer_parts and reasoning_buffer_identity != pure_reasoning["identity"]:
                    if not await _flush_reasoning_buffer():
                        break
                if not reasoning_buffer_parts:
                    reasoning_buffer_template = pure_reasoning["template"]
                    reasoning_buffer_identity = pure_reasoning["identity"]
                reasoning_buffer_parts.append(pure_reasoning["reasoning_content"])
                reasoning_buffer_chars += len(pure_reasoning["reasoning_content"])
                max_reasoning_buffer_chars = max(max_reasoning_buffer_chars, reasoning_buffer_chars)

                elapsed_since_flush = time.perf_counter() - last_reasoning_flush_at
                if (
                    reasoning_buffer_chars >= REASONING_BUFFER_MAX_CHARS
                    or elapsed_since_flush >= REASONING_FLUSH_INTERVAL_SECONDS
                ):
                    if not await _flush_reasoning_buffer():
                        break
                continue

            if reasoning_value is not None:
                rejected_for_semantics = False
                if content_value not in (None, ""):
                    reasoning_rejected_content_count += 1
                    rejected_for_semantics = True
                if tool_calls_value not in (None, []):
                    reasoning_rejected_tool_count += 1
                    rejected_for_semantics = True
                if finish_reason_value is not None:
                    reasoning_rejected_finish_count += 1
                    rejected_for_semantics = True
                if not rejected_for_semantics:
                    reasoning_rejected_parse_count += 1

            pure_content = _pure_content_chunk_info(parsed_chunk)
            if pure_content is not None:
                if not await _flush_reasoning_buffer():
                    break
                pure_content_matched_count += 1
                if not first_content_forwarded:
                    if not await _send_asgi({"type": "http.response.body", "body": (chunk + "\n\n").encode("utf-8"), "more_body": True}):
                        break
                    content_forwarded_chunk_count += 1
                    first_content_forwarded = True
                    last_content_flush_at = time.perf_counter()
                    continue

                if content_buffer_parts and content_buffer_identity != pure_content["identity"]:
                    if not await _flush_content_buffer():
                        break
                if not content_buffer_parts:
                    content_buffer_template = pure_content["template"]
                    content_buffer_identity = pure_content["identity"]
                content_buffer_parts.append(pure_content["content"])
                content_buffer_chars += len(pure_content["content"])
                max_content_buffer_chars = max(max_content_buffer_chars, content_buffer_chars)

                elapsed_since_content_flush = time.perf_counter() - last_content_flush_at
                if (
                    content_buffer_chars >= content_buffer_max_chars
                    or elapsed_since_content_flush >= content_flush_interval_seconds
                ):
                    if not await _flush_content_buffer():
                        break
                continue

            if not await _flush_reasoning_buffer():
                break
            if not await _flush_content_buffer():
                break
            if usage_only_chunk:
                continue
            if not await _send_asgi({"type": "http.response.body", "body": (chunk + "\n\n").encode("utf-8"), "more_body": True}):
                break
            if reasoning_value is not None:
                reasoning_forwarded_chunk_count += 1
                last_reasoning_flush_at = time.perf_counter()
            if isinstance(content_value, str) and content_value:
                content_forwarded_chunk_count += 1
                last_content_flush_at = time.perf_counter()

        if not response_closed and not client_disconnected:
            if await _send_asgi({"type": "http.response.body", "body": b"", "more_body": False}):
                response_closed = True
                extra_final_body_sent = True

        usage_line = _cache_usage_line(latest_usage)
        if usage_line:
            _log(usage_line)
        total_stream_ms = int((time.perf_counter() - stream_started) * 1000)
        _log(
            "[stream-diagnostic] "
            f"upstream_first_sse_ms={upstream_first_sse_ms if upstream_first_sse_ms is not None else -1}; "
            f"heartbeat_count={heartbeat_count}; "
            f"usage_chunk_seen={str(usage_chunk_seen).lower()}; "
            f"usage_chunk_forwarded={str(usage_chunk_forwarded).lower()}; "
            f"sse_line_count={sse_line_count}; "
            f"asgi_send_count={asgi_send_count}; "
            f"received_done={str(received_done).lower()}; "
            f"client_disconnected={str(client_disconnected).lower()}; "
            "client_requested_stream=true; "
            f"done_sent_with_end_stream={str(done_sent_with_end_stream).lower()}; "
            f"extra_final_body_sent={str(extra_final_body_sent).lower()}; "
            f"reasoning_raw_chunk_count={reasoning_raw_chunk_count}; "
            f"reasoning_forwarded_chunk_count={reasoning_forwarded_chunk_count}; "
            f"reasoning_chars={reasoning_chars}; "
            f"first_reasoning_ms={first_reasoning_ms if first_reasoning_ms is not None else -1}; "
            f"first_content_ms={first_content_ms if first_content_ms is not None else -1}; "
            f"reasoning_to_content_gap_ms={reasoning_to_content_gap_ms if reasoning_to_content_gap_ms is not None else -1}; "
            f"content_chunk_count={content_chunk_count}; "
            f"content_raw_chunk_count={content_raw_chunk_count}; "
            f"content_forwarded_chunk_count={content_forwarded_chunk_count}; "
            f"content_flush_count={content_flush_count}; "
            f"content_chars={content_chars}; "
            f"max_content_buffer_chars={max_content_buffer_chars}; "
            f"pure_content_matched_count={pure_content_matched_count}; "
            f"first_content_forwarded={str(first_content_forwarded).lower()}; "
            f"total_text_forwarded_chunk_count={reasoning_forwarded_chunk_count + content_forwarded_chunk_count}; "
            f"tool_call_chunk_count={tool_call_chunk_count}; "
            f"reasoning_flush_count={reasoning_flush_count}; "
            f"max_reasoning_buffer_chars={max_reasoning_buffer_chars}; "
            f"pure_reasoning_matched_count={pure_reasoning_matched_count}; "
            f"reasoning_rejected_content_count={reasoning_rejected_content_count}; "
            f"reasoning_rejected_tool_count={reasoning_rejected_tool_count}; "
            f"reasoning_rejected_finish_count={reasoning_rejected_finish_count}; "
            f"reasoning_rejected_parse_count={reasoning_rejected_parse_count}; "
            f"first_reasoning_forwarded={str(first_reasoning_forwarded).lower()}; "
            f"send_error_type={send_error_type}; "
            f"total_stream_ms={total_stream_ms}"
        )


        # ==========================================
        # 💾 异步写入：把本轮对话存到 Supabase + Pinecone（不阻塞响应）
        # 持有 task 强引用避免被 GC，完成时自动从集合移除
        # ==========================================
        if sb and user_msg and (collected_content or tool_calls_dict):
            task = asyncio.create_task(
                self._save_conversation(sb, user_msg, collected_content, collected_reasoning, tool_calls_dict)
            )
            _pending_save_tasks.add(task)
            task.add_done_callback(_pending_save_tasks.discard)

    async def _inject_context(self, req_data, sb, current_query):
        """
        智能体上下文注入（缓存最优版）：

        ┌─────────────────────────────────────────────────────┐
        │ system message  →  稳定内容（跨轮不变，可被 cache）    │
        │   persona / user_facts / Core_Cognition              │
        ├─────────────────────────────────────────────────────┤
        │ 多轮对话历史  →  前端原样透传（跨轮不变，随轮增长）     │
        │   [user_1, assistant_1, user_2, assistant_2, ...]    │
        ├─────────────────────────────────────────────────────┤
        │ 最后一条 user 消息  →  原始 query + 动态内容后缀       │
        │   时间 / 向量记忆 ... 仅本条不被 cache，代价最小       │
        └─────────────────────────────────────────────────────┘

        GPT 之前的做法（在 last_user 前插入 role:system 消息）会打断多轮历史的 prefix 连续性，
        导致每轮只能命中 system 这 ~1.3K token。改为拼入 user content 后，
        cache 可以随对话轮数增长，长对话命中率趋近 90%+。
        """
        user_name = os.environ.get("USER_NAME", "用户")
        user_id = os.environ.get("USER_ID", "default")
        persona = os.environ.get("AI_PERSONA", "").strip()
        chat_tag = os.environ.get("CHAT_TAG", "Web_Chat")
        context_started = time.perf_counter()

        cache_friendly = os.environ.get("CACHE_FRIENDLY_MODE", "true").strip().lower() not in ("0", "false", "no", "off")
        time_injection = os.environ.get("TIME_INJECTION", "hour" if cache_friendly else "minute").strip().lower()
        inject_silence = os.environ.get("INJECT_SILENCE_HOURS", "true").strip().lower() in ("1", "true", "yes", "on")

        core_summary_limit = _env_int("CORE_SUMMARY_LIMIT", 1, 0, 3)
        core_summary_chars = _env_int("CORE_SUMMARY_CHARS", 1000, 0, 6000)

        try:
            vector_top_k = int(os.environ.get("VECTOR_TOP_K", "3"))
        except Exception:
            vector_top_k = 3
        vector_top_k = max(0, min(vector_top_k, 8))

        try:
            vector_memory_chars = int(os.environ.get("VECTOR_MEMORY_CHARS", "1000"))
        except Exception:
            vector_memory_chars = 1000
        vector_memory_chars = max(200, min(vector_memory_chars, 6000))

        try:
            user_fact_limit = int(os.environ.get("USER_FACT_LIMIT", "40"))
        except Exception:
            user_fact_limit = 40
        user_fact_limit = max(1, min(user_fact_limit, 80))

        try:
            user_fact_value_chars = int(os.environ.get("USER_FACT_VALUE_CHARS", "500"))
        except Exception:
            user_fact_value_chars = 500
        user_fact_value_chars = max(80, min(user_fact_value_chars, 2000))

        # 先清理末尾 assistant 尾巴，防止前端误带导致请求结构异常。
        while req_data.get("messages") and req_data["messages"][-1].get("role") == "assistant":
            req_data["messages"].pop()

        # ===== 时间：保留时间感，但避免分钟/秒级频繁破坏缓存 =====
        now_bj = datetime.datetime.utcnow() + datetime.timedelta(hours=8)
        if time_injection in ("off", "none", "false", "0"):
            time_line = ""
        elif time_injection in ("minute", "full"):
            time_line = f"当前时间：{now_bj.strftime('%Y-%m-%d %H:%M')}（北京时间）。"
        elif time_injection in ("hour", "hours", "coarse"):
            hour = now_bj.hour
            if 0 <= hour < 5:
                part = "深夜"
            elif 5 <= hour < 8:
                part = "清晨"
            elif 8 <= hour < 12:
                part = "上午"
            elif 12 <= hour < 14:
                part = "中午"
            elif 14 <= hour < 18:
                part = "下午"
            elif 18 <= hour < 22:
                part = "晚上"
            else:
                part = "深夜"
            time_line = f"当前时间背景：{now_bj.strftime('%Y-%m-%d')}，{part}，约 {hour} 点（北京时间）。"
        else:
            time_line = f"当前日期：{now_bj.strftime('%Y-%m-%d')}（北京时间）。"

        silence_hours = None
        if inject_silence:
            try:
                res = await asyncio.to_thread(lambda: sb.table("memories").select("created_at").eq("tags", chat_tag).order("created_at", desc=True).limit(1).execute())
                if res and res.data:
                    last = res.data[0].get("created_at", "")
                    if last:
                        raw = last[:19]
                        fmt = "%Y-%m-%d %H:%M:%S" if "T" not in raw else "%Y-%m-%dT%H:%M:%S"
                        last_dt = datetime.datetime.strptime(raw, fmt)
                        silence_hours = max(0, round((now_bj - last_dt).total_seconds() / 3600, 1))
            except Exception:
                silence_hours = None

        # ===== 三路并行查询，消除串行等待导致的首 token 延迟 =====
        # core_summaries、user_facts、Pinecone 向量检索同时发出，取最慢的那个为准
        async def _fetch_core_summaries():
            if core_summary_limit <= 0:
                return "无长期阶段总结", 0
            try:
                sr = await asyncio.to_thread(lambda: sb.table("memories").select("content").eq("tags", "Core_Cognition").order("created_at", desc=True).limit(core_summary_limit).execute())
                if sr and sr.data:
                    lines = []
                    for s in sr.data[:core_summary_limit]:
                        lines.append(f"- {str(s.get('content', ''))[:core_summary_chars]}")
                    return "\n".join(lines), len(lines)
            except Exception:
                pass
            return "无长期阶段总结", 0

        async def _fetch_user_prof():
            try:
                pr = await asyncio.to_thread(lambda: sb.table("user_facts").select("key, value").neq("key", "sys_config").neq("key", "llm_settings").order("key").execute())
                if pr and pr.data:
                    lines = []
                    for r in pr.data[:user_fact_limit]:
                        key = str(r.get("key", ""))
                        val = str(r.get("value", ""))[:user_fact_value_chars]
                        lines.append(f"- {key}: {val}")
                    return "\n".join(lines)
            except Exception:
                pass
            return "暂无"

        async def _fetch_vector():
            if vector_top_k <= 0:
                return "无相关深层记忆", 0
            try:
                import server
                vc = getattr(server, "vector_client", None)
                if vc and vc.index and current_query.strip():
                    def _s():
                        return vc.search(query=str(current_query), user_id=user_id, limit=vector_top_k)
                    results = await asyncio.to_thread(_s)
                    if isinstance(results, list) and results:
                        chunks = []
                        for item in results:
                            text = item.get("memory", str(item)) if isinstance(item, dict) else str(item)
                            text = str(text).strip()
                            if text:
                                chunks.append(f"- {text[:vector_memory_chars]}")
                        if chunks:
                            return "\n".join(chunks), len(chunks)
            except Exception as e:
                _log(f"Pinecone 检索失败（跳过）: {e}")
            return "无相关深层记忆", 0

        (core_summaries, core_summary_count), user_prof, (vector_context, vector_result_count) = await asyncio.gather(
            _fetch_core_summaries(),
            _fetch_user_prof(),
            _fetch_vector(),
        )

        # ===== 稳定层注入 system =====
        # ⚡ 关键：这里只放 persona（真正永不变的人设）。
        # user_prof / core_summaries 虽然更新不算频繁，但 core_summaries 每 30 条消息就会变一次，
        # 一旦放在最前面的 system 里，每次更新都会让"整个前缀"失效，把后面累积的多轮历史缓存也一起清零。
        # 所以把它们挪到 dynamic_part（最后一条 user 消息里），只让本轮这一条不缓存，代价最小。
        stable_inject = persona.strip() if persona else ""

        has_system = False
        for m in req_data.get("messages", []):
            if m.get("role") == "system":
                m["content"] = str(m.get("content", "")) + ("\n\n" + stable_inject if stable_inject else "")
                has_system = True
                break
        if not has_system and req_data.get("messages") and stable_inject:
            req_data["messages"].insert(0, {"role": "system", "content": stable_inject})

        dynamic_lines = []
        if time_line:
            dynamic_lines.append(time_line)
        if silence_hours is not None:
            dynamic_lines.append(f"距离上次聊天约 {silence_hours} 小时。")
        dynamic_header = "\n".join(dynamic_lines) if dynamic_lines else "本轮未注入精确时间。"

        dynamic_part = (
            f"[本轮动态上下文]\n"
            f"{dynamic_header}\n"
            f"【{user_name}的核心画像 / user_facts】:\n{user_prof}\n\n"
            f"【近{core_summary_limit}次阶段总结 / Core_Cognition】:\n{core_summaries}\n"
            f"--- 以下为调取的历史背景记忆：这是过去的事，不是现在正在发生的事 ---\n"
            f"【深层关联记忆 / Pinecone】:\n{vector_context}\n"
            f"------------------------------------------------"
        )

        # ===== ⚡ 关键修复：动态内容直接追加到最后一条 user 消息的文本里 =====
        #
        # GPT 的方案（在 last_user 前面插入一条 role:system 消息）是错的，原因：
        #
        #   Turn 2 DeepSeek 看到：[system_stable, user_1, assistant_1, DYNAMIC_SYS, user_2]
        #   Turn 3 DeepSeek 看到：[system_stable, user_1, assistant_1, user_2, assistant_2, DYNAMIC_SYS, user_3]
        #
        #   Turn 3 的 index-3 是 user_2，但 Turn 2 缓存的 index-3 是 DYNAMIC_SYS → 在 index-3 就 cache miss。
        #   结果：每轮只能命中 system_stable 这 ~1.3K token，永远涨不了。
        #
        # 正确做法：把动态内容直接拼进最后一条 user 消息的末尾。
        # 这样 [system_stable, user_1, assistant_1, user_2, assistant_2, ...] 这段多轮历史
        # 完全不被动态内容破坏，DeepSeek 可以跨轮复用，缓存随对话轮数不断增长。
        #
        # 缓存增长示意：
        #   Turn 2: cache = system(1.3K) + user_1 + assistant_1 = ~2K
        #   Turn 3: cache = system(1.3K) + user_1 + assistant_1 + user_2 + assistant_2 = ~3K
        #   Turn N: cache ≈ (N-1) × 平均每轮 token → 命中率随对话增长趋近 90%+

        # 清理：去掉末尾误带的 assistant（防止前端多传）
        while req_data.get("messages") and req_data["messages"][-1].get("role") == "assistant":
            req_data["messages"].pop()

        # 找最后一条 user 消息，把动态上下文后缀拼入其 content
        messages = req_data.get("messages", [])
        _append_dynamic_context_to_last_user(messages, dynamic_part)

        _log(
            f"🧠 [智能体/用户消息注入] 注入完成：画像{len(user_prof)}字 + "
            f"总结{len(core_summaries)}字 + 向量记忆{len(vector_context)}字；"
            f"time={time_injection}, vector_top_k={vector_top_k}"
        )
        _log(
            "[context-diagnostic] "
            f"context_fetch_ms={int((time.perf_counter() - context_started) * 1000)}; "
            f"user_facts_chars={len(user_prof)}; "
            f"core_summary_count={core_summary_count}; "
            f"core_summary_chars={len(core_summaries)}; "
            f"vector_result_count={vector_result_count}; "
            f"vector_chars={len(vector_context)}; "
            f"dynamic_chars={len(dynamic_part)}"
        )


    async def _save_conversation(self, sb, user_msg, ai_msg, reasoning, tool_calls):
        """异步把本轮对话存到 Supabase memories 表 + Pinecone"""
        ai_name = os.environ.get("AI_NAME", "助手")
        user_name = os.environ.get("USER_NAME", "用户")
        user_id = os.environ.get("USER_ID", "default")
        chat_tag = os.environ.get("CHAT_TAG", "Web_Chat")
        now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        final_save_text = ai_msg or ""
        if reasoning and _env_bool("SAVE_REASONING_TO_MEMORY", False):
            final_save_text = f"<think>\n{reasoning}\n</think>\n\n{final_save_text}"
        if not final_save_text and tool_calls:
            tc_names = [tc.get("function", {}).get("name", "unknown") for tc in tool_calls.values()]
            final_save_text = f"[系统记录：调用了工具 {', '.join(tc_names)}]"

        # 1. 存到 memories 表（user + assistant 两条）。失败自动重试一次。
        def _save_both():
            sb.table("memories").insert({
                "title": f"💬 {user_name}说",
                "content": f"{user_name}：{user_msg[:2000]}",
                "category": "流水",
                "mood": "平静",
                "tags": chat_tag,
                "created_at": now_str,
            }).execute()
            sb.table("memories").insert({
                "title": f"🤖 {ai_name}回复",
                "content": f"我({ai_name})：{final_save_text[:2000]}",
                "category": "流水",
                "mood": "温和",
                "tags": chat_tag,
                "created_at": now_str,
            }).execute()

        saved = False
        for attempt in (1, 2):
            try:
                await asyncio.to_thread(_save_both)
                saved = True
                break
            except Exception as e:
                if attempt == 1:
                    _log(f"⚠️ 存库首次失败，1s 后重试: {e}")
                    await asyncio.sleep(1.0)
                else:
                    _log(f"❌ 存库重试仍失败，放弃: {e}")
        if saved:
            _log(f"💾 已存库：{user_name}问({len(user_msg)}字) + {ai_name}答({len(final_save_text)}字)")

        # 2. 写入 Pinecone 向量记忆（可选）
        try:
            import server
            vc = getattr(server, "vector_client", None)
            if vc and vc.index and user_msg:
                def _add_vec():
                    vc.add([
                        {"role": "user", "content": user_msg},
                        {"role": "assistant", "content": final_save_text},
                    ], user_id=user_id)
                await asyncio.to_thread(_add_vec)
                _log("🧠 Pinecone 已写入")
        except Exception as e:
            _log(f"Pinecone 写入失败: {e}")

        # 3. 🧠 异步触发全渠道统一对话总结（不阻塞响应）
        #    监控网页/QQ/TG/邮件等所有渠道的对话流水，
        #    累计达到 SUMMARY_THRESHOLD（默认30条）时自动总结归档。
        try:
            import napcat
            await napcat.check_and_summarize_all()
        except Exception as e:
            _log(f"⚠️ 触发对话总结失败（不影响主流程）: {e}")

    # ------------------------------------------
    # 管理接口
    # ------------------------------------------

    async def _handle_logs(self, send):
        try:
            await _send_json_resp(send, 200, {"logs": "\n".join(_system_logs_buffer[-100:])})
        except Exception as e:
            await _send_json_resp(send, 500, {"error": str(e)})


# ==========================================
# 辅助函数
# ==========================================

async def _check_api_secret(scope, send):
    """校验 API_SECRET。返回 True=通过，False=已拒绝(已发送 401)"""
    api_secret = os.environ.get("API_SECRET", "").strip()
    if not api_secret:
        return True   # 没配就不强制鉴权（保持兼容）
    headers_dict = {k.decode("utf-8").lower(): v.decode("utf-8") for k, v in scope.get("headers", [])}
    auth_token = headers_dict.get("authorization", "").replace("Bearer ", "").replace("bearer ", "").strip()
    x_api_key = headers_dict.get("x-api-key", "").strip()
    if auth_token != api_secret and x_api_key != api_secret:
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"), (b"access-control-allow-origin", b"*")]})
        await send({"type": "http.response.body", "body": b'{"error":"Unauthorized: Missing or invalid API key"}'})
        return False
    return True


async def _send_json_resp(send, status: int, data: dict):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"access-control-allow-origin", b"*"),
            (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
            (b"access-control-allow-headers", b"Content-Type, Authorization"),
        ]
    })
    await send({"type": "http.response.body", "body": body})


async def _send_cors_preflight(send):
    await send({
        "type": "http.response.start",
        "status": 204,
        "headers": [
            (b"access-control-allow-origin", b"*"),
            (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
            (b"access-control-allow-headers", b"Content-Type, Authorization"),
            (b"access-control-max-age", b"86400"),
        ]
    })
    await send({"type": "http.response.body", "body": b""})
