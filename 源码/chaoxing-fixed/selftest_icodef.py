# -*- coding: utf-8 -*-
"""
TikuIcodef（网课小工具题库 / GO题）离线自测
==========================================
用假响应走通「icodef 返回 -> Tiku.query 校验 -> chaoxing 最终提交的选项字母」整条链路，
不依赖网络，也不依赖超星接口。

重点验证三件事（都是实测踩出来的）：
  1. icodef 返回的是「选项原文」而不是字母，而多选题用 '#' 连接多个答案；
     交给 chaoxing 前必须归一化，否则 clean_res() 之后匹配不上，答案被丢弃。
  2. 官方限「1 并发」，而 chaoxing 默认 jobs=8：
     本测试用多线程并发调用，断言真实并发数恒为 1（串行化生效）。
  3. code=-1（题不在库里）是正常业务结果，不能计入熔断；
     code=-2（触发流控）才是服务级故障，要退避重试。
"""
import json
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api.answer as A
from api.answer import TikuIcodef
from api.answer_check import check_answer, cut


# ---------------------------------------------------------------- chaoxing 侧逻辑复刻
def clean_res(res):
    if isinstance(res, str):
        res = [res]
    return [re.sub(r"^[A-Za-z]|[.,!?;:，。！？；：]", "", c).strip() for c in res]


def is_subsequence(a, o):
    it = iter(o)
    return all(c in it for c in a)


def resolve_single(options_str, res):
    options_list = cut(options_str)
    if options_list is None:
        return ""
    t_res = clean_res(res)
    for o in options_list:
        if is_subsequence(t_res[0], o):
            return o[:1]
    return ""


def resolve_multiple(options_str, res):
    options_list = cut(options_str)
    res_list = cut(res)
    if res_list is None or options_list is None:
        return ""
    answer = ""
    for _a in clean_res(res_list):
        for o in options_list:
            if is_subsequence(_a, o):
                answer += o[:1]
    return "".join(sorted(answer))


# ---------------------------------------------------------------- 假 icodef 响应
class FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


CONF = {
    "provider": "TikuIcodef",
    "submit": "true",
    "cover_rate": "0.8",
    "true_list": "正确,对,√,是",
    "false_list": "错误,错,×,否,不对,不正确",
    "icodef_url": "https://cx.icodef.com/wyn-nb?v=4",
    "icodef_min_interval": "0",
    "icodef_timeout": "5",
}

CALLS = []


def make_tiku(body, status=200, conf=None):
    """构造 TikuIcodef，并把 requests.post 换成本地假响应。"""
    t = TikuIcodef()
    t.config_set(conf or CONF)
    t.init_tiku()
    CALLS.clear()

    def fake_post(url, **kw):
        CALLS.append({"url": url, "data": kw.get("data"), "headers": kw.get("headers")})
        return FakeResp(status, body)

    A.requests.post = fake_post
    return t


# ---------------------------------------------------------------- 用例
OPTIONS_SINGLE = "A. 实事求是, 群众路线\nB. 中国共产党的领导\nC. 北京、上海"
OPTIONS_MULTI = "A. 科学社会主义理论\nB. 马克思主义哲学\nC. 马克思主义政治经济学"

CASES = [
    # (说明, 题目类型, 选项, icodef 返回, 期望最终提交结果)
    ("单选-返回选项原文（实测《诗经》那种）", "single", OPTIONS_SINGLE, "中国共产党的领导", "B"),
    ("单选-原文里含顿号，净化后仍要匹配到 C", "single", OPTIONS_SINGLE, "北京、上海", "C"),
    ("单选-题库返回字母 A", "single", OPTIONS_SINGLE, "A", "A"),
    ("多选-'#' 连接多个答案（icodef 实测格式）", "multiple", OPTIONS_MULTI,
     "科学社会主义理论#马克思主义哲学#马克思主义政治经济学", "ABC"),
    ("多选-只中两个", "multiple", OPTIONS_MULTI, "马克思主义哲学#马克思主义政治经济学", "BC"),
    ("多选-题库返回字母 AB", "multiple", OPTIONS_MULTI, "AB", "AB"),
    ("判断-返回'正确'", "judgement", "A. 正确\nB. 错误", "正确", "正确"),
    ("判断-返回'×'", "judgement", "A. 正确\nB. 错误", "×", "错误"),
    ("填空-明文答案", "completion", "", "社会主义初级阶段", "社会主义初级阶段"),
]


