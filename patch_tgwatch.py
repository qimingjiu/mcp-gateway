# -*- coding: utf-8 -*-
"""
patch_tgwatch.py —— TG 链路「侦察兵」可观测性补丁
=================================================
背景：TG 静默（不回复、无落库、无 Crash）时，print 因 stdout 缓冲在 Zeabur 日志不可见，
无法远程诊断。本补丁让乌鸦把 TG 链路状态「自我上报」到 memories（tags=TG_Polling）：

1. heartbeat.py 新增 _report_tg_fault() 模块级助手（同指纹 5 分钟去重，防刷库）
2. token 未配置 → 上报「TG 轮询休眠」后退出（此前只 print）
3. 启动自检：上报 token 状态 + 主动向 TG_CHAT_ID 发一条侦察兵消息，验证发送链路
4. getUpdates 返回 ok=false（401/409 等）→ 上报错误详情（此前静默重试）
5. 回复生成阶段异常 → 上报（此前只 print，消息来了但回复炸了我们看不见）
6. 轮询循环外层异常（网络错误等）→ 上报
7. server.py / heartbeat.py 的 _MONOLOGUE_TAGS 加入 "TG_Polling"：探针记录永不喂回大脑

探针记录 category=流水（importance 1），两天后由日记 worker 自动清理，不污染长期记忆。
"""

import sys

