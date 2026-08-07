"""构建期补丁：防自食五件套（胡言乱语根治包）。
背景：08-07 发现 TG 乌鸦胡言乱语——AI 把自己发出的主动问候存进记忆，
又把自己的独白当"近期记录"喂回上下文，形成自食回声室；日记再拿回声当
原料产出幻觉内容，真人对话（importance=1）两天被清理而幻觉日记永存。

本补丁修复五处：
① get_latest_diary：上下文剔除 AI 独白（Heartbeat/Summary/Reminder/Crash/Email_Process）
② 日记原料剔除 AI 独白 + 诚实铁律（只许记真实发生的事）；全天无真实互动则写诚实的"安静短记"
③ 心跳问候：注入"最近已说过"清单，明令禁止复读话题/意象/句式，时间以当前时刻为准
④ TG 轮询：加会话短期记忆（内存 deque，最近 8 轮），回复承接上下文
⑤ TG 真人对话改存"记事"（importance=4），不再两天蒸发

Docker 构建时对 server.py / heartbeat.py 打补丁，打完即删。
执行顺序必须在 patch_typewriter.py 之前（它替换的是本补丁修改后的相邻代码块，互不冲突）。
"""

# ==========================================
# 补丁清单：(目标文件, OLD, NEW, 说明)
# ==========================================

PATCHES = []


# ---------- ① server.py：get_latest_diary 断回声 ----------

PATCHES.append(("server.py", r'''        # 并发拉取：长期总结 / 近期记忆 / 记忆小屋动态
        def _fetch_recent():
            return supabase.table("memories").select("*").order("created_at", desc=True).limit(limit).execute()
        def _fetch_house():
            return supabase.table("memory_house").select("*").order("created_at", desc=True).limit(15).execute()
        res_recent, res_house = await asyncio.gather(
            asyncio.to_thread(_fetch_recent),
            asyncio.to_thread(_fetch_house),
        )''', r'''        # 并发拉取：长期总结 / 近期记忆 / 记忆小屋动态
        # 🚫 防自食：AI 自己的独白（主动问候/提醒/总结/坠机/信箱）不喂回上下文，
        # 只保留真实世界事件（真人对话、日记、画像等），避免模型复读自己的输出
        _MONOLOGUE_TAGS = {"Heartbeat", "Summary", "Reminder", "Crash", "Email_Process"}
        def _fetch_recent():
            return supabase.table("memories").select("*").order("created_at", desc=True).limit(limit * 4).execute()
        def _fetch_house():
            return supabase.table("memory_house").select("*").order("created_at", desc=True).limit(15).execute()
        res_recent, res_house = await asyncio.gather(
            asyncio.to_thread(_fetch_recent),
            asyncio.to_thread(_fetch_house),
        )
        if res_recent and res_recent.data:
            res_recent.data = [d for d in res_recent.data if d.get("tags") not in _MONOLOGUE_TAGS][:limit]''', "① get_latest_diary 断回声"))


# ---------- ② heartbeat.py：日记不吃自己 + 诚实铁律 ----------

