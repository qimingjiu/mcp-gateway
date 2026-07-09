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


def _log(msg: str):
    """统一的日志打印 + 内存缓存（供 /api/logs 查询）"""
    line = f"[{datetime.datetime.utcnow().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    _system_logs_buffer.append(line)
    if len(_system_logs_buffer) > _MAX_LOGS:
        del _system_logs_buffer[: len(_system_logs_buffer) - _MAX_LOGS]


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

        if sb and user_msg:
            try:
                await self._inject_context(req_data, sb, user_msg)
            except Exception as e:
                _log(f"⚠️ 上文注入失败（已降级为透传）: {e}")
        else:
            if sb:
                _log("➡️ [透传] 无 user 消息或无 Supabase，直接转发")

        # 强制流式（便于边透传边收集）
        req_data["stream"] = True
        if req_data.get("tools"):
            req_data["tool_choice"] = "auto"

        # 构造请求头（修复 python-requests UA 被拦截 + 透传客户端头）
        client_headers = {k.decode("utf-8", "ignore").lower(): v.decode("utf-8", "ignore") for k, v in scope.get("headers", [])}
        client_ua = client_headers.get("user-agent", "")
        fwd_headers = {
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json",
            "User-Agent": client_ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": client_headers.get("accept", "application/json"),
        }
        for h in ("accept-language", "x-requested-with"):
            if h in client_headers:
                fwd_headers[h] = client_headers[h]

        _log(f"➡️ [转发] POST {upstream_url} | model={req_data.get('model')} | key={upstream_key[:6]}***")

        # 启动响应流（通知客户端开始接收 SSE）
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/event-stream; charset=utf-8"),
                (b"cache-control", b"no-cache"),
                (b"access-control-allow-origin", b"*"),
            ],
        })

        # 后台线程：读取上游流，喂给队列
        import queue
        import threading
        q = queue.Queue()

        def _stream_forward():
            try:
                with requests.post(upstream_url, headers=fwd_headers, json=req_data, stream=True, timeout=300) as resp:
                    if resp.status_code != 200:
                        q.put({"error": f"HTTP {resp.status_code}: {resp.text[:500]}"})
                        q.put(None)
                        return
                    for line in resp.iter_lines(chunk_size=1, decode_unicode=True):
                        if line:
                            q.put(line)
                q.put(None)
            except Exception as e:
                q.put({"error": str(e)})
                q.put(None)

        threading.Thread(target=_stream_forward, daemon=True).start()

        collected_content = ""
        collected_reasoning = ""
        tool_calls_dict = {}

        # 主循环：透传 + 收集
        while True:
            chunk = await asyncio.to_thread(q.get)
            if chunk is None:
                break

            if isinstance(chunk, dict) and "error" in chunk:
                _log(f"❌ 上游流式报错: {chunk['error']}")
                err_chunk = {
                    "id": "chatcmpl-error",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": req_data.get("model"),
                    "choices": [{"index": 0, "delta": {"content": f"\n\n[上游错误] {chunk['error']}"}, "finish_reason": "stop"}],
                }
                await send({"type": "http.response.body", "body": f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8"), "more_body": True})
                continue

            await send({"type": "http.response.body", "body": (chunk + "\n\n").encode("utf-8"), "more_body": True})

            if chunk.startswith("data: ") and chunk != "data: [DONE]":
                try:
                    dj = json.loads(chunk[6:])
                    if dj.get("choices"):
                        delta = dj["choices"][0].get("delta", {})
                        if delta.get("content"):
                            collected_content += delta["content"]
                        if delta.get("reasoning_content"):
                            collected_reasoning += delta["reasoning_content"]
                        if delta.get("tool_calls"):
                            for tc in delta["tool_calls"]:
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_dict:
                                    tool_calls_dict[idx] = tc
                                else:
                                    if tc.get("function", {}).get("arguments"):
                                        tool_calls_dict[idx]["function"].setdefault("arguments", "")
                                        tool_calls_dict[idx]["function"]["arguments"] += tc["function"]["arguments"]
                except Exception:
                    pass

        # 结束响应
        await send({"type": "http.response.body", "body": b"", "more_body": False})

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
        智能体上下文注入（尾部动态注入版）：
        - 稳定内容只放在最前面的 system：AI_PERSONA / user_facts / Core_Cognition
        - 动态内容放到最后一条 user 之前：时间 / 沉默时长 / Pinecone 本轮检索 / Supabase 最近历史
        - 这样 DeepSeek prompt cache 不会被动态 RAG 卡在 2K 左右，能继续复用前缀里的稳定人设和前端历史
        """
        ai_name = os.environ.get("AI_NAME", "助手")
        user_name = os.environ.get("USER_NAME", "用户")
        user_id = os.environ.get("USER_ID", "default")
        persona = os.environ.get("AI_PERSONA", "").strip()
        chat_tag = os.environ.get("CHAT_TAG", "Web_Chat")

        cache_friendly = os.environ.get("CACHE_FRIENDLY_MODE", "true").strip().lower() not in ("0", "false", "no", "off")
        time_injection = os.environ.get("TIME_INJECTION", "hour" if cache_friendly else "minute").strip().lower()
        inject_silence = os.environ.get("INJECT_SILENCE_HOURS", "true").strip().lower() in ("1", "true", "yes", "on")

        try:
            history_limit = int(os.environ.get("HISTORY_LIMIT", "4"))
        except Exception:
            history_limit = 4
        history_limit = max(0, min(history_limit, 20))

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

        # ===== 稳定层：放在最前面，尽量让 DeepSeek cache 命中这部分 =====
        core_summaries = "无长期阶段总结"
        try:
            sr = await asyncio.to_thread(lambda: sb.table("memories").select("content").eq("tags", "Core_Cognition").order("created_at", desc=True).limit(3).execute())
            if sr and sr.data:
                core_summaries = "\n".join([f"- {str(s.get('content', ''))[:1200]}" for s in sr.data])
        except Exception:
            pass

        user_prof = "暂无"
        try:
            pr = await asyncio.to_thread(lambda: sb.table("user_facts").select("key, value").neq("key", "sys_config").neq("key", "llm_settings").order("key").execute())
            if pr and pr.data:
                lines = []
                for r in pr.data[:user_fact_limit]:
                    key = str(r.get("key", ""))
                    val = str(r.get("value", ""))[:user_fact_value_chars]
                    lines.append(f"- {key}: {val}")
                user_prof = "\n".join(lines)
        except Exception:
            pass

        stable_parts = []
        if persona:
            stable_parts.append(persona)
        stable_parts.append(
            f"\n[长期稳定上下文]\n"
            f"【{user_name}的核心画像 / user_facts】:\n{user_prof}\n\n"
            f"【近3次阶段总结 / Core_Cognition】:\n{core_summaries}\n"
        )
        stable_inject = "\n".join(stable_parts).strip()

        # 注入稳定 system：已有 system 就追加；没有就插到最前。
        has_system = False
        for m in req_data.get("messages", []):
            if m.get("role") == "system":
                m["content"] = str(m.get("content", "")) + "\n\n" + stable_inject
                has_system = True
                break
        if not has_system and req_data.get("messages"):
            req_data["messages"].insert(0, {"role": "system", "content": stable_inject})

        # ===== 动态层：本轮 query 相关，只放到最后一条 user 前面 =====
        vector_context = "无相关深层记忆"
        if vector_top_k > 0:
            try:
                import server
                vc = getattr(server, "vector_client", None)
                if vc and vc.index and current_query.strip():
                    def _s():
                        return vc.search(query=str(current_query), user_id=user_id, limit=vector_top_k)
                    results = await asyncio.to_thread(_s)
                    if isinstance(results, list) and results:
                        chunks = []
                        for m in results:
                            if isinstance(m, dict):
                                text = m.get("memory", str(m))
                            else:
                                text = str(m)
                            text = str(text).strip()
                            if text:
                                chunks.append(f"- {text[:vector_memory_chars]}")
                        if chunks:
                            vector_context = "\n".join(chunks)
            except Exception as e:
                _log(f"Pinecone 检索失败（跳过）: {e}")

        history_msgs = []
        if history_limit > 0:
            try:
                _TAGS = [chat_tag, "TG_MSG", "QQ_Chat", "QQ_Group", "Email_Process"]
                fetch_limit = max(history_limit * 2, history_limit)
                hr = await asyncio.to_thread(lambda: sb.table("memories").select("content, tags").in_("tags", _TAGS).order("created_at", desc=True).limit(fetch_limit).execute())
                if hr and hr.data:
                    rows = list(reversed(hr.data))[-history_limit:]
                    for row in rows:
                        c = str(row.get("content", "")).strip()
                        if not c:
                            continue
                        if c.startswith(user_name):
                            history_msgs.append({"role": "user", "content": (c.split("：", 1)[-1] if "：" in c else c)[:500]})
                        elif c.startswith("我(") or c.startswith(f"我({ai_name})"):
                            history_msgs.append({"role": "assistant", "content": (c.split("：", 1)[-1] if "：" in c else c)[:500]})
                    merged = []
                    for m in history_msgs:
                        if merged and merged[-1]["role"] == m["role"]:
                            merged[-1]["content"] += "\n" + m["content"]
                        else:
                            merged.append(m)
                    history_msgs = merged
                    while history_msgs and history_msgs[0]["role"] != "user":
                        history_msgs.pop(0)
            except Exception as e:
                _log(f"拉取上文失败（跳过）: {e}")

        dynamic_lines = []
        if time_line:
            dynamic_lines.append(time_line)
        if silence_hours is not None:
            dynamic_lines.append(f"距离上次聊天约 {silence_hours} 小时。")
        dynamic_header = "\n".join(dynamic_lines) if dynamic_lines else "本轮未注入精确时间。"

        dynamic_part = (
            f"[本轮动态上下文]\n"
            f"{dynamic_header}\n"
            f"--- 以下为调取的历史背景记忆：这是过去的事，不是现在正在发生的事 ---\n"
            f"【深层关联记忆 / Pinecone】:\n{vector_context}\n"
            f"------------------------------------------------"
        )

        # 找最后一条 user，把动态上下文和 Supabase 最近上文插到它前面。
        # 这样前面的 stable system + 前端原始多轮历史能继续形成可缓存前缀。
        insert_idx = len(req_data.get("messages", []))
        for i in range(len(req_data.get("messages", [])) - 1, -1, -1):
            if req_data["messages"][i].get("role") == "user":
                insert_idx = i
                break

        tail_msgs = []
        if history_msgs:
            tail_msgs.extend(history_msgs)
        tail_msgs.append({"role": "system", "content": dynamic_part})
        for j, msg in enumerate(tail_msgs):
            req_data["messages"].insert(insert_idx + j, msg)

        _log(
            f"🧠 [智能体/尾部动态注入] 注入完成：画像{len(user_prof)}字 + "
            f"总结{len(core_summaries)}字 + 向量记忆{len(vector_context)}字 + "
            f"上文{len(history_msgs)}条；time={time_injection}, history={history_limit}, vector_top_k={vector_top_k}"
        )


    async def _save_conversation(self, sb, user_msg, ai_msg, reasoning, tool_calls):
        """异步把本轮对话存到 Supabase memories 表 + Pinecone"""
        ai_name = os.environ.get("AI_NAME", "助手")
        user_name = os.environ.get("USER_NAME", "用户")
        user_id = os.environ.get("USER_ID", "default")
        chat_tag = os.environ.get("CHAT_TAG", "Web_Chat")
        now_str = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        final_save_text = ai_msg
        if reasoning:
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