PATCHES = [
    # ------------------------------------------------------------------
    # 1. heartbeat.py：插入 _report_tg_fault 模块级助手（放在轮询函数之前）
    # ------------------------------------------------------------------
    (
        "heartbeat.py",
        '''async def async_telegram_polling():
    """轮询 Telegram Bot 的 getUpdates 接口，接收并处理用户消息。"""''',
        '''# ==========================================
# 🛰️ TG 链路故障自我上报（侦察兵）
# 同指纹 5 分钟内只报一次，避免刷库；tags=TG_Polling 已被独白过滤器屏蔽
# ==========================================
_TG_FAULT_CACHE = {}

def _report_tg_fault(kind: str, detail: str):
    """把 TG 链路的启动状态/故障写进 memories，方便事后从数据库远程诊断。"""
    try:
        fp = f"{kind}|{detail[:120]}"
        now = time.time()
        if now - _TG_FAULT_CACHE.get(fp, 0) < 300:
            return
        _TG_FAULT_CACHE[fp] = now
        from server import _save_memory_to_db
        threading.Thread(
            target=_save_memory_to_db,
            args=(f"🛰️ {kind}", str(detail)[:800], "流水", "故障", "TG_Polling"),
            daemon=True,
        ).start()
    except Exception:
        pass


async def async_telegram_polling():
    """轮询 Telegram Bot 的 getUpdates 接口，接收并处理用户消息。"""''',
        "heartbeat: 插入侦察兵上报助手",
    ),
    # ------------------------------------------------------------------
    # 2. heartbeat.py：token 未配置 → 上报后退出
    # ------------------------------------------------------------------
    (
        "heartbeat.py",
        '''    if not token:
        print("⚠️ 未配置 TG_BOT_TOKEN，Telegram 轮询休眠。")
        return''',
        '''    if not token:
        print("⚠️ 未配置 TG_BOT_TOKEN，Telegram 轮询休眠。")
        _report_tg_fault("TG 轮询休眠", "TG_BOT_TOKEN 未配置（Zeabur Variables 缺失），轮询线程直接退出。")
        return''',
        "heartbeat: token 缺失上报",
    ),
    # ------------------------------------------------------------------
    # 3. heartbeat.py：启动自检 + 主动发送探针
    # ------------------------------------------------------------------
    (
        "heartbeat.py",
        '''    base_url = f"https://api.telegram.org/bot{token}"
    offset = 0''',
        '''    base_url = f"https://api.telegram.org/bot{token}"
    offset = 0

    # 🛰️ 侦察兵启动自检：上报 token 状态，并主动发一条消息验证发送链路
    _report_tg_fault("TG 轮询线程启动", f"token 已配置（长度 {len(token)}），开始轮询。")
    try:
        _probe_chat = os.environ.get("TG_CHAT_ID", "").strip()
        if _probe_chat:
            _probe_resp = requests.post(
                f"{base_url}/sendMessage",
                json={"chat_id": _probe_chat, "text": "🛰️ 乌鸦侦察兵上线：发送链路正常，开始竖起耳朵。"},
                timeout=15,
            ).json()
            if not _probe_resp.get("ok"):
                _report_tg_fault("侦察兵发送失败", json.dumps(_probe_resp, ensure_ascii=False)[:400])
    except Exception as _pe:
        _report_tg_fault("侦察兵发送异常", f"{type(_pe).__name__}: {_pe}")''',
        "heartbeat: 启动自检+发送探针",
    ),
    # ------------------------------------------------------------------
    # 4. heartbeat.py：getUpdates ok=false → 上报详情
    # ------------------------------------------------------------------
    (
        "heartbeat.py",
        '''            if not data.get("ok"):
                await asyncio.sleep(5)
                continue''',
        '''            if not data.get("ok"):
                _report_tg_fault("getUpdates 被拒绝", json.dumps(data, ensure_ascii=False)[:400])
                await asyncio.sleep(5)
                continue''',
        "heartbeat: getUpdates 失败上报",
    ),
    # ------------------------------------------------------------------
    # 5. heartbeat.py：回复生成阶段异常 → 上报
    # ------------------------------------------------------------------
    (
        "heartbeat.py",
        '''                    except Exception as e:
                        print(f"❌ TG 回复生成失败: {e}")''',
        '''                    except Exception as e:
                        print(f"❌ TG 回复生成失败: {e}")
                        _report_tg_fault("TG 回复生成失败", f"{type(e).__name__}: {e}")''',
        "heartbeat: 回复生成失败上报",
    ),
    # ------------------------------------------------------------------
    # 6. heartbeat.py：轮询循环外层异常 → 上报
    # ------------------------------------------------------------------
    (
        "heartbeat.py",
        '''        except Exception as e:
            print(f"❌ TG 轮询错误: {e}")
            await asyncio.sleep(5)''',
        '''        except Exception as e:
            print(f"❌ TG 轮询错误: {e}")
            _report_tg_fault("TG 轮询循环异常", f"{type(e).__name__}: {e}")
            await asyncio.sleep(5)''',
        "heartbeat: 轮询循环异常上报",
    ),
    # ------------------------------------------------------------------
    # 7. heartbeat.py：日记独白过滤器屏蔽 TG_Polling
    # ------------------------------------------------------------------
    (
        "heartbeat.py",
        '''        _MONOLOGUE_TAGS = {"Heartbeat", "Summary", "Reminder", "Crash", "Email_Process"}''',
        '''        _MONOLOGUE_TAGS = {"Heartbeat", "Summary", "Reminder", "Crash", "Email_Process", "TG_Polling"}''',
        "heartbeat: 日记过滤器屏蔽 TG_Polling",
    ),
    # ------------------------------------------------------------------
    # 8. server.py：核心大脑过滤器屏蔽 TG_Polling
    # ------------------------------------------------------------------
    (
        "server.py",
        '''        _MONOLOGUE_TAGS = {"Heartbeat", "Summary", "Reminder", "Crash", "Email_Process"}''',
        '''        _MONOLOGUE_TAGS = {"Heartbeat", "Summary", "Reminder", "Crash", "Email_Process", "TG_Polling"}''',
        "server: 大脑过滤器屏蔽 TG_Polling",
    ),
]


def apply_patches():
    for fname, old, new, label in PATCHES:
        with open(fname, "r", encoding="utf-8") as f:
            src = f.read()
        count = src.count(old)
        assert count == 1, f"[{label}] 锚点在 {fname} 中出现 {count} 次（应为 1），补丁中止！"
        src = src.replace(old, new)
        with open(fname, "w", encoding="utf-8") as f:
            f.write(src)
        print(f"✅ {label}")
    print("🛰️ 侦察兵补丁全部应用完成。")


if __name__ == "__main__":
    apply_patches()
