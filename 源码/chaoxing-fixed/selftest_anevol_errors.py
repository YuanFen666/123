# -*- coding: utf-8 -*-
"""
TikuAnevol 错误路径自测
========================
用假响应验证：不同错误各自会发几次请求、熔断有没有生效。

背景（线上实测）：
  ANEVOL 真实 token 请求 500，响应体是
  {"code":0,"detail":"主模型和所有备用模型都失败了。最后的错误: 调用AI API时出错: AI API调用失败: HTTP 402"}
  注意字段叫 detail 而不是 msg —— 只读 msg 的话会漏判，导致每道题白跑 3 次（≈77 秒）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
import api.answer as A
from api.answer import TikuAnevol

CONF = {
    "provider": "TikuAnevol",
    "submit": "false",
    "cover_rate": "0.8",
    "true_list": "正确,对,√,是",
    "false_list": "错误,错,×,否,不对,不正确",
    "url": "",
    "tokens": "TEST_TOKEN",
    "anevol_min_interval": "0.01",
}

PAYLOAD_402 = (
    '{"code":0,"detail":"主模型和所有备用模型都失败了。最后的错误: '
    '调用AI API时出错: AI API调用失败: HTTP 402 Payment Required"}'
)

CALLS = []


class FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return json.loads(self.text)


class FakeError(Exception):
    pass


def build(handler):
    """handler() 每次被调用返回一个 FakeResp 或抛异常。"""
    t = TikuAnevol()
    t.config_set(CONF)
    t.init_tiku()
    CALLS.clear()

    def fake_post(url, **kw):
        CALLS.append(time.time())
        return handler()

    A.requests.post = fake_post
    return t


def run_case(name, handler, expect_calls, also_check=None):
    t = build(handler)
    ans = t._query({"title": "测试题", "options": "A 甲\nB 乙", "type": "single"})
    n = len(CALLS)
    ok = (n == expect_calls)
    extra = ""
    if also_check:
        r = also_check(t)
        ok = ok and r[0]
        extra = r[1]
    print(" [%s] %-46s 请求次数=%d(期望%d) 返回=%r%s"
          % ("PASS" if ok else "FAIL", name, n, expect_calls, ans, extra))
    return ok


def main():
    import requests.exceptions as rex
    results = []

    print("=" * 96)
    print(" TikuAnevol 错误路径自测（离线，假响应）")
    print("=" * 96)

    # 1) 500 + detail 里带 402（线上真实情况）→ 只发 1 次，并触发致命熔断
    results.append(run_case(
        "500 + detail 含 402 → 不重试，致命熔断",
        lambda: FakeResp(500, PAYLOAD_402),
        1,
        also_check=lambda t: (t._pause_remaining() > 500, "  熔断剩余=%.0fs" % t._pause_remaining()),
    ))

    # 2) 熔断后：下一题直接跳过，不再发请求
    t = build(lambda: FakeResp(500, PAYLOAD_402))
    t._query({"title": "题1", "options": "A 甲\nB 乙", "type": "single"})
    n1 = len(CALLS)
    t._query({"title": "题2", "options": "A 甲\nB 乙", "type": "single"})
    n2 = len(CALLS)
    ok = (n1 == 1 and n2 == 1)
    print(" [%s] %-46s 第一题=%d次 第二题累计=%d次(期望1)" % ("PASS" if ok else "FAIL",
          "致命熔断后，后续题目直接跳过", n1, n2))
    results.append(ok)

    # 3) 500 + detail 是普通服务端故障（非额度）→ 1 次（业务错误也重试无意义）
    results.append(run_case(
        "500 + detail 普通故障 → 1 次",
        lambda: FakeResp(500, '{"code":0,"detail":"AI服务暂时不可用，请稍后重试"}'),
        1,
    ))

    # 4) 401 密钥无效 → 1 次
    results.append(run_case(
        "401 密钥无效 → 1 次",
        lambda: FakeResp(401, '{"code":0,"msg":"无效的API密钥或调用次数不足"}'),
        1,
    ))

    # 5) 200 但 code=0 → 1 次
    results.append(run_case(
        "200 + code=0 → 1 次",
        lambda: FakeResp(200, '{"code":0,"msg":"没有找到答案"}'),
        1,
    ))

    # 6) 读超时 → 不重试（1 次）
    def timeout_handler():
        raise rex.ReadTimeout("read timeout")
    results.append(run_case("读超时 → 不重试（1 次）", timeout_handler, 1))

    # 7) 连接错误 → 重试 1 次（共 2 次）
    def conn_handler():
        raise rex.ConnectionError("connection refused")
    results.append(run_case("连接错误 → 重试 1 次（共 2 次）", conn_handler, 2))

    # 8) 成功
    results.append(run_case(
        "成功 → 返回答案", lambda: FakeResp(200, '{"code":1,"answer":"B"}'), 1))

    # 9) 连接错误第 2 次成功
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise rex.ConnectionError("boom")
        return FakeResp(200, '{"code":1,"answer":"A"}')
    results.append(run_case("先连接错误后成功 → 自动恢复", flaky, 2))

    # ---- 熔断器行为 ----
    print("-" * 96)
    print(" 熔断器行为")
    print("-" * 96)

    def q(i, qtype="single"):
        return {"title": "题%d" % i, "options": "A 甲\nB 乙", "type": qtype}

    # 10) 连续 5 次服务级故障(500) → 熔断 120 秒；第 6 题直接跳过不再发请求
    t = build(lambda: FakeResp(500, '{"code":0,"detail":"AI服务暂时不可用，请稍后重试"}'))
    for i in range(5):
        t._query(q(i))
    n_after5 = len(CALLS)
    t._query(q(99))
    n_after6 = len(CALLS)
    ok = (n_after5 == 5 and n_after6 == 5 and 100 < t._pause_remaining() <= 120)
    print(" [%s] %-46s 前5题=%d次 第6题累计=%d次(期望5)  熔断剩余=%.0fs(期望~120)"
          % ("PASS" if ok else "FAIL", "连续5次服务故障→熔断120s并跳过后续", n_after5, n_after6,
             t._pause_remaining()))
    results.append(ok)

    # 11) 连续 5 次“题不在库里”(HTTP 200 + code=0) → **不应该**熔断
    t = build(lambda: FakeResp(200, '{"code":0,"msg":"没有找到相关答案"}'))
    for i in range(6):
        t._query(q(i))
    ok = (len(CALLS) == 6 and t._pause_remaining() == 0)
    print(" [%s] %-46s 6题都发了请求=%d次(期望6)  熔断剩余=%.0fs(期望0)"
          % ("PASS" if ok else "FAIL", "题不在库里→不计入熔断（正常搜不到不该停）",
             len(CALLS), t._pause_remaining()))
    results.append(ok)

    # 12) 熔断时长逐级翻倍：同一实例连续两轮服务故障 → 120s 后变 240s
    t = build(lambda: FakeResp(500, '{"code":0,"detail":"AI服务暂时不可用，请稍后重试"}'))
    for i in range(5):
        t._query(q(i))
    r1 = t._pause_remaining()
    t._pause_until = time.time() - 1     # 模拟等满第一轮暂停
    for i in range(5):
        t._query(q(i))
    r2 = t._pause_remaining()
    ok = (100 < r1 <= 120) and (200 < r2 <= 240)
    print(" [%s] %-46s 第一轮=%.0fs(期望~120) 第二轮=%.0fs(期望~240)"
          % ("PASS" if ok else "FAIL", "熔断时长逐级翻倍 120→240", r1, r2))
    results.append(ok)

    print("-" * 96)
    print(" 结果: %d passed, %d failed" % (sum(results), len(results) - sum(results)))
    print("=" * 96)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