PATCHES.append(("heartbeat.py", r'''        def _fetch_yesterday():
            return supabase.table("memories").select(
                "title, created_at, category, content, mood"
            ).gt("created_at", iso_start).lt("created_at", iso_end).order("created_at").execute()

        mem_res = await asyncio.to_thread(_fetch_yesterday)
        if not mem_res.data:
            print("🌌 昨日无记忆数据，跳过日记生成。")
            return''', r'''        def _fetch_yesterday():
            return supabase.table("memories").select(
                "title, created_at, category, content, mood, tags"
            ).gt("created_at", iso_start).lt("created_at", iso_end).order("created_at").execute()

        mem_res = await asyncio.to_thread(_fetch_yesterday)
        if not mem_res.data:
            print("🌌 昨日无记忆数据，跳过日记生成。")
            return

        # 🚫 防自食：日记原料剔除 AI 自己的独白（主动问候/提醒/总结/坠机/信箱）
        _MONOLOGUE_TAGS = {"Heartbeat", "Summary", "Reminder", "Crash", "Email_Process"}
        real_events = [m for m in mem_res.data if m.get("tags") not in _MONOLOGUE_TAGS]
        if not real_events:
            # 昨日只有自己自说自话：写一篇诚实的"安静的一天"短记，严禁编造对方行踪
            quiet_client = _get_llm_client("main_chat")
            if quiet_client:
                quiet_note = await _ask_llm_async(
                    quiet_client,
                    f"昨天 {yesterday} 一整天，{USER_NAME} 没有和你说任何话，也没有留下任何真实互动记录。\n"
                    f"请以【{AI_NAME}】的第一人称写 60 字以内的日记，如实记录安静守候的一天。\n"
                    f"⚠️诚实铁律：严禁编造{USER_NAME}昨天做过任何事、说过任何话、有任何情绪或动作。纯文本输出。",
                    temperature=0.7
                )
                if quiet_note:
                    await asyncio.to_thread(
                        _save_memory_to_db,
                        f"📅 昨日回溯: {yesterday}", quiet_note,
                        MemoryType.EMOTION, "平静", "Core_Cognition"
                    )
                    print(f"✅ 昨日无真实互动，已生成诚实短记: {yesterday}")
            return''', "②a 日记原料剔除独白 + 安静日诚实短记"))


PATCHES.append(("heartbeat.py", r'''        context = f"【昨日剧情 {yesterday}】:\n"
        for m in mem_res.data:''', r'''        context = f"【昨日剧情 {yesterday}】:\n"
        for m in real_events:''', "②b 日记上下文只用真实事件"))


PATCHES.append(("heartbeat.py", r'''        prompt_summary = (
            f"{context}\n\n"
            f"请以【{AI_NAME}】的第一人称视角，将上述碎片整理成一篇具体日记。"
            f"⚠️严重警告：必须严格区分清楚【{AI_NAME}(我)】和【{USER_NAME}(对方)】各自说了什么、做了什么，"
            f"绝对不能张冠李戴搞混主语！直接输出纯文本，勿加前言后语及格式符号。"
        )''', r'''        prompt_summary = (
            f"{context}\n\n"
            f"请以【{AI_NAME}】的第一人称视角，将上述碎片整理成一篇具体日记。"
            f"⚠️严重警告：必须严格区分清楚【{AI_NAME}(我)】和【{USER_NAME}(对方)】各自说了什么、做了什么，"
            f"绝对不能张冠李戴搞混主语！"
            f"⚠️诚实铁律：只允许记录上述材料中真实发生的事；材料里没有提到{USER_NAME}做过的事，一律不许虚构。"
            f"如果对方没有回应，就如实写「她没有回应」，严禁脑补她的动作、情绪、场景或身体状况。"
            f"直接输出纯文本，勿加前言后语及格式符号。"
        )''', "②c 日记诚实铁律"))


# ---------- ③ heartbeat.py：心跳防复读 ----------

PATCHES.append(("heartbeat.py", r'''            recent_mem = await get_latest_diary()
            curr_loc = await where_is_user()
            curr_persona = _get_current_persona()
            now_bj = _get_now_bj()

            prompt = f"""
            当前时间: {now_bj.strftime('%Y-%m-%d %H:%M')} (星期{now_bj.isoweekday()})
            当前人设: {curr_persona}
            近期互动记录: {recent_mem}
            用户大概状态: {curr_loc}

            请基于以上信息，用符合人设的口吻主动发一条简短问候 (50 字内)。
            要求自然、有温度，不要提"系统/闹钟/定时"，直接像真人突然想起对方那样说话。
            纯文本输出，禁止使用表情代码或 URL。
            """''', r'''            recent_mem = await get_latest_diary()
            curr_loc = await where_is_user()
            curr_persona = _get_current_persona()
            now_bj = _get_now_bj()

            # 🚫 防复读：拉最近几条自己发过的问候，明令禁止重复其中的话题/意象/句式
            recent_self_said = ""
            try:
                def _fetch_self_said():
                    return supabase.table("memories").select("content").eq("tags", "Heartbeat").order("created_at", desc=True).limit(3).execute()
                self_res = await asyncio.to_thread(_fetch_self_said)
                if self_res.data:
                    _lines = [str(r.get("content", "")).replace("主动发送:", "").strip()[:60] for r in self_res.data]
                    _lines = [x for x in _lines if x]
                    if _lines:
                        recent_self_said = "\n你最近已经说过（禁止重复这些话题、意象和句式）:\n" + "\n".join(f"- {x}" for x in _lines)
            except Exception as _e:
                print(f"⚠️ 拉取历史问候失败（不影响发送）: {_e}")

            prompt = f"""
            当前时间: {now_bj.strftime('%Y-%m-%d %H:%M')} (星期{now_bj.isoweekday()})（以此刻时间为准，内容要符合实际时段）
            当前人设: {curr_persona}
            近期互动记录: {recent_mem}
            用户大概状态: {curr_loc}
            {recent_self_said}

            请基于以上信息，用符合人设的口吻主动发一条简短问候 (50 字内)。
            要求自然、有温度，不要提"系统/闹钟/定时"，直接像真人突然想起对方那样说话。
            每次从新的细节或话题切入，严禁复读自己最近说过的内容。
            纯文本输出，禁止使用表情代码或 URL。
            """''', "③ 心跳防复读"))