def run_cases():
    passed = failed = 0
    for desc, qtype, options, icodef_ans, expect in CASES:
        tiku = make_tiku({"code": 1, "data": icodef_ans, "msg": ""})
        q_info = {"title": "测试题目", "options": options, "type": qtype}

        raw = tiku._query(q_info)
        gate = check_answer(raw, qtype, tiku) if raw else False

        if qtype == "single":
            final = resolve_single(options, raw) if raw else ""
        elif qtype == "multiple":
            final = resolve_multiple(options, raw) if raw else ""
        else:
            final = raw or ""

        ok = (final == expect) and gate
        passed += ok
        failed += (not ok)
        print(" [{}] {}".format("PASS" if ok else "FAIL", desc))
        if not ok:
            print("        icodef 返回={!r}  _query 输出={!r}  类型校验={}  最终提交={!r} (期望 {!r})".format(
                icodef_ans, raw, gate, final, expect))
    return passed, failed


def run_error_paths():
    results = []

    def case(name, body, status, expect_calls, expect_ans=None, check=None):
        t = make_tiku(body, status)
        ans = t._query({"title": "测试题", "options": OPTIONS_SINGLE, "type": "single"})
        n = len(CALLS)
        ok = (n == expect_calls) and (expect_ans is None or ans == expect_ans)
        extra = ""
        if check:
            r, extra = check(t)
            ok = ok and r
        print(" [{}] {:<44} 请求={}次(期望{}) 返回={!r}{}".format(
            "PASS" if ok else "FAIL", name, n, expect_calls, ans, extra))
        results.append(ok)

    # 1) 题不在库里：code=-1 → 1 次请求，返回 None，且不计入熔断
    case("code=-1 题不在库 → 1次, 不计熔断",
         {"code": -1, "data": "李恒雅正在吃晚饭饭...(未搜索到答案)", "msg": "未搜索到答案"},
         200, 1, None,
         check=lambda t: (t._pause_remaining() == 0 and t._consec_fail == 0,
                          "  熔断剩余={:.0f}s 连续失败={}".format(t._pause_remaining(), t._consec_fail)))

    # 2) 触发流控：code=-2 → 退避重试到 3 次
    case("code=-2 触发流控 → 重试 3 次",
         {"code": -2, "data": "李恒雅忙不过来啦,触发流控限制: 1并发限制", "msg": "触发流控限制: 1并发限制"},
         200, 3, None)

    # 3) 服务端 500 → 属基础设施抖动，重试满 3 次（响应很快，代价只有 2 秒退避）
    case("HTTP 500 → 重试 3 次", '{"code":0,"msg":"服务异常"}', 500, 3, None)

    # 4) 200 但 code=0 → 1 次
    case("200 + code=0 → 1 次", {"code": 0, "msg": "没有找到答案"}, 200, 1, None)

    # 5) code=1 但内容写着没搜到 → 当成搜不到，返回 None，不计熔断
    case("code=1 但内容是'未搜索到答案' → None",
         {"code": 1, "data": "未搜索到答案", "msg": ""}, 200, 1, None,
         check=lambda t: (t._pause_remaining() == 0, "  熔断剩余={:.0f}s".format(t._pause_remaining())))

    # 6) 超时 → 不重试
    def timeout_handler(url, **kw):
        CALLS.append(1)
        import requests.exceptions as rex
        raise rex.ReadTimeout("read timeout")

    t = TikuIcodef()
    t.config_set(CONF)
    t.init_tiku()
    CALLS.clear()
    A.requests.post = timeout_handler
    ans = t._query({"title": "测试题", "options": OPTIONS_SINGLE, "type": "single"})
    ok = (len(CALLS) == 1 and ans is None)
    print(" [{}] {:<44} 请求={}次(期望1) 返回={!r}".format("PASS" if ok else "FAIL", "读超时 → 不重试", len(CALLS), ans))
    results.append(ok)

    # 7) 连续 5 次服务级故障 → 熔断，第 6 题直接跳过
    #    每题重试满 3 次，所以前 5 题共 15 次请求；第 6 题起被熔断拦住，不再发请求
    t = make_tiku({"code": 0, "msg": "服务异常"}, 500)
    for i in range(5):
        t._query({"title": "题%d" % i, "options": OPTIONS_SINGLE, "type": "single"})
    n5 = len(CALLS)
    t._query({"title": "题X", "options": OPTIONS_SINGLE, "type": "single"})
    n6 = len(CALLS)
    ok = (n5 == 15 and n6 == 15 and t._pause_remaining() > 0)
    print(" [{}] {:<44} 前5题={}次(期望15) 第6题累计={}次(期望15) 熔断剩余={:.0f}s".format(
        "PASS" if ok else "FAIL", "连续5次服务故障 → 熔断并跳过", n5, n6, t._pause_remaining()))
    results.append(ok)

    # 8) 连续 6 次「题不在库」(code=-1) → 不应该熔断
    t = make_tiku({"code": -1, "data": "未搜索到答案", "msg": "未搜索到答案"})
    for i in range(6):
        t._query({"title": "题%d" % i, "options": OPTIONS_SINGLE, "type": "single"})
    ok = (len(CALLS) == 6 and t._pause_remaining() == 0)
    print(" [{}] {:<44} 6题都发了请求={}次(期望6) 熔断剩余={:.0f}s(期望0)".format(
        "PASS" if ok else "FAIL", "题不在库 → 不计入熔断", len(CALLS), t._pause_remaining()))
    results.append(ok)

    return results


