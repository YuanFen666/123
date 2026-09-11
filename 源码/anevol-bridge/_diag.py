# -*- coding: utf-8 -*-
"""诊断 ANEVOL 服务端当前状态：区分“题库无答案(code=0)”与“服务端故障(500/超时)”"""
import json
import time
import urllib.error
import urllib.request

TOKEN = "在这里填你的 ANEVOL token（不要提交真实值）"
URL = "https://tiku.anevol.cn/api/search?TOKEN = "在这里填你的 ANEVOL token（不要提交真实值）"正常短题", {"title": "中国的首都是"}),
    ("正常选择题", {"title": "下列哪个数是质数？", "options": "A. 4\nB. 5\nC. 6\nD. 8", "type": "single"}),
    ("乱码题干(截图同款)", {"title": "均策略络空华的落子候训进行地展和评估，拉助圻策", "type": "single"}),
    ("乱码题干(截图同款2)", {"title": "未来人猩智能发魔播可能包挫以", "type": "multiple"}),
    ("超长题干+选项", {"title": "未来人工智能发展可能包括以下哪些方面", "options": "A. 边缘人工智能（Edge AI）的普及，实现数据本地处理\nB. 多模态智能的发展，融合文本、图像、语音等多种信息\nC. 人工智能安全防护技术的强化，应对潜在风险", "type": "multiple"}),
    ("判断题", {"title": "蒙特卡洛树搜索算法可以依概率坍缩", "type": "judgement"}),
    ("空选项数组", {"title": "下面哪个不是编程语言", "options": [], "type": "single"}),
]


def call(payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(URL, data=data, headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.loads(r.read().decode("utf-8"))
            return r.status, body.get("code"), (body.get("answer") or body.get("msg") or "")[:36], None, time.time() - t0
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        return e.code, body.get("code"), (body.get("msg") or body.get("detail") or "")[:36], None, time.time() - t0
    except Exception as e:
        return None, None, "", "%s: %s" % (type(e).__name__, e), time.time() - t0


print("%-20s %-6s %-5s %-38s %s" % ("用例", "HTTP", "code", "answer/msg", "耗时"))
print("-" * 100)
for name, payload in CASES:
    status, code, text, err, dt = call(payload)
    print("%-20s %-6s %-5s %-38s %.1fs%s" % (name, status, code, text, dt, ("  ERR=" + err) if err else ""))

print("-" * 100)
print("连续压测：同一道普通题打 8 次，统计 500/超时出现率")
ok = bad = 0
for i in range(8):
    status, code, text, err, dt = call({"title": "太阳系中直径最大的行星是"})
    if status == 200 and code == 1:
        ok += 1
        print("  %d) OK   %-24s %.1fs" % (i + 1, text, dt))
    else:
        bad += 1
        print("  %d) FAIL HTTP=%s code=%s %s %s" % (i + 1, status, code, text, err or ""))
    time.sleep(0.5)
print("成功 %d / 失败 %d" % (ok, bad))