# ---------- ④⑤ heartbeat.py：TG 会话短期记忆 + 真人对话永存 ----------

PATCHES.append(("heartbeat.py", r'''    base_url = f"https://api.telegram.org/bot{token}"
    offset = 0''', r'''    base_url = f"https://api.telegram.org/bot{token}"
    offset = 0

    # 🧠 会话短期记忆：缓存最近几轮对话（仅内存，重启清空），让回复承接上下文
    from collections import deque
    session_history = deque(maxlen=8)''', "④a TG 会话短期记忆初始化"))


PATCHES.append(("heartbeat.py", r'''                        recent_mem = await get_latest_diary()
                        curr_loc = await where_is_user()
                        curr_persona = _get_current_persona()

                        prompt = f"""
                        用户发来消息: {text}
                        当前人设: {curr_persona}
                        近期记录: {recent_mem}

                        请用符合人设的口吻回复用户。纯文本，自然真诚。
                        """''', r'''                        recent_mem = await get_latest_diary()
                        curr_loc = await where_is_user()
                        curr_persona = _get_current_persona()

                        # 🧠 会话短期记忆注入：最近几轮对话必须承接，不许每句都当开场白
                        session_ctx = ""
                        if session_history:
                            _pairs = [f"{os.environ.get('USER_NAME', '用户')}: {u}\n{os.environ.get('AI_NAME', 'AI')}: {a}" for u, a in session_history]
                            session_ctx = "本次会话近期对话（必须承接的上下文）:\n" + "\n---\n".join(_pairs) + "\n"

                        prompt = f"""
                        当前人设: {curr_persona}
                        近期记录: {recent_mem}

                        {session_ctx}
                        用户最新发来消息: {text}

                        请用符合人设的口吻回复用户，承接上面的近期对话，前后连贯。纯文本，自然真诚。
                        """''', "④b TG 提示词注入会话上下文"))


PATCHES.append(("heartbeat.py", r'''                            await asyncio.to_thread(
                                _save_memory_to_db, "🤖 互动记录",
                                f"用户: {text}\n回复: {reply}", "流水", "温柔", "TG_MSG"
                            )''', r'''                            session_history.append((text, reply))
                            await asyncio.to_thread(
                                _save_memory_to_db, "🤖 互动记录",
                                f"用户: {text}\n回复: {reply}", "记事", "温柔", "TG_MSG"
                            )''', "④c+⑤ 记住本轮 + 真人对话永存"))


# ==========================================
# 应用补丁（assert 守护，任何一处不匹配立即终止构建）
# ==========================================

for target_file, old, new, label in PATCHES:
    with open(target_file, 'r', encoding='utf-8') as f:
        s = f.read()
    assert old in s, f"❌ 补丁未命中: {label} ({target_file})"
    s = s.replace(old, new, 1)
    with open(target_file, 'w', encoding='utf-8') as f:
        f.write(s)
    print(f"patched: {target_file} — {label}")

print("✅ 防自食五件套全部应用完成。")