def run_concurrency():
    """官方限 1 并发，断言多线程下真实并发数恒为 1。"""
    live = {"cur": 0, "max": 0}
    gate = threading.Lock()

    def handler(url, **kw):
        with gate:
            live["cur"] += 1
            live["max"] = max(live["max"], live["cur"])
        time.sleep(0.1)
        with gate:
            live["cur"] -= 1
        return FakeResp(200, {"code": 1, "data": "中国共产党的领导", "msg": ""})

    t = make_tiku({"code": 1, "data": "x"})
    A.requests.post = handler
    TikuIcodef._serial_last_call = 0.0

    outs = []
    threads = [
        threading.Thread(
            target=lambda i=i: outs.append(
                t._query({"title": "并发题%d" % i, "options": OPTIONS_SINGLE, "type": "single"})))
        for i in range(6)
    ]
    start = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.time() - start

    ok = (live["max"] == 1 and len(outs) == 6 and all(o for o in outs))
    print(" [{}] {:<44} 6 线程并发调用 → 实际并发数={} (期望1) 全部拿到答案={} 耗时={:.2f}s".format(
        "PASS" if ok else "FAIL", "并发调用被串行化（1 并发限制）",
        live["max"], all(o for o in outs), elapsed))
    return [ok]


def main():
    print("=" * 96)
    print(" TikuIcodef（网课小工具题库 / GO题）离线自测")
    print("=" * 96)

    p, f = run_cases()

    print("-" * 96)
    print(" 错误路径 / 熔断器 / 并发控制")
    print("-" * 96)
    results = run_error_paths()
    results += run_concurrency()

    p += sum(results)
    f += len(results) - sum(results)

    print("-" * 96)
    print(" 结果: {} passed, {} failed".format(p, f))
    print("=" * 96)
    return 0 if f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
