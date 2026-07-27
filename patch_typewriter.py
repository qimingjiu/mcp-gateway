"""构建期补丁：TG 回复打字机模式 v2。
- 流式接收 → 按句切段 → 短句合并成气泡 → 逐气泡发送，句间 typing
- 气泡粒度 / 打字节奏全部走环境变量（TG_BUBBLE_*），改完 Restart 即生效，无需改代码
- 流式与非流式双腿全断时，发送坠机遗言气泡 + 落库 Crash 记录，不再沉默
Docker 构建时对 heartbeat.py 打补丁，打完即删。本地运行则先执行 python patch_typewriter.py。
"""

NEW_FUNC = r'''async def _ask_llm_stream_sentences(client, prompt, temperature=0.8):
    """流式调用主对话模型，按句子异步产出文本（打字机 v2：短句合并，粒度可调）。"""
    model_name = os.environ.get("CHAT_MODEL_NAME", "gpt-4o-mini")
    min_len = int(os.environ.get("TG_BUBBLE_MIN_LEN", "20"))
    max_hold = max(min_len * 8, 160)
    messages = [{"role": "user", "content": prompt}]
    queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def _stream_worker():
        try:
            kwargs = dict(model=model_name, messages=messages, temperature=temperature, stream=True)
            try:
                stream = client.chat.completions.create(**kwargs)
            except Exception as e:
                # 部分模型（如 kimi-k3）只允许 temperature=1，自动降级重试
                if "temperature" in str(e).lower():
                    kwargs["temperature"] = 1
                    stream = client.chat.completions.create(**kwargs)
                else:
                    raise
            for chunk in stream:
                try:
                    delta = chunk.choices[0].delta.content
                except Exception:
                    delta = None
                if delta:
                    loop.call_soon_threadsafe(queue.put_nowait, delta)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=_stream_worker, daemon=True).start()

    buffer = ""
    pending = ""
    pattern = re.compile(r'[^。！？!?…\n]*[。！？!?…\n]+')
    while True:
        item = await queue.get()
        if item is None:
            break
        if isinstance(item, Exception):
            raise item
        buffer += item
        while True:
            m = pattern.match(buffer)
            if not m:
                break
            sent = m.group(0)
            buffer = buffer[m.end():]
            pending += sent
            ready = pending.strip()
            # 严格按最小长度吐泡：攒够 min_len 字才发一个气泡，防碎屏
            if ready and len(ready) >= min_len:
                yield ready
                pending = ""
        if len(buffer) > 400:  # 保险：过长无句读时强制转入待发送
            pending += buffer
            buffer = ""
        if len(pending.strip()) >= max_hold:  # 保险：攒太久强制吐出
            yield pending.strip()
            pending = ""
    tail = (pending + buffer).strip()
    if tail:
        yield tail'''


OLD_BLOCK = r'''                        reply = await _ask_llm_async(client, prompt, temperature=0.8)

                        if reply:
                            await asyncio.to_thread(
                                lambda: requests.post(
                                    f"{base_url}/sendMessage",
                                    json={"chat_id": chat_id, "text": reply},
                                    timeout=15
                                )
                            )'''


NEW_BLOCK = r'''                        # 打字机模式 v2：流式接收 → 按句合并 → 逐气泡发送（句间 typing）
                        _base_delay = float(os.environ.get("TG_BUBBLE_BASE_DELAY", "0.3"))
                        _per_char = float(os.environ.get("TG_BUBBLE_PER_CHAR", "0.08"))
                        _max_delay = float(os.environ.get("TG_BUBBLE_MAX_DELAY", "3.0"))
                        reply_parts = []
                        _stream_err = ""
                        try:
                            async for _sent in _ask_llm_stream_sentences(client, prompt, temperature=0.8):
                                _sent = _sent.strip()
                                if not _sent:
                                    continue
                                reply_parts.append(_sent)
                                await asyncio.to_thread(
                                    lambda s=_sent: requests.post(
                                        f"{base_url}/sendChatAction",
                                        json={"chat_id": chat_id, "action": "typing"},
                                        timeout=10
                                    )
                                )
                                await asyncio.sleep(min(_per_char * len(_sent), _max_delay) + _base_delay)
                                await asyncio.to_thread(
                                    lambda s=_sent: requests.post(
                                        f"{base_url}/sendMessage",
                                        json={"chat_id": chat_id, "text": s},
                                        timeout=15
                                    )
                                )
                        except Exception as _e:
                            _stream_err = str(_e)
                            print(f"❌ 流式回复失败: {_e}")

                        # 流式失败时回退：非流式整段发送
                        if not reply_parts:
                            _fallback = await _ask_llm_async(client, prompt, temperature=0.8)
                            if _fallback:
                                reply_parts.append(_fallback)
                                await asyncio.to_thread(
                                    lambda s=_fallback: requests.post(
                                        f"{base_url}/sendMessage",
                                        json={"chat_id": chat_id, "text": s},
                                        timeout=15
                                    )
                                )
                            else:
                                print("❌ 回退回复也为空（LLM 双腿全断）")

                        # 双腿全断：坠机遗言气泡 + 落库，不让沉默背锅
                        if not reply_parts:
                            _crash = (
                                "🪦 乌鸦坠机：LLM 调用失败，一句话都没接上。\n"
                                f"原因：{(_stream_err or '未知')[:300]}\n"
                                "去查 Zeabur Variables（CHAT_MODEL_NAME / CHAT_API_KEY）或账户额度。"
                            )
                            await asyncio.to_thread(
                                lambda s=_crash: requests.post(
                                    f"{base_url}/sendMessage",
                                    json={"chat_id": chat_id, "text": s},
                                    timeout=15
                                )
                            )
                            await asyncio.to_thread(
                                _save_memory_to_db, "🪦 坠机记录",
                                f"用户: {text}\n坠机原因: {(_stream_err or '未知')[:300]}", "流水", "故障", "Crash"
                            )

                        reply = "\n".join(reply_parts)

                        if reply:'''


with open('heartbeat.py', 'r', encoding='utf-8') as f:
    s = f.read()

assert 'async def async_telegram_polling():' in s, "未找到 TG 轮询函数"
assert OLD_BLOCK in s, "未找到回复代码块"

s = s.replace('async def async_telegram_polling():',
              NEW_FUNC + '\n\n\nasync def async_telegram_polling():', 1)
s = s.replace(OLD_BLOCK, NEW_BLOCK, 1)

with open('heartbeat.py', 'w', encoding='utf-8') as f:
    f.write(s)

print("patched: heartbeat.py (typewriter v2)")
