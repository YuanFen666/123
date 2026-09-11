# -*- coding: utf-8 -*-
"""
ANEVOL 接口体检：区分「服务端故障」和「请求姿势不对 / 密钥问题」。
用法: python probe_anevol.py [token]
token 优先取参数, 否则读 E:\\system\\Desktop\\anevol_token.txt
"""
import json
import os
import sys
import time

import requests

TOKEN_FILE = r"E:\system\Desktop\anevol_token.txt"
API = "https://tiku.anevol.cn/api/search"


def load_token():
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    for p in (TOKEN_FILE, os.path.join(os.path.dirname(os.path.abspath(__file__)), "anevol_token.txt")):
        try:
            if os.path.isfile(p):
                t = open(p, encoding="utf-8-sig").read().strip()
                if t:
                    return t
        except Exception:
            pass
    return os.environ.get("ANEVOL_TOKEN", "").strip()


def probe(name, token, payload, method="POST", url=API, params=None, timeout=(5, 40)):
    """返回 (状态, 说明)"""
    q = dict(params or {})
    if token is not None:
        q["token"] = token
    t0 = time.time()
    try:
        if method == "POST":
            r = requests.post(url, params=q, json=payload, timeout=timeout, verify=False)
        else:
            r = requests.get(url, params=q, timeout=timeout, verify=False)
        dt = time.time() - t0
        body = r.text.strip().replace("\n", " ")[:220]
        print("  [%-22s] HTTP %s  %5.1fs  %s" % (name, r.status_code, dt, body))
        return r.status_code, body
    except Exception as e:
        dt = time.time() - t0
        print("  [%-22s] %s  %5.1fs  %s" % (name, type(e).__name__, dt, str(e)[:110]))
        return None, str(e)


def main():
    tok = load_token()
    if not tok:
        print("!! 没找到 token")
        return 1
    print("=" * 82)
    print(" ANEVOL 接口体检   token=%s...(%d位)" % (tok[:8], len(tok)))
    print("=" * 82)

    q_single = {
        "title": "我国现行宪法是哪一年颁布的？",
        "options": "A. 1954年\nB. 1978年\nC. 1982年\nD. 1993年",
        "type": "single",
    }

    print("\n--- 1. 正常请求（我们实际用的姿势） ---")
    probe("正常+真token", tok, q_single)

    print("\n--- 2. 换 token（判断是密钥还是服务端） ---")
    probe("乱写的token", "deadbeef" * 8, q_single)
    probe("空token", "", q_single)
    probe("不带token", None, q_single)

    print("\n--- 3. 换请求姿势（判断是不是我们字段写错了） ---")
    probe("不带options", tok, {"title": "我国现行宪法是哪一年颁布的？", "type": "single"})
    probe("不带type", tok, {"title": "我国现行宪法是哪一年颁布的？"})
    probe("最简title", tok, {"title": "1+1=?"})
    probe("question字段", tok, {"question": "我国现行宪法是哪一年颁布的？", "type": "single"})

    print("\n--- 4. GET / 直接访问 ---")
    probe("GET根路径", tok, None, method="GET", url="https://tiku.anevol.cn/", timeout=(5, 20))
    probe("GET /api/search", tok, None, method="GET", timeout=(5, 20))

    print("\n" + "=" * 82)
    print(" 判读：")
    print("  · 真token 和 乱写token 返回**一样**的 500/msg → 服务端故障（后端 AI 挂了），跟你无关")
    print("  · 乱写token 返回**不同**的（401/密钥无效/额度） → 说明请求能到达业务层，问题是你的额度/密钥")
    print("  · 换姿势后能成功 → 说明我们字段写错了，改代码即可")
    print("=" * 82)
    return 0


if __name__ == "__main__":
    sys.exit(main())
