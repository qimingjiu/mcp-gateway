"""构建期补丁：让 LLM 调用兼容只允许 temperature=1 的模型（如 kimi-k3）。
原理：按原温度调用，若报错含 temperature 字样则自动降级为 temperature=1 重试。
本脚本在 Docker 构建时对 server.py / napcat.py 各打一处补丁，构建完即删除。
若本地运行本项目，在启动前执行一次 python patch_temperature.py 即可。
"""

def patch(path, old, new):
    with open(path, 'r', encoding='utf-8') as f:
        s = f.read()
    assert old in s, f"{path} 未找到目标代码，补丁未应用"
    with open(path, 'w', encoding='utf-8') as f:
        f.write(s.replace(old, new, 1))
    print(f"patched: {path}")


# server.py —— 中心函数 _ask_llm_async 的 _call
patch('server.py',
'''    def _call():
        return client.chat.completions.create(model=model_name, messages=messages, temperature=temperature)''',
'''    def _call():
        try:
            return client.chat.completions.create(model=model_name, messages=messages, temperature=temperature)
        except Exception as e:
            # 部分模型（如 kimi-k3）只允许 temperature=1，自动降级重试
            if "temperature" in str(e).lower():
                return client.chat.completions.create(model=model_name, messages=messages, temperature=1)
            raise''')

# napcat.py —— 全渠道阶段总结的直传调用
patch('napcat.py',
'''                        summary = client.chat.completions.create(
                            model=model_name,
                            messages=[{"role": "user", "content": prompt}],
                            temperature=0.7
                        ).choices[0].message.content.strip()''',
'''                        try:
                            summary = client.chat.completions.create(
                                model=model_name,
                                messages=[{"role": "user", "content": prompt}],
                                temperature=0.7
                            ).choices[0].message.content.strip()
                        except Exception as e:
                            if "temperature" in str(e).lower():
                                summary = client.chat.completions.create(
                                    model=model_name,
                                    messages=[{"role": "user", "content": prompt}],
                                    temperature=1
                                ).choices[0].message.content.strip()
                            else:
                                raise''')

print("all patches applied")
