# -*- coding: utf-8 -*-
"""
ANEVOL 接口探针：对比不同鉴权方式/请求方式，定位"查询老是失败"的原因。

token 从 config.ini 里读，不写死在脚本里。
"""
import configparser
import json
import sys
import time

import requests
from urllib3 import disable_warnings, exceptions

disable_warnings(exceptions.InsecureRequestWarning)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

READ_TIMEOUT = 30          # 单次读取超时，别等太久
URL = "https://tiku.anevol.cn/api/search"
CFG = r"E:\system\Desktop\config.ini"

cfg = configparser.ConfigParser()
cfg.read(CFG, encoding="utf8")
TOKEN = (cfg["tiku"].get("tokens") or "").strip().split(",")[0].strip()
if not TOKEN:
    TOKEN = (cfg["tiku"].get("anevol_token") or "").strip()

print("token 长度 %d，前 8 位 %s..." % (len(TOKEN), TOKEN[:8]))
print("=" * 78)

PAYLOAD = {
    "title": "下列哪个数是质数？",
    "options": "A. 4\nB. 5\nC. 6\nD. 8",
    "type": "single",
}
PAYLOAD_SA = {"title": "1+1=?", "type": "short_answer"}


def probe(label, method="POST", params=None, headers=None, json_body=None, data=None):
    t0 = time.time()
    try:
        if method == "POST":
            r = requests.post(URL, params=params, headers=headers or {},
                              json=json_body, data=data, timeout=(5, READ_TIMEOUT), verify=False)
        else:
            r = requests.get(URL, params=params, headers=headers or {},
                             timeout=(5, READ_TIMEOUT), verify=False)
        dt = time.time() - t0
        body = r.text.strip().replace("\n", " ")
        print("[%-34s] HTTP %s  %.1fs" % (label, r.status_code, dt))
        print("   body: %s" % body[:220])
        return r.status_code, body
    except Exception as e:
        dt = time.time() - t0
        print("[%-34s] 异常 %s: %s  %.1fs" % (label, type(e).__name__, str(e)[:90], dt))
        return None, str(e)
    finally:
        print("-" * 78)


# 1) 我们现在用的方式：token 放 query
probe("① query ?token=真实 (post json 单选)", params={"token": TOKEN}, json_body=PAYLOAD)
# 2) 文档写的方式：X-API-Key 头
probe("② header X-API-Key=真实 (post json 单选)",
      headers={"X-API-Key": TOKEN}, json_body=PAYLOAD)
# 3) 完全不带鉴权
probe("③ 不带任何鉴权 (post json 单选)", json_body=PAYLOAD)
# 4) 假 token 走 header
probe("④ header X-API-Key=假 (对照组)",
      headers={"X-API-Key": "deadbeef" * 8}, json_body=PAYLOAD)
# 5) 假 token 走 query
probe("⑤ query ?token=假 (对照组)", params={"token": "deadbeef" * 8}, json_body=PAYLOAD)
# 6) 简答题（不带 options）
probe("⑥ header 真实 + 简答题(无options)",
      headers={"X-API-Key": TOKEN}, json_body=PAYLOAD_SA)
# 7) GET 方式
probe("⑦ GET ?token=真实&title=...&type=short_answer",
      method="GET", params={"token": TOKEN, "title": "1+1=?", "type": "short_answer"})
# 8) 完全不传 type
probe("⑧ header 真实 + 不传 type",
      headers={"X-API-Key": TOKEN}, json_body={"title": "1+1=?"})

print("=" * 78)
print("结论速查：")
print("  若 ① 和 ② 都是 500 而 ④/⑤ 是 401 → 鉴权没问题，确实是服务端 AI 故障")
print("  若 ② 成功而 ① 失败 → 我们把 token 放错位置了，改用 X-API-Key 头即可")
print("  若 ① 和 ② 都是 500，且 ③ 也 500 → 与鉴权无关，纯服务端问题")
print("  若出现 429 → 是限流（请求太频繁）")